"""Small copies of pictures for previews: about 480 px on the long side, made once and kept on disk.

A copy is named by the original's content hash, so a job output and an attachment with the same bytes share
one, and a changed original gets a new one. The original download is never touched."""
import io
import os
import uuid

from artifact_io import SafeRoot, describe_file

SIDE = 480
FOLDER = "_thumbs"
KINDS = ((".jpg", "image/jpeg"), (".png", "image/png"))


def small(data_root, blob, info):
    """Open the small copy of `blob` (described by `info`), making it on first use, as (file, info, mime).

    None means show the original: Pillow is not installed, the bytes are not a PNG, JPEG or WebP picture,
    the picture is no larger than SIDE already, or the copy could not be written."""
    try:
        from PIL import Image
    except ImportError:
        return None
    root = SafeRoot(data_root)
    try:
        base = f"{FOLDER}/{info['sha256']}-{SIDE}"
        for extension, mime in KINDS:
            try:
                return _opened(root, base + extension, mime)
            except FileNotFoundError:
                pass
        made = _shrink(Image, blob)
        if made is None:
            return None
        data, (extension, mime) = made
        try:
            root.mkdir(FOLDER)
            temporary = f"{FOLDER}/.{uuid.uuid4().hex}.tmp"
            with root.create_file(temporary) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            root.replace(temporary, base + extension)
        except OSError:
            return None
        return _opened(root, base + extension, mime)
    finally:
        root.close()


def _opened(root, rel, mime):
    stream = root.open_file(rel)
    try:
        return stream, {"kind": "file", "path": rel.rsplit("/", 1)[-1], **describe_file(stream)}, mime
    except BaseException:
        stream.close()
        raise


def _shrink(Image, blob):
    blob.seek(0)
    try:
        with Image.open(blob) as picture:
            if picture.format not in ("PNG", "JPEG", "WEBP") or max(picture.size) <= SIDE:
                return None
            picture.thumbnail((SIDE, SIDE))
            clear = picture.mode in ("RGBA", "LA", "PA") or "transparency" in picture.info
            out = io.BytesIO()
            if clear:
                picture.convert("RGBA").save(out, "PNG", optimize=True)
            else:
                picture.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
            return out.getvalue(), KINDS[1] if clear else KINDS[0]
    except Exception:  # noqa: BLE001 — anything Pillow cannot read is shown as the original
        return None
    finally:
        blob.seek(0)
