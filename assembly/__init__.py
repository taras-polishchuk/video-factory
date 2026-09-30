"""Assembly + QC. FFmpeg-based postprocessor."""

from assembly.ffmpeg_assembler import (
    QCResult,
    ffprobe_or_none,
    is_mock_asset,
    probe_duration_seconds,
    probe_has_audio,
    probe_video_dimensions,
    qc_outputs,
    stitch_scene_clips,
)

__all__ = [
    "QCResult",
    "ffprobe_or_none",
    "is_mock_asset",
    "probe_duration_seconds",
    "probe_has_audio",
    "probe_video_dimensions",
    "qc_outputs",
    "stitch_scene_clips",
]