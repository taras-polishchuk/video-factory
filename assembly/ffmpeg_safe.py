"""Helpers that keep untrusted text out of ffmpeg syntax (SECURITY S1c).

Rules:

* Filtergraphs hold constants only. User text is written to a job-owned
  file and drawn with ``drawtext=textfile=<name>:expansion=none``; the
  file name is a portable basename resolved against the ffmpeg cwd.
* Concat lists hold portable relative basenames we generate
  (``clip_0001.mp4``). They live next to the list, so the concat demuxer
  runs in its default safe mode (no ``-safe 0``).
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

# ffmpeg concat "safe" rule: portable charset, relative, no leading dot.
PORTABLE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_SAFE_SUFFIX = re.compile(r"^\.[a-z0-9]{1,8}$")


def require_portable_name(name: str) -> str:
    if not isinstance(name, str) or not PORTABLE_NAME.match(name):
        raise ValueError("unsafe file name for ffmpeg")
    return name


def safe_media_suffix(name: str) -> str:
    """Lower-case extension of ``name`` if it is short alnum, else ''."""
    suffix = Path(name).suffix.lower()
    return suffix if _SAFE_SUFFIX.match(suffix) else ""


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
