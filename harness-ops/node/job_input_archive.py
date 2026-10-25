"""Bounded, read-only archive preflight followed by controlled input writes."""
import gzip
import io
import os
import re
import tarfile
import zlib
from dataclasses import dataclass


POLICY = {
    "compressed_bytes": 512 << 20,
    "expanded_bytes": 512 << 20,
    "tar_stream_bytes": 544 << 20,
    "metadata_bytes": 1 << 20,
    "entries": 4096,
    "min_free_bytes": 64 << 20,
    "scope": "prepared_input_file_payload",
}
_CHUNK = 256 << 10
_DEPTH = 64


class Error(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.status = {"input_budget_exceeded": 413,
                       "invalid_input_archive": 400, "target_conflict": 409}[code]
        self.details = {} if details is None else details

    def as_dict(self):
        return {"code": self.code, "status": self.status,
                "message": str(self), "details": self.details}


def _invalid(message):
    return Error("invalid_input_archive", message)


def _limit(dimension, value, maximum):
    if value > maximum:
        raise Error("input_budget_exceeded", "input archive exceeds its preparation budget",
                    {"dimension": dimension, "limit": maximum, "observed": value})


def _path(value, directory=False, typed=False):
    def invalid():
        return Error("target_conflict" if typed else "invalid_input_archive",
                     "input target must be a safe relative path")

    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise invalid()
    # tar commonly spells its root './' and prefixes members with './'.
    while value.startswith("./"):
        value = value[2:]
    if directory:
        value = value.rstrip("/")
        if value in ("", "."):
            return ""
    parts = value.split("/")
    if (any(part in ("", ".", "..") for part in parts)
            or re.match(r"^[A-Za-z]:", parts[0])):
        raise invalid()
    _limit("depth", len(parts), _DEPTH)
    return "/".join(parts)


class _Namespace:
    def __init__(self, maximum, targets):
        self.maximum = maximum
        self.paths = {}
        self.explicit = set()
        for target in targets:
            self.add(_path(target, typed=True), "file", typed=True)

    def add(self, path, kind, typed=False):
        if (path, typed) in self.explicit:
            self.conflict(typed)
        self.explicit.add((path, typed))
        if not path:
            return
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            key = "/".join(parts[:index])
            expected = kind if index == len(parts) else "dir"
            old = self.paths.get(key)
            if old is not None:
                old_kind, old_typed = old
                if old_kind != expected or expected == "file":
                    self.conflict(typed or old_typed)
                # Retain typed ancestry for accurate conflicts with tar files.
                self.paths[key] = (expected, typed or old_typed)
            else:
                self.paths[key] = (expected, typed)
                _limit("entries", len(self.paths), self.maximum)

    @staticmethod
    def conflict(typed):
        raise Error("target_conflict" if typed else "invalid_input_archive",
                    "input targets overlap or have incompatible file types")


class _Decoded:
    """Forward-only gzip reader; tarfile's seeks consume bounded chunks too."""
    def __init__(self, source, maximum):
        self.source = source
        self.maximum = maximum
        self.count = 0

    def tell(self):
        return self.count

    def read(self, size):
        if size < 0:
            raise _invalid("unbounded archive read is not allowed")
        data = self.source.read(min(size, self.maximum - self.count + 1))
        self.count += len(data)
        _limit("tar_stream_bytes", self.count, self.maximum)
        return data

    def seek(self, offset, whence=0):
        if whence == 1:
            offset += self.count
        elif whence != 0:
            raise _invalid("unsupported archive seek")
        if offset < self.count:
            raise _invalid("archive requires a backward seek")
        _limit("tar_stream_bytes", offset, self.maximum)
        while self.count < offset:
            if not self.read(min(_CHUNK, offset - self.count)):
                raise _invalid("truncated input archive")
        return self.count


def _tar_info(reader, limits):
    # Standard TarInfo processes PAX/GNU recursively. Counting frombuf therefore
    # includes extension headers which TarFile.next does not expose as members.
    class CheckedInfo(tarfile.TarInfo):
        headers = 0

        @classmethod
        def frombuf(cls, buf, encoding, errors):
            try:
                info = super().frombuf(buf, encoding, errors)
            except (tarfile.InvalidHeaderError, tarfile.TruncatedHeaderError):
                # TarFile.next otherwise treats a bad non-first header as EOF.
                raise _invalid("invalid tar header") from None
            cls.headers += 1
            _limit("headers", cls.headers, limits["entries"])
            allowed = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE,
                       tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                       tarfile.GNUTYPE_LONGNAME)
            if info.type not in allowed:
                raise _invalid("only ordinary files and directories are supported")
            if info.size < 0:
                raise _invalid("negative tar member size")
            if info.type in (tarfile.XHDTYPE, tarfile.XGLTYPE,
                             tarfile.SOLARIS_XHDTYPE, tarfile.GNUTYPE_LONGNAME):
                # tarfile reads one extension payload at once before parsing it.
                _limit("metadata_bytes", info.size, limits["metadata_bytes"])
            _limit("tar_stream_bytes", reader.count + info._block(info.size),
                   limits["tar_stream_bytes"])
            return info

        def _apply_pax_info(self, headers, encoding, errors):
            if any(key.startswith("GNU.sparse.") for key in headers):
                raise _invalid("sparse input files are not supported")
            if "size" in headers and not re.fullmatch(r"[0-9]+", headers["size"]):
                raise _invalid("invalid PAX member size")
            try:
                return super()._apply_pax_info(headers, encoding, errors)
            except (ValueError, OverflowError) as error:
                raise _invalid("invalid PAX metadata") from error

        def _proc_pax(self, archive):
            try:
                return super()._proc_pax(archive)
            except Error:
                raise
            except (ValueError, OverflowError) as error:
                raise _invalid("invalid PAX metadata") from error

        def _proc_gnusparse_00(self, *args):
            raise _invalid("sparse input files are not supported")

        _proc_gnusparse_01 = _proc_gnusparse_00
        _proc_gnusparse_10 = _proc_gnusparse_00

    return CheckedInfo


