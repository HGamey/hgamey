"""Bounded container identification for explicit research media reads, not decoding."""
import os
import struct


HEADER_LIMIT = 64 << 10
LIMITS = {
    "image": {"max_bytes": 16 << 20, "max_dimension": 8192, "max_pixels": 16777216},
    "video": {"max_bytes": 128 << 20, "max_dimension": None, "max_pixels": None},
}


class MediaError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.payload = status, {"code": code, "error": message}


def unsupported():
    raise MediaError(415, "media_unsupported", "media header is not a supported PNG/JPEG or MP4/WebM container within 64 KiB")


def _dimensions(width, height):
    limits = LIMITS["image"]
    if not width or not height:
        unsupported()
    if max(width, height) > limits["max_dimension"] or width * height > limits["max_pixels"]:
        raise MediaError(413, "media_dimensions_exceeded", "image exceeds 8192 pixels per side or 16777216 total pixels")
    return width, height


def _jpeg(data):
    position = 2
    while position < len(data):
        if data[position] != 255:
            unsupported()
        while position < len(data) and data[position] == 255:
            position += 1
        if position >= len(data):
            unsupported()
        marker = data[position]
        position += 1
        if marker in (0, 0xD8, 0xD9, 0xDA) or 0xD0 <= marker <= 0xD7:
            unsupported()
        if marker == 0x01:
            continue
        if position + 2 > len(data):
            unsupported()
        length = int.from_bytes(data[position:position + 2], "big")
        if length < 2 or position + length > len(data):
            unsupported()
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if length < 8:
                unsupported()
            height, width = struct.unpack(">HH", data[position + 3:position + 7])
            return _dimensions(width, height)
        position += length
    unsupported()


def _mp4(data):
    position = 0
    brands = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash", b"MSNV"}
    while position + 8 <= len(data):
        length, tag = struct.unpack(">I4s", data[position:position + 8])
        header = 8
        if length == 1:
            if position + 16 > len(data):
                unsupported()
            length = int.from_bytes(data[position + 8:position + 16], "big")
            header = 16
        if length < header or position + length > len(data):
            unsupported()
        if tag == b"ftyp":
            payload = data[position + header:position + length]
            if len(payload) < 8 or len(payload) % 4 or not ({payload[:4]} | {payload[i:i + 4] for i in range(8, len(payload), 4)}) & brands:
                unsupported()
            return
        if tag not in (b"free", b"skip", b"wide"):
            unsupported()
        position += length
    unsupported()


def _vint(data, position, identifier=False):
    if position >= len(data) or not data[position]:
        unsupported()
    first, mask, length = data[position], 0x80, 1
    while not first & mask:
        mask >>= 1
        length += 1
    if length > (4 if identifier else 8) or position + length > len(data):
        unsupported()
    value = int.from_bytes(data[position:position + length], "big")
    if not identifier:
        value &= (1 << (7 * length)) - 1
        if value == (1 << (7 * length)) - 1:
            unsupported()
    return value, position + length


def _webm(data):
    length, position = _vint(data, 4)
    end = position + length
    if end > len(data):
        unsupported()
    document = None
    while position < end:
        tag, position = _vint(data, position, True)
        length, position = _vint(data, position)
        if position + length > end:
            unsupported()
        if tag == 0x4282:
            if document is not None:
                unsupported()
            document = data[position:position + length]
        position += length
    if document != b"webm":
        unsupported()


def inspect_media(stream, media_kind):
    if media_kind not in LIMITS:
        raise ValueError("media_kind must be image or video")
    size = os.fstat(stream.fileno()).st_size
    if size > LIMITS[media_kind]["max_bytes"]:
        raise MediaError(413, "media_too_large", "media exceeds the explicit %s byte limit" % LIMITS[media_kind]["max_bytes"])
    stream.seek(0)
    data = stream.read(HEADER_LIMIT)
    stream.seek(0)
    width = height = None
    if media_kind == "image":
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            if len(data) < 33 or data[8:16] != b"\x00\x00\x00\rIHDR":
                unsupported()
            width, height = _dimensions(*struct.unpack(">II", data[16:24]))
            mime = "image/png"
        elif data.startswith(b"\xff\xd8"):
            width, height = _jpeg(data)
            mime = "image/jpeg"
        else:
            unsupported()
    elif data.startswith(b"\x1a\x45\xdf\xa3"):
        _webm(data)
        mime = "video/webm"
    else:
        _mp4(data)
        mime = "video/mp4"
    return {"media_kind": media_kind, "mime": mime, "width": width, "height": height}
