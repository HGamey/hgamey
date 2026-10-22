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
    def __init__(self, path):
        self.fd = None
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
        return json.loads(self.read_bytes(rel, maximum))

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

    def write_json(self, rel, value):
        temporary = rel + "." + uuid.uuid4().hex + ".tmp"
        try:
            with self.create_file(temporary) as f:
                f.write(json.dumps(value, ensure_ascii=False).encode())
            self.replace(temporary, rel)
        finally:
            try:
                self.unlink(temporary)
            except FileNotFoundError:
                pass


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
