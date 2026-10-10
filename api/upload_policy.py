"""Upload + download policy for tenant files (SECURITY S1a).

One place for the rules the asset upload routes and the file
download routes share:

* ``ALLOWED_TYPES``: content-type allow-list per asset ``kind``.
  A type outside the list is rejected with 415.  ``misc`` is the
  union of every concrete kind.  Active content (``text/html``,
  ``image/svg+xml``, ``application/javascript``, ...) is never on
  the list.
* ``sniff_ok``: the first bytes of the upload must match the
  declared binary type (PNG magic for ``image/png`` and so on).
  A declared type that does not match the bytes is rejected with
  415.  Text types have no reliable magic and are not sniffed;
  downloads are served with ``nosniff`` + ``attachment`` anyway.
* ``max_upload_bytes``: ``VIDEO_FACTORY_MAX_UPLOAD_MB`` (default
  200).  Enforced while streaming the upload to disk, so a large
  body never sits in memory.
* ``download_headers`` / ``safe_download_type``: every file
  response carries ``X-Content-Type-Options: nosniff`` and
  ``Content-Disposition: attachment``.  A stored mime type that is
  not on the allow-list (rows written before S1a) is served as
  ``application/octet-stream``.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, UploadFile

MAX_UPLOAD_MB_ENV = "VIDEO_FACTORY_MAX_UPLOAD_MB"
DEFAULT_MAX_UPLOAD_MB = 200

_IMAGE = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_VIDEO = frozenset({"video/mp4", "video/quicktime", "video/webm"})
_AUDIO = frozenset({
    "audio/mpeg", "audio/wav", "audio/x-wav", "audio/wave",
    "audio/mp4", "audio/x-m4a", "audio/aac", "audio/ogg",
})
_FONT = frozenset({
    "font/ttf", "font/otf", "font/woff", "font/woff2",
    "application/font-woff", "application/x-font-ttf",
    "application/x-font-otf", "application/vnd.ms-opentype",
})
_DOC = frozenset({
    "application/pdf", "text/plain", "text/markdown", "text/csv",
    "application/json",
})

ALLOWED_TYPES: dict[str, frozenset[str]] = {
    "logo": _IMAGE,
    "image": _IMAGE,
    "video": _VIDEO,
    "audio": _AUDIO,
    "voice": _AUDIO,
    "font": _FONT,
    "doc": _DOC,
    "misc": _IMAGE | _VIDEO | _AUDIO | _FONT | _DOC,
}

ALL_ALLOWED: frozenset[str] = frozenset().union(*ALLOWED_TYPES.values())

# Types we can verify by magic bytes.  Anything in ALL_ALLOWED and
# not here is a text type (no reliable signature).
_FONT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"OTTO", b"wOFF", b"wOF2")
_MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip")


def _is_mp3(h: bytes) -> bool:
    return h.startswith(b"ID3") or (
        len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0
    )


def _is_adts(h: bytes) -> bool:
    return h.startswith(b"ADIF") or (
        len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xF0) == 0xF0
    )


def _is_riff(h: bytes, form: bytes) -> bool:
    return h[:4] == b"RIFF" and h[8:12] == form


_SNIFFERS = {
    "image/png": lambda h: h.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda h: h.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda h: h[:6] in (b"GIF87a", b"GIF89a"),
    "image/webp": lambda h: _is_riff(h, b"WEBP"),
    "video/mp4": lambda h: h[4:8] in _MP4_BOXES,
    "video/quicktime": lambda h: h[4:8] in _MP4_BOXES,
    "video/webm": lambda h: h.startswith(b"\x1a\x45\xdf\xa3"),
    "audio/mp4": lambda h: h[4:8] in _MP4_BOXES,
    "audio/x-m4a": lambda h: h[4:8] in _MP4_BOXES,
    "audio/mpeg": _is_mp3,
    "audio/aac": _is_adts,
    "audio/wav": lambda h: _is_riff(h, b"WAVE"),
    "audio/x-wav": lambda h: _is_riff(h, b"WAVE"),
    "audio/wave": lambda h: _is_riff(h, b"WAVE"),
    "audio/ogg": lambda h: h.startswith(b"OggS"),
    "application/pdf": lambda h: h.startswith(b"%PDF-"),
}
for _t in _FONT:
    _SNIFFERS[_t] = lambda h: h[:4] in _FONT_MAGIC


def max_upload_bytes() -> int:
    raw = (os.environ.get(MAX_UPLOAD_MB_ENV) or "").strip()
    try:
        mb = float(raw) if raw else float(DEFAULT_MAX_UPLOAD_MB)
    except ValueError:
        mb = float(DEFAULT_MAX_UPLOAD_MB)
    if mb <= 0:
        mb = float(DEFAULT_MAX_UPLOAD_MB)
    return int(mb * 1024 * 1024)


def normalize_type(content_type: Optional[str]) -> str:
    """``"Image/PNG; charset=x"`` -> ``"image/png"``."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def check_declared_type(kind: str, content_type: Optional[str]) -> str:
    """Return the normalized type or raise 400 (kind) / 415 (type)."""
    allowed = ALLOWED_TYPES.get(kind)
    if allowed is None:
        raise HTTPException(
            400, f"unknown asset kind {kind!r}; allowed: "
                 f"{', '.join(sorted(ALLOWED_TYPES))}",
        )
    ct = normalize_type(content_type)
    if ct not in allowed:
        raise HTTPException(
            415, f"content type {ct or '(none)'!r} not allowed for "
                 f"kind {kind!r}",
        )
    return ct


