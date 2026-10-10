"""EditPlan: explicit, deterministic edit stage.

An EditPlan describes clip order, in/out points, crop/aspect, transitions,
voice/music refs, captions, and overlays. The assembler (FFmpeg) consumes
the plan and produces the final files. In mock mode the plan uses
short FFmpeg-generated synthetic clips; live mode would mix real
provider outputs.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from assembly.ffmpeg_safe import (
    drawtext_from_file,
    write_concat_list,
    write_text_file,
)
from core.text_safety import TOPIC_MAX_CHARS, clean_user_text

# Upper bound for a burned-in label: topic cap + " #NNNN" clip suffix.
MAX_LABEL_CHARS = TOPIC_MAX_CHARS + 8
_SAFE_COLOR = re.compile(r"^(#[0-9A-Fa-f]{6}|[A-Za-z]{1,32})$")


@dataclass
class ClipSpec:
    clip_id: str
    source_kind: str  # "synthetic" | "scene" | "voiceover"
    duration_seconds: float
    label: str
    seed_color: str = "#1f2937"  # used for synthetic clips


@dataclass
class EditPlan:
    plan_id: str
    clips: List[ClipSpec] = field(default_factory=list)
    aspect_ratio: str = "9:16"
    caption_lines: List[dict] = field(default_factory=list)
    overlay_text: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "aspect_ratio": self.aspect_ratio,
            "clips": [c.__dict__ for c in self.clips],
            "caption_lines": self.caption_lines,
            "overlay_text": self.overlay_text,
        }

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


def build_plan(*, duration_seconds: int, n_clips: int = 3,
               aspect_ratio: str = "9:16", topic: str = "",
               captions: Optional[list] = None) -> EditPlan:
    """Default mock plan: split duration across N synthetic clips.

    Real provider would slot scene clips here.
    """
    per = max(1.0, duration_seconds / max(1, n_clips))
    # SECURITY S1c: the label is drawn from a file, never inlined in the
    # filtergraph; still strip control chars and cap it for legacy rows
    # and non-API callers.
    label_topic = clean_user_text(topic or "")[:TOPIC_MAX_CHARS] or "clip"
    clips = []
    palette = ["#0f172a", "#1e3a8a", "#0d9488", "#7c2d12", "#581c87"]
    for i in range(n_clips):
        clips.append(ClipSpec(
            clip_id=f"clip_{i+1}",
            source_kind="synthetic",
            duration_seconds=per,
            label=f"{label_topic} #{i+1}",
            seed_color=palette[i % len(palette)],
        ))
    return EditPlan(
        plan_id="plan_" + hashlib.sha256(f"{topic}|{duration_seconds}|{n_clips}".encode()).hexdigest()[:12],
        clips=clips,
        aspect_ratio=aspect_ratio,
        caption_lines=list(captions or []),
        overlay_text=topic or None,
    )


def render_plan_to_storage(*, plan: EditPlan, storage, output_key: str) -> Optional[dict]:
    """Execute the plan deterministically using FFmpeg.

    Returns a dict with output_ref + qc summary, or None if FFmpeg
    unavailable or fails.
    """
    if not plan.clips:
        return None

    try:
        from control_plane.storage import LocalDiskStorage  # noqa
    except Exception:
        pass

    width, height = _dimensions_for(plan.aspect_ratio)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        clip_files: List[str] = []
        clip_names: List[str] = []
        for idx, clip in enumerate(plan.clips, start=1):
            # Names are ours (portable, relative): the concat list never
            # carries plan- or user-derived text.
            cf = tmpdir / f"seg_{idx:04d}.mp4"
            ok = _ffmpeg_synthetic_clip(
                out=str(cf),
                duration=clip.duration_seconds,
                width=width,
                height=height,
                color=clip.seed_color,
                label=clip.label,
            )
            if not ok:
                return None
            clip_files.append(str(cf))
            clip_names.append(cf.name)
        concat_file = write_concat_list(tmpdir / "concat.txt", clip_names)
        final = tmpdir / "final.mp4"
        ok = _ffmpeg_concat(
            concat=str(concat_file),
            out=str(final),
            width=width,
            height=height,
            duration=int(round(sum(c.duration_seconds for c in plan.clips))),
        )
        if not ok:
            return None
        data = final.read_bytes()
        ref = storage.put(key=output_key, data=data, mime_type="video/mp4")
        return {
            "output_ref": ref.to_dict(),
            "duration_seconds": sum(c.duration_seconds for c in plan.clips),
            "clip_count": len(plan.clips),
            "plan_fingerprint": plan.fingerprint(),
        }


def _dimensions_for(aspect: str) -> tuple:
    return {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
    }.get(aspect, (1080, 1920))


def _ffmpeg_synthetic_clip(*, out: str, duration: float, width: int,
                           height: int, color: str, label: str) -> bool:
    """Render one colour card with ``label`` centered.

    SECURITY S1c: ``label`` is user-derived. It goes to a private text file
    next to ``out`` and is drawn with ``textfile=<basename>`` +
    ``expansion=none`` (ffmpeg runs with cwd = that directory), so the
    filtergraph only ever holds constants. The lavfi source strings are
    built from validated numbers and a colour token only.
    """
    if not (isinstance(duration, (int, float)) and math.isfinite(duration)
            and duration > 0):
        return False
    if not (isinstance(color, str) and _SAFE_COLOR.match(color)):
        return False
    width, height = int(width), int(height)
    out_path = Path(out).resolve()
    workdir = out_path.parent
    text = clean_user_text(str(label))[:MAX_LABEL_CHARS]
    try:
        label_file = write_text_file(workdir, text)
    except OSError:
        return False
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:d={duration}:r=30",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-vf", drawtext_from_file(label_file),
        # Synthetic placeholder clips: `ultrafast` cuts x264 peak RSS from
        # ~680 MB to ~225 MB per 1080x1920 clip (measured 2026-10-09) and
        # runs ~3x faster. Quality is irrelevant for colour-card placeholders.
        "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(out_path),
    ]
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60, cwd=str(workdir))
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    finally:
        (workdir / label_file).unlink(missing_ok=True)


def _ffmpeg_concat(*, concat: str, out: str, width: int, height: int,
                   duration: int) -> bool:
    cmd = [
        # Safe mode (default): the list only holds relative portable names.
        "ffmpeg", "-y", "-f", "concat", "-i", concat,
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-an", out,
    ]
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False