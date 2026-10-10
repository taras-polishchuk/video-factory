"""Regression pin: synthetic placeholder clips must use a low-memory x264
preset. With the default preset each 1080x1920 clip peaks at ~680 MB RSS,
which OOM-killed 4 GB worker cgroups when the suite ran in parallel and
would starve a 4 GB production host running concurrent jobs.
"""

from __future__ import annotations

from core import edit_plan


def test_synthetic_clip_uses_ultrafast_preset(monkeypatch, tmp_path):
    captured = {}

    def fake_check_call(cmd, **kwargs):
        captured["cmd"] = cmd
        return 0

    monkeypatch.setattr(edit_plan.subprocess, "check_call", fake_check_call)
    ok = edit_plan._ffmpeg_synthetic_clip(
        out=str(tmp_path / "c.mp4"), duration=1.0, width=1080, height=1920,
        color="blue", label="clip #1",
    )
    assert ok is True
    cmd = captured["cmd"]
    assert "-preset" in cmd
    assert cmd[cmd.index("-preset") + 1] == "ultrafast"
