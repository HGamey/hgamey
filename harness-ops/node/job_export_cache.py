"""Placement of derived directory exports; originals and builder locks stay put."""
import errno
import os
import re
import stat

from artifact_io import SafeRoot, ExportError, export_lock

CACHE_FILE = re.compile(r"\.outcache-[0-9a-f]{10}\.tar\.gz(?:\.json)?\Z")


def validate_root(path, jobs_root, history_sources):
    if path is None:
        return None
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        raise ValueError("export-cache-root must be an absolute directory")
    path = os.path.realpath(path)
    for original in [jobs_root, *(entry["jobs_root"] for entry in history_sources)]:
        original = os.path.realpath(original)
        if os.path.commonpath((path, original)) in (path, original):
            raise ValueError("export-cache-root must not overlap current or historical jobs")
    return path


class ExportCache:
    def __init__(self, original, jobs_root, root=None, history_sources=()):
        self.configured_root = validate_root(root, jobs_root, history_sources)
        self.original = original
        self.jobs_root = os.path.realpath(jobs_root)
        observed = os.fstat(original.fd)
        self.source = {"jobs_root": self.jobs_root, "device": observed.st_dev, "inode": observed.st_ino}
        self.namespace = f"jobs-{observed.st_dev:x}-{observed.st_ino:x}"
        self.store, self.error = original, None
        if self.configured_root is not None:
            self.store = None
            try:
                os.makedirs(self.configured_root, mode=0o700, exist_ok=True)
                self.store = SafeRoot(self.configured_root)
            except (OSError, ValueError) as error:
                self.error = ExportError(507 if getattr(error, "errno", None) in (errno.ENOSPC, errno.EDQUOT) else 503,
                                         "export_storage_unavailable", "external export cache could not be opened: " + str(error))

    def close(self):
        if self.configured_root is not None and self.store is not None:
            self.store.close()

    def public(self):
        external = self.configured_root is not None
        return {"scope": "directory_export", "mode": "external" if external else "legacy_job_directory",
                "configured_root": self.configured_root,
                "effective_root": os.path.join(self.configured_root, self.namespace) if external else self.jobs_root,
                "availability": "unavailable" if self.error else "available",
                "reason": str(self.error) if self.error else None, "legacy_entries_migrated": False}

    def target(self, jid):
        if self.error:
            raise self.error
        if self.configured_root is None:
            return self.store, jid, None
        prefix = self.namespace + "/" + jid
        try:
            for directory in (self.namespace, prefix):
                try:
                    fd = self.store.open_fd(directory, directory=True)
                except FileNotFoundError:
                    self.store.mkdir(directory)
                else:
                    os.close(fd)
        except (OSError, ValueError) as error:
            raise ExportError(507 if getattr(error, "errno", None) in (errno.ENOSPC, errno.EDQUOT) else 503,
                              "export_storage_unavailable", "external export cache namespace is unavailable: " + str(error)) from error
        return self.store, prefix, {**self.source, "job": jid}

    def inspect(self, jid, *, clear=False):
        if self.error:
            raise self.error
        if clear:
            with export_lock(self.original, jid):
                return self._inspect(jid, clear=True)
        return self._inspect(jid)

    def _inspect(self, jid, *, clear=False):
        placement = self.public()
        prefix = self.namespace + "/" + jid if self.configured_root is not None else jid
        result = {"job_id": jid, "scope": "directory_export_cache", "mode": placement["mode"],
                  "cache_path": os.path.join(placement["effective_root"], jid),
                  "byte_basis": "logical_file_sizes", "count": 0, "bytes": 0, "complete": True, "errors": []}
        if clear:
            result["deleted"] = {"count": 0, "bytes": 0}
        try:
            directory = self.store.open_fd(prefix, directory=True)
        except FileNotFoundError:
            return result  # Reading never creates an external job namespace.
        except (OSError, ValueError) as error:
            raise ExportError(503, "export_storage_unavailable", "export cache directory is unavailable: " + str(error)) from error
        try:
            def error(name, operation, problem):
                detail = {"name": name, "operation": operation, "error": str(problem)}
                if detail not in result["errors"]:
                    result["errors"].append(detail)
                result["complete"] = False

            def entries():
                files = []
                try:
                    names = os.listdir(directory)
                except OSError as problem:
                    error(None, "list", problem)
                    return files
                for name in names:
                    if not CACHE_FILE.fullmatch(name):
                        continue
                    try:
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode):
                            error(name, "inspect", "matching cache entry is not a regular file; retained")
                        else:
                            files.append((name, info.st_size))
                    except FileNotFoundError:
                        pass  # Another observer may have removed it after listing.
                    except OSError as problem:
                        error(name, "inspect", problem)
                return files

            files = entries()
            if clear:
                for name, size in files:
                    try:
                        os.unlink(name, dir_fd=directory)
                        result["deleted"]["count"] += 1
                        result["deleted"]["bytes"] += size
                    except FileNotFoundError:
                        pass
                    except OSError as problem:
                        error(name, "unlink", problem)
                files = entries()
            result.update(count=len(files), bytes=sum(size for _, size in files))
            return result
        finally:
            os.close(directory)