def sniff_ok(content_type: str, head: bytes) -> bool:
    """True iff ``head`` matches the declared binary type.

    Text types (no sniffer) only have to be free of NUL bytes so
    a binary blob cannot be smuggled in as ``text/plain``.
    """
    sniffer = _SNIFFERS.get(content_type)
    if sniffer is None:
        return b"\x00" not in head
    return bool(sniffer(head))


async def save_upload(file: UploadFile, *, kind: str, target: Path,
                      ) -> tuple[str, int, str]:
    """Validate and stream ``file`` to ``target``.

    Returns ``(content_type, size_bytes, sha256_hex)``.  Raises
    400 (unknown kind), 415 (type not allowed / bytes do not match
    the type), 413 (over ``VIDEO_FACTORY_MAX_UPLOAD_MB``).  Nothing
    is left on disk when it raises.
    """
    ct = check_declared_type(kind, file.content_type)
    cap = max_upload_bytes()
    head = await file.read(8192)
    if not sniff_ok(ct, head):
        raise HTTPException(
            415, f"file content does not match declared type {ct!r}",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    try:
        with open(target, "wb") as out:
            chunk = head
            while chunk:
                size += len(chunk)
                if size > cap:
                    raise HTTPException(
                        413, f"asset exceeds {cap // (1024 * 1024)}MB cap "
                             f"({MAX_UPLOAD_MB_ENV})",
                    )
                h.update(chunk)
                out.write(chunk)
                chunk = await file.read(1024 * 1024)
    except BaseException:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        raise
    return ct, size, h.hexdigest()


def safe_download_type(mime_type: Optional[str]) -> str:
    ct = normalize_type(mime_type)
    return ct if ct in ALL_ALLOWED else "application/octet-stream"


def download_headers() -> dict[str, str]:
    return {"X-Content-Type-Options": "nosniff"}


__all__ = [
    "ALLOWED_TYPES",
    "ALL_ALLOWED",
    "DEFAULT_MAX_UPLOAD_MB",
    "MAX_UPLOAD_MB_ENV",
    "check_declared_type",
    "download_headers",
    "max_upload_bytes",
    "normalize_type",
    "safe_download_type",
    "save_upload",
    "sniff_ok",
]