def _walk(body, targets, limits, consume=None):
    namespace = _Namespace(limits["entries"], targets)
    payload = 0
    if not body:
        return payload, 0, len(namespace.paths)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as zipped:
            reader = _Decoded(zipped, limits["tar_stream_bytes"])
            with tarfile.open(fileobj=reader, mode="r:",
                              tarinfo=_tar_info(reader, limits)) as archive:
                while True:
                    info = archive.next()
                    if info is None:
                        break
                    if info.issparse() or not (info.isreg() or info.isdir()):
                        raise _invalid("only ordinary files and directories are supported")
                    if info.size < 0 or (info.isdir() and info.size):
                        raise _invalid("invalid tar member size")
                    path = _path(info.name, directory=info.isdir())
                    namespace.add(path, "dir" if info.isdir() else "file")
                    payload += info.size
                    _limit("expanded_bytes", payload, limits["expanded_bytes"])
                    _limit("tar_stream_bytes", reader.count + info._block(info.size),
                           limits["tar_stream_bytes"])
                    if info.isdir():
                        if consume is not None:
                            consume(path, info, None)
                        continue
                    with archive.extractfile(info) as source:
                        if consume is not None:
                            consume(path, info, source)
                        else:
                            remaining = info.size
                            while remaining:
                                data = source.read(min(_CHUNK, remaining))
                                if not data:
                                    raise _invalid("truncated tar member")
                                remaining -= len(data)
            # Tar EOF is not gzip EOF: account for all padding, trailing decoded
            # bytes and subsequent gzip members, and verify each gzip checksum.
            while reader.read(_CHUNK):
                pass
            return payload, reader.count, len(namespace.paths)
    except Error:
        raise
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, zlib.error,
            UnicodeError, RecursionError, OverflowError) as error:
        raise _invalid("invalid or truncated gzip/tar input") from error


@dataclass(frozen=True)
class Archive:
    body: bytes
    payload_bytes: int
    stream_bytes: int
    entries: int
    _targets: tuple
    _limits: dict

    def extract(self, store, jid, write):
        if not isinstance(jid, str) or not re.fullmatch(r"[0-9a-f]{12}", jid):
            raise ValueError("invalid job ID")
        prefix = jid + "/in"
        store.mkdir(prefix)
        created = {""}

        def directories(path):
            parts = path.split("/") if path else []
            for index in range(1, len(parts) + 1):
                directory = "/".join(parts[:index])
                if directory not in created:
                    store.mkdir(prefix + "/" + directory)
                    created.add(directory)

        def consume(path, info, source):
            if info.isdir():
                directories(path)
                return
            directories(path.rpartition("/")[0])
            with store.create_file(prefix + "/" + path) as destination:
                os.fchmod(destination.fileno(), 0o700 if info.mode & 0o100 else 0o600)
                remaining = info.size
                while remaining:
                    data = source.read(min(_CHUNK, remaining))
                    if not data:
                        raise _invalid("truncated tar member")
                    write(destination, data)
                    remaining -= len(data)

        _walk(self.body, self._targets, self._limits, consume)


def inspect(body, typed_targets=()):
    if not isinstance(body, bytes):
        raise _invalid("input archive must be immutable bytes")
    limits = dict(POLICY)
    _limit("compressed_bytes", len(body), limits["compressed_bytes"])
    targets = []
    for target in typed_targets:
        targets.append(target)
        _limit("entries", len(targets), limits["entries"])
    targets = tuple(targets)
    payload, stream, entries = _walk(body, targets, limits)
    return Archive(body, payload, stream, entries, targets, limits)
