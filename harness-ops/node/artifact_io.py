"""Descriptor-relative host artifacts. Never reopen a checked untrusted path."""
import errno
import hashlib
import json
import os
import stat
import tarfile
import uuid


class ArtifactChanged(ValueError):
    """A writer changed an artifact while it was being described or archived."""


def relative_path(value):
    if not isinstance(value, str) or "\x00" in value or os.path.isabs(value) or ".." in value.split("/"):
        raise ValueError("path must be relative to this job's output")
    return value


def stamp(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def open_child(parent, name, directory=None):
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("invalid artifact component")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if directory is True:
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.ENODEV):
            raise ValueError("links, special files and non-directory path components are not allowed") from None
        raise
    st = os.fstat(fd)
    valid = stat.S_ISDIR(st.st_mode) if directory is True else stat.S_ISREG(st.st_mode) if directory is False else (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode))
    if not valid:
        os.close(fd)
        raise ValueError("links and special files are not readable artifacts")
    return fd


class SafeRoot:
    """Pin an operator-configured root once; all later components use openat."""
    def __init__(self, path, json_owner=None):
        self.fd = None
        if json_owner is not None:
            if (not isinstance(json_owner, (tuple, list)) or len(json_owner) != 2
                    or any(type(value) is not int or value < 0 for value in json_owner)):
                raise ValueError("json_owner must be a nonnegative UID/GID pair")
            if os.geteuid() != 0:
                raise ValueError("json_owner requires a privileged publisher")
            json_owner = tuple(json_owner)
        self.json_owner = json_owner
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            # Resolve configured aliases (e.g. macOS /var) before pinning the root.
            # Every resulting component is then opened without following links.
            for part in os.path.realpath(path).split("/"):
                if part:
                    child = open_child(fd, part, directory=True)
                    os.close(fd)
                    fd = child
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __del__(self):
        self.close()

    def open_fd(self, rel, directory=None):
        parts = [p for p in relative_path(rel).split("/") if p not in ("", ".")]
        fd = os.dup(self.fd)
        try:
            for index, part in enumerate(parts):
                child = open_child(fd, part, directory=True if index < len(parts) - 1 else directory)
                os.close(fd)
                fd = child
            if not parts and directory is False:
                raise ValueError("expected a regular file")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def open_file(self, rel):
        return os.fdopen(self.open_fd(rel, directory=False), "rb")

    def read_bytes(self, rel, maximum):
        with self.open_file(rel) as f:
            data = f.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("metadata exceeds its read limit")
        return data

    def read_json(self, rel, maximum=1 << 20):
        if self.json_owner is None:
            return json.loads(self.read_bytes(rel, maximum))
        with self.open_file(rel) as f:
            if os.fstat(f.fileno()).st_uid != self.json_owner[0]:
                raise ValueError("worker JSON must belong to the configured node identity")
            data = f.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("metadata exceeds its read limit")
        return json.loads(data)

    def _parent(self, rel):
        relative_path(rel)
        parent, name = os.path.split(rel)
        if name in ("", ".", ".."):
            raise ValueError("invalid file name")
        return self.open_fd(parent, directory=True), name

    def create_file(self, rel):
        parent, name = self._parent(rel)
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
            return os.fdopen(fd, "w+b")
        finally:
            os.close(parent)

    def replace(self, source, destination):
        src, source_name = self._parent(source)
        try:
            dst, destination_name = self._parent(destination)
            try:
                os.replace(source_name, destination_name, src_dir_fd=src, dst_dir_fd=dst)
            finally:
                os.close(dst)
        finally:
            os.close(src)

    def unlink(self, rel):
        parent, name = self._parent(rel)
        try:
            os.unlink(name, dir_fd=parent)
        finally:
            os.close(parent)

    def _sync_target(self, rel, directory=None):
        """Open the target and parent together; the caller owns both descriptors."""
        parts = [p for p in relative_path(rel).split("/") if p not in ("", ".")]
        if not parts:
            if directory is False:
                raise ValueError("expected a regular file")
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=self.fd)
            try:
                return parent, os.dup(self.fd)
            except BaseException:
                os.close(parent)
                raise
        parent, name = self._parent("/".join(parts))
        try:
            return parent, open_child(parent, name, directory=directory)
        except BaseException:
            os.close(parent)
            raise

    def sync(self, rel, directory=None):
        """Sync one pinned object and its directory entry, including rel=''."""
        parent, target = self._sync_target(rel, directory)
        try:
            os.fsync(target)
            os.fsync(parent)
        finally:
            os.close(target)
            os.close(parent)

    def mkdir(self, rel):
        """Create one directory, or verify an existing real directory, durably."""
        parent, name = self._parent(rel)
        try:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            target = open_child(parent, name, directory=True)
            try:
                os.fsync(target)
                os.fsync(parent)
            finally:
                os.close(target)
        finally:
            os.close(parent)

    def sync_tree(self, rel):
        """Sync a fixed tree leaf-first. The caller must exclude concurrent writers."""
        def visit(fd, depth=0):
            if depth > 64:
                raise ValueError("artifact directory nesting exceeds 64")
            before = os.fstat(fd)
            if stat.S_ISDIR(before.st_mode):
                for name in sorted(os.listdir(fd)):
                    child = open_child(fd, name)
                    try:
                        visit(child, depth + 1)
                    finally:
                        os.close(child)
            os.fsync(fd)
            if stamp(os.fstat(fd)) != stamp(before):
                raise ArtifactChanged("artifact changed during persistence barrier")

        parent, target = self._sync_target(rel)
        try:
            visit(target)
            os.fsync(parent)
        finally:
            os.close(target)
            os.close(parent)

    def _publish_json(self, rel, value, once):
        # Keep the same parent fd through publication and its durability barrier.
        # A failing barrier may leave the new name visible: propagate that error.
        data = json.dumps(value, ensure_ascii=False).encode()
        parent, name = self._parent(rel)
        temporary = name + "." + uuid.uuid4().hex + ".tmp"
        try:
            fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, "w+b") as f:
                f.write(data)
                f.flush()
                if self.json_owner is not None:
                    os.fchown(f.fileno(), *self.json_owner)
                os.fsync(f.fileno())
            created = True
            if once:
                try:
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                            follow_symlinks=False)
                except FileExistsError:
                    # A prior link may be visible after its publisher's fsync
                    # failed. Observing it is not a durability acknowledgement.
                    created = False
                    target = open_child(parent, name, directory=False)
                    try:
                        os.fsync(target)
                    finally:
                        os.close(target)
                os.unlink(temporary, dir_fd=parent)
            else:
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
            return created
        finally:
            try:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
            finally:
                os.close(parent)

    def write_json(self, rel, value):
        """Publish durable JSON; an error can occur after rename made it visible."""
        self._publish_json(rel, value, once=False)

    def publish_json_once(self, rel, value):
        """Publish once; an existing target is also synced before returning False."""
        return self._publish_json(rel, value, once=True)


