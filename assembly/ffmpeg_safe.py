"""Helpers that keep untrusted text out of ffmpeg syntax (SECURITY S1c).

Rules:

* Filtergraphs hold constants only. User text is written to a job-owned
  file and drawn with ``drawtext=textfile=<name>:expansion=none``; the
  file name is a portable basename resolved against the ffmpeg cwd.
* Concat lists hold portable relative basenames we generate
  (``clip_0001.mp4``). They live next to the list, so the concat demuxer
  runs in its default safe mode (no ``-safe 0``).
* Media bytes from a provider are untrusted. Local copies get a suffix
  from a fixed allow-list (a ``.m3u8`` name would let ffmpeg's HLS
  demuxer pull any local ``.mp4`` into the output), and the inputs are
  opened with a file-only protocol and container allow-list.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

# ffmpeg concat "safe" rule: portable charset, relative, no leading dot.
PORTABLE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

CLIP_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm"})
AUDIO_SUFFIXES = frozenset({".m4a", ".aac", ".mp3", ".wav", ".ogg", ".opus", ".flac"})

# Input options for untrusted media: local files only, container demuxers
# only (no hls/concat-in-concat/image2/etc. via content sniffing).
CONCAT_INPUT_GUARD = ("-protocol_whitelist", "file",
                      "-format_whitelist", "concat,mov,matroska")
AUDIO_INPUT_GUARD = ("-protocol_whitelist", "file",
                     "-format_whitelist", "mov,matroska,mp3,wav,ogg,flac,aac")
PROBE_GUARD = ("-protocol_whitelist", "file")


def require_portable_name(name: str) -> str:
    if not isinstance(name, str) or not PORTABLE_NAME.match(name):
        raise ValueError("unsafe file name for ffmpeg")
    return name


def media_suffix(name: str, allowed: frozenset, default: str) -> str:
    """Lower-case extension of ``name`` if allow-listed, else ``default``."""
    suffix = Path(str(name)).suffix.lower()
    return suffix if suffix in allowed else default


def write_concat_list(path: Path, names: Iterable[str]) -> Path:
    """Write a concat-demuxer list of portable basenames next to ``path``."""
    names = [require_portable_name(n) for n in names]
    if not names:
        raise ValueError("empty concat list")
    path = Path(path)
    path.write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
    return path


def write_text_file(directory: Path, text: str) -> str:
    """Write ``text`` (UTF-8) to a new private file in ``directory``.

    Returns the basename, suitable for ``textfile=`` with cwd=directory.
    """
    fd, full = tempfile.mkstemp(prefix="label_", suffix=".txt", dir=str(directory))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return require_portable_name(os.path.basename(full))


def drawtext_from_file(textfile_name: str, *, fontcolor: str = "white",
                       fontsize: int = 64) -> str:
    """Centered drawtext that reads its text from ``textfile_name``.

    ``expansion=none`` disables ``%{...}`` sequences in the file content.
    """
    require_portable_name(textfile_name)
    if not re.fullmatch(r"[A-Za-z]+|#[0-9A-Fa-f]{6}", fontcolor):
        raise ValueError("unsafe fontcolor")
    return (
        f"drawtext=textfile={textfile_name}:expansion=none"
        f":fontcolor={fontcolor}:fontsize={int(fontsize)}"
        ":x=(w-text_w)/2:y=(h-text_h)/2"
    )
