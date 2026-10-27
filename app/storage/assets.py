"""Immutable content-addressed files; filenames never select storage paths."""
import hashlib
import io
import ntpath
import os
from pathlib import Path, PurePosixPath
from uuid import uuid4

from PIL import Image, UnidentifiedImageError


def safe_path(root: Path, key: str) -> Path:
    root = root.resolve()
    if not isinstance(key, str) or not key or "\\" in key or ntpath.splitdrive(key)[0] or key.startswith("/") or any(part in {"..", ".", ""} for part in key.split("/")):
        raise ValueError("Unsafe asset key")
    candidate = root.joinpath(*PurePosixPath(key).parts)
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ValueError("Asset symlinks are forbidden")
        current = current.parent
    resolved = candidate.resolve()
    if root not in resolved.parents:
        raise ValueError("Asset escapes data root")
    return resolved


def store_bytes(root: Path, payload: bytes, *, max_bytes=50*1024*1024, require_image=True):
    if not payload or len(payload) > max_bytes:
        raise ValueError("Empty file or upload size limit exceeded")
    mime = "application/octet-stream"
    width = height = None
    thumbnail = None
    if require_image:
        try:
            with Image.open(io.BytesIO(payload), formats=["JPEG", "PNG", "WEBP", "GIF"]) as image:
                if image.width * image.height > 80_000_000:
                    raise ValueError("Image dimensions exceed safe limit")
                image.verify()
            with Image.open(io.BytesIO(payload), formats=["JPEG", "PNG", "WEBP", "GIF"]) as image:
                mime = Image.MIME.get(image.format, "application/octet-stream")
                width, height = image.size
                image.thumbnail((640,640))
                image = image.convert("RGB")
                result = io.BytesIO(); image.save(result, "JPEG", quality=82)
                thumbnail = result.getvalue()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ValueError("File content is not a safe image") from error
    digest = hashlib.sha256(payload).hexdigest()
    key = f"assets/{digest[:2]}/{digest}"
    _install(root, key, payload)
    thumbnail_key = f"thumbnails/{digest[:2]}/{digest}.jpg" if thumbnail else None
    if thumbnail_key:
        _install(root, thumbnail_key, thumbnail)
    return {"id":str(uuid4()),"storage_key":key,"sha256":digest,"mime_type":mime,"size":len(payload),"width":width,"height":height,"state":"available","thumbnail_key":thumbnail_key}


def _install(root, key, payload):
    path = safe_path(root,key)
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(payload).digest():
            raise ValueError("Immutable asset hash mismatch")
        return
    temporary = path.with_name(path.name+"."+uuid4().hex+".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