def describe_file(file, cache=None, cache_lock=None):
    """Hash a stable regular fd and retain it for the matching response bytes."""
    before = stamp(os.fstat(file.fileno()))
    if not stat.S_ISREG(before[2]):
        raise ValueError("expected a regular artifact")
    digest = None
    if cache is not None:
        with cache_lock:
            digest = cache.get(before)
    if digest is None:
        file.seek(0)
        h = hashlib.sha256()
        remaining = before[3]
        while remaining:
            chunk = file.read(min(1 << 20, remaining))
            if not chunk:
                raise ArtifactChanged("artifact changed while hashing; retry")
            h.update(chunk)
            remaining -= len(chunk)
        digest = h.hexdigest()
        if stamp(os.fstat(file.fileno())) != before:
            raise ArtifactChanged("artifact changed while hashing; retry")
        if cache is not None:
            with cache_lock:
                if len(cache) >= 2048:
                    cache.clear()
                cache[before] = digest
    file.seek(0)
    file.artifact_stamp = before
    return {"size": before[3], "sha256": digest, "etag": digest[:16]}


def ensure_unchanged(file):
    if stamp(os.fstat(file.fileno())) != file.artifact_stamp:
        raise ArtifactChanged("artifact changed before download; retry")


def walk_directory(directory, prefix=".", depth=0):
    if depth > 64:
        raise ValueError("artifact directory nesting exceeds 64")
    before = stamp(os.fstat(directory))
    yield prefix, directory, os.fstat(directory)
    for name in sorted(os.listdir(directory)):
        child = open_child(directory, name)
        try:
            st = os.fstat(child)
            path = prefix + "/" + name
            if stat.S_ISDIR(st.st_mode):
                yield from walk_directory(child, path, depth + 1)
            else:
                yield path, child, st
                if stamp(os.fstat(child)) != stamp(st):
                    raise ArtifactChanged("artifact changed while archiving; retry")
        finally:
            os.close(child)
    if stamp(os.fstat(directory)) != before:
        raise ArtifactChanged("artifact directory changed; retry")


def _signature_update(digest, name, st):
    digest.update(json.dumps([name, stamp(st)], ensure_ascii=True).encode() + b"\n")


def directory_signature(directory):
    digest = hashlib.sha256()
    for name, _, st in walk_directory(directory):
        _signature_update(digest, name, st)
    return digest.hexdigest()


def archive_directory(directory, output):
    """Add only opened directories/regular files; never let tar reopen a path."""
    digest = hashlib.sha256()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, fd, st in walk_directory(directory):
            _signature_update(digest, name, st)
            info = tarfile.TarInfo(name)
            info.mode = stat.S_IMODE(st.st_mode)
            info.uid, info.gid, info.mtime = st.st_uid, st.st_gid, st.st_mtime
            if stat.S_ISDIR(st.st_mode):
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = st.st_size
                with os.fdopen(os.dup(fd), "rb") as source:
                    archive.addfile(info, source)
    output.flush()
    return digest.hexdigest()
