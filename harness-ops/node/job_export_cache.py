"""Placement of derived directory exports; originals and builder locks stay put."""
import errno
import os

from artifact_io import SafeRoot, ExportError


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
