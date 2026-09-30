"""Storage adapter. Pluggable. Default LocalDiskStorage.

All artifacts get a deterministic namespaced key. Hashes computed at
write time. No large media is committed to git; only metadata + refs.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class ArtifactRef:
    """Pointer to a stored artifact. Provider-agnostic."""
    key: str
    checksum: str
    size_bytes: int
    mime_type: str
    path: Optional[str] = None
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None

    def to_dict(self) -> dict:
        d = {
            "key": self.key,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
        }
        if self.path:
            d["path"] = self.path
        if self.duration_seconds is not None:
            d["duration_seconds"] = self.duration_seconds
        if self.width is not None:
            d["width"] = self.width
        if self.height is not None:
            d["height"] = self.height
        return d


class ChecksumMismatch(Exception):
    pass


class Storage:
    def put(self, *, key: str, data: bytes, mime_type: str) -> ArtifactRef:
        raise NotImplementedError

    def put_stream(self, *, key: str, stream: BinaryIO, mime_type: str) -> ArtifactRef:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def list(self, prefix: str) -> List[ArtifactRef]:
        raise NotImplementedError

    def path_for(self, key: str) -> Optional[str]:
        """Absolute filesystem path for the key, or None if not local."""
        return None


class LocalDiskStorage(Storage):
    """Filesystem-backed storage. Default for offline demo + tests."""

    def __init__(self, root: str = "./artifacts") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        # Keys are forward-slash namespaced. Reject path traversal.
        if ".." in key.split("/"):
            raise ValueError("invalid key: path traversal")
        if key.startswith("/"):
            raise ValueError("invalid key: absolute path")
        return self.root / key

    def path_for(self, key: str) -> Optional[str]:
        return str(self._resolve(key))

    def put(self, *, key: str, data: bytes, mime_type: str) -> ArtifactRef:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        checksum = hashlib.sha256(data).hexdigest()
        path.write_bytes(data)
        return ArtifactRef(
            key=key,
            checksum=checksum,
            size_bytes=len(data),
            mime_type=mime_type,
            path=str(path),
        )

    def put_stream(self, *, key: str, stream: BinaryIO, mime_type: str) -> ArtifactRef:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        size = 0
        with open(path, "wb") as f:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                h.update(chunk)
                f.write(chunk)
                size += len(chunk)
        return ArtifactRef(
            key=key,
            checksum=h.hexdigest(),
            size_bytes=size,
            mime_type=mime_type,
            path=str(path),
        )

    def get(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def delete(self, key: str) -> None:
        p = self._resolve(key)
        if p.exists():
            p.unlink()

    def list(self, prefix: str) -> List[ArtifactRef]:
        root = self._resolve(prefix) if prefix else self.root
        if not root.exists():
            return []
        out: List[ArtifactRef] = []
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = str(p.relative_to(self.root)).replace(os.sep, "/")
            data = p.read_bytes()
            out.append(
                ArtifactRef(
                    key=rel,
                    checksum=hashlib.sha256(data).hexdigest(),
                    size_bytes=len(data),
                    mime_type=_guess_mime(rel),
                    path=str(p),
                )
            )
        return out


_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".srt": "application/x-subrip",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _guess_mime(key: str) -> str:
    ext = os.path.splitext(key)[1].lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")