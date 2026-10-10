"""SECURITY S1c: user text must never reach an ffmpeg filtergraph or concat list.

Defect (main 4167ba0 / e60ac99): ``core/edit_plan.py`` put the job topic
unescaped into ``drawtext=text='<topic> #n'``. A topic such as
``a',drawtext=textfile=/etc/hostname,drawtext=text='b`` chained an extra
filter that drew a host file into the customer's video. Concat lists were
built with ``file '<path>'`` from storage key basenames, unescaped, under
``-safe 0``.

These tests pin the fix:

* the label goes to a job-owned text file read with ``textfile=`` +
  ``expansion=none``; the filtergraph only holds constants;
* concat lists only hold portable relative names we generate; ``-safe 0``
  is gone;
* the API validates the topic (1..200 chars after control-char strip) and
  never reflects it in the error.

Real-ffmpeg tests skip when ffmpeg is not on PATH.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from core import edit_plan

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")

# Card topics + a few neighbours.
HOSTILE_TOPICS = {
    "apostrophe": "Don't panic",
    "textfile-option": "a':textfile=/etc/hostname:x='",
    "filter-chain": "a',drawbox=c=red:t=fill,drawtext=text='b",
    "chain-textfile": "a',drawtext=textfile=/etc/hostname:x=0:y=0,drawtext=text='b",
    "percent-expansion": "100%{pts}",
    "backslash-quote": "x\\\\'y",
    "single-backslash": "x\\'y",
    "semicolon-graph": "a';[in]nullsink;[x]drawtext=text='b",
    "long-2000": "L" * 2000,
    "unicode-emoji": "Café ☕ 🚀 ünïcödé — 日本語",
}

PORTABLE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _split_unescaped(s: str, sep: str) -> list[str]:
    """Split on ``sep`` outside of '...' quotes and not after a backslash."""
    out, cur, quoted, i = [], [], False, 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2])
            i += 2
            continue
        if ch == "'":
            quoted = not quoted
        if ch == sep and not quoted:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def _vf_of(cmd: list[str]) -> str:
    return cmd[cmd.index("-vf") + 1]


# ── 1. filtergraph never contains user text ─────────────────────────────


@pytest.mark.parametrize("topic", list(HOSTILE_TOPICS.values()), ids=list(HOSTILE_TOPICS))
def test_drawtext_filter_carries_no_user_text(monkeypatch, tmp_path, topic):
    label = edit_plan.build_plan(duration_seconds=6, n_clips=2, topic=topic).clips[0].label
    seen: dict = {}

    def fake_check_call(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["cwd"] = kwargs.get("cwd")
        vf = _vf_of(cmd)
        opts = dict(o.split("=", 1) for o in _split_unescaped(vf.split("=", 1)[1], ":"))
        seen["opts"] = opts
        textfile = Path(kwargs["cwd"]) / opts["textfile"]
        seen["file_text"] = textfile.read_text(encoding="utf-8")
        return 0

    monkeypatch.setattr(edit_plan.subprocess, "check_call", fake_check_call)
    ok = edit_plan._ffmpeg_synthetic_clip(
        out=str(tmp_path / "clip_1.mp4"), duration=1.0, width=320, height=240,
        color="#0f172a", label=label,
    )
    assert ok is True
    vf = _vf_of(seen["cmd"])
    # Exactly one filter, and it is drawtext.
    assert len(_split_unescaped(vf, ",")) == 1, vf
    assert len(_split_unescaped(vf, ";")) == 1, vf
    assert vf.startswith("drawtext="), vf
    opts = seen["opts"]
    assert "text" not in opts, "user text must not be inlined via text="
    assert opts.get("expansion") == "none", "expansion must be off for user text"
    assert PORTABLE.match(opts["textfile"]), opts["textfile"]
    assert vf.count("textfile=") == 1
    # No argv element carries any piece of the topic.
    probe = topic[:40]
    for arg in seen["cmd"]:
        assert probe not in arg, f"topic leaked into argv: {arg[:120]!r}"
    # The file holds the label verbatim (minus control chars / length cap).
    assert seen["file_text"].endswith("#1")
    assert seen["file_text"].startswith(topic[:50])
    assert len(seen["file_text"]) <= edit_plan.MAX_LABEL_CHARS
    # Label file is cleaned up after the call.
    assert not (Path(seen["cwd"]) / opts["textfile"]).exists()


def test_label_sanitizer_strips_control_chars_and_caps_length():
    plan = edit_plan.build_plan(
        duration_seconds=4, n_clips=2,
        topic="Hello\x00\x1b[31m\nWorld\r\t\x7f\u0085!" + "x" * 5000,
    )
    label = plan.clips[1].label
    assert label.endswith(" #2")
    assert not any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in label)
    assert label.startswith("Hello[31m World")
    assert len(label) <= edit_plan.MAX_LABEL_CHARS


def test_synthetic_clip_refuses_hostile_color_and_duration(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(edit_plan.subprocess, "check_call",
                        lambda cmd, **kw: calls.append(cmd) or 0)
    for color, duration in [("red:s=1x1,nullsink", 1.0), ("#0f172a", float("nan")),
                            ("#0f172a", -1.0), ("#0f172a", "1:d=9999")]:
        ok = edit_plan._ffmpeg_synthetic_clip(
            out=str(tmp_path / "c.mp4"), duration=duration, width=320,
            height=240, color=color, label="x",
        )
        assert ok is False, (color, duration)
    assert calls == []


# ── 2. concat lists ──────────────────────────────────────────────────────


def _fake_ffmpeg_writing_output(seen: list):
    def fake(cmd, **kwargs):
        rec = {"cmd": list(cmd), "kwargs": kwargs}
        if "concat" in cmd:
            listing = Path(cmd[cmd.index("-i") + 1])
            rec["concat_text"] = listing.read_text()
            rec["listed_bytes"] = {
                f.name: f.read_bytes() for f in listing.parent.iterdir() if f != listing
            }
        seen.append(rec)
        Path(cmd[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        return 0
    return fake


def test_edit_plan_concat_list_is_relative_and_safe(monkeypatch, tmp_path):
    from control_plane.storage import LocalDiskStorage
    seen: list = []
    monkeypatch.setattr(edit_plan.subprocess, "check_call", _fake_ffmpeg_writing_output(seen))
    plan = edit_plan.build_plan(duration_seconds=6, n_clips=3,
                                topic=HOSTILE_TOPICS["chain-textfile"])
    res = edit_plan.render_plan_to_storage(
        plan=plan, storage=LocalDiskStorage(str(tmp_path / "st")), output_key="o/final.mp4",
    )
    assert res is not None
    concat = [r for r in seen if "concat" in r["cmd"]]
    assert len(concat) == 1
    cmd = concat[0]["cmd"]
    assert "-safe" not in cmd, "concat must run in ffmpeg safe mode (no -safe 0)"
    lines = concat[0]["concat_text"].splitlines()
    assert len(lines) == 3
    for line in lines:
        m = re.fullmatch(r"file '([^'/\\]+)'", line)
        assert m and PORTABLE.match(m.group(1)), line


def test_stitch_concat_list_ignores_hostile_ref_names(monkeypatch, tmp_path):
    from assembly import ffmpeg_assembler as fa
    from control_plane.storage import LocalDiskStorage
    storage = LocalDiskStorage(str(tmp_path / "st"))
    refs = ["clips/it's.mp4",
            "clips/x'\nfile 'evil.mp4",
            "a/same.mp4", "b/same.mp4",
            "clips/-i.mp4"]
    for i, ref in enumerate(refs):
        storage.put(key=ref, data=b"\x00\x00\x00\x18ftypmp42" + bytes([i]) * 32,
                    mime_type="video/mp4")
    storage.put(key="audio/it's\n.m4a", data=b"\x00\x00\x00\x18ftypM4A ",
                mime_type="audio/mp4")
    seen: list = []
    monkeypatch.setattr(fa.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(fa.subprocess, "check_call", _fake_ffmpeg_writing_output(seen))
    monkeypatch.setattr(fa, "qc_outputs", lambda **kw: fa.QCResult(True, {}, []))
    res = fa.stitch_scene_clips(storage=storage, clip_refs=refs,
                                output_ref="assembled/x/stitched.mp4",
                                target_duration_seconds=5,
                                audio_ref="audio/it's\n.m4a")
    assert res is not None and res.get("output_ref")
    (call,) = seen
    assert "-safe" not in call["cmd"]
    lines = call["concat_text"].splitlines()
    assert len(lines) == len(refs), lines
    names = []
    for line in lines:
        m = re.fullmatch(r"file '([^'/\\]+)'", line)
        assert m and PORTABLE.match(m.group(1)), line
        names.append(m.group(1))
    # One distinct file per ref, carrying that ref's bytes (no basename collision).
    assert len(set(names)) == len(refs)
    for name, ref in zip(names, refs):
        assert call["listed_bytes"][name] == storage.get(ref)
    for arg in call["cmd"]:
        assert "it's" not in arg and "evil" not in arg and "\n" not in arg


def test_write_concat_list_rejects_unsafe_names(tmp_path):
    from assembly.ffmpeg_safe import write_concat_list
    for bad in ["it's.mp4", "a\nfile 'b.mp4'", "/etc/hostname", "../x.mp4",
                ".hidden.mp4", "http:x", "a b.mp4", ""]:
        with pytest.raises(ValueError):
            write_concat_list(tmp_path / "c.txt", [bad])
    p = write_concat_list(tmp_path / "c.txt", ["clip_0001.mp4", "clip_0002.mp4"])
    assert p.read_text() == "file 'clip_0001.mp4'\nfile 'clip_0002.mp4'\n"


# ── 3. real ffmpeg ───────────────────────────────────────────────────────


def _frame(mp4: str, at: str = "0") -> str:
    raw = subprocess.check_output(
        ["ffmpeg", "-v", "error", "-ss", at, "-i", mp4, "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        timeout=60,
    )
    assert raw, "no frame decoded"
    return hashlib.sha256(raw).hexdigest()


def _clip(tmp: Path, name: str, label: str, duration: float = 1.0) -> str:
    out = str(tmp / f"{name}.mp4")
    ok = edit_plan._ffmpeg_synthetic_clip(
        out=out, duration=duration, width=320, height=240, color="#0f172a", label=label,
    )
    assert ok is True, f"ffmpeg failed for label {label[:60]!r}"
    return out


@needs_ffmpeg
@pytest.mark.parametrize("topic", list(HOSTILE_TOPICS.values()), ids=list(HOSTILE_TOPICS))
def test_real_ffmpeg_renders_every_hostile_topic(tmp_path, topic):
    label = edit_plan.build_plan(duration_seconds=2, n_clips=1, topic=topic).clips[0].label
    out = _clip(tmp_path, "c", label)
    from assembly.ffmpeg_assembler import probe_duration_seconds
    d = probe_duration_seconds(out)
    assert d is not None and 0.5 <= d <= 2.0


@needs_ffmpeg
def test_real_ffmpeg_injected_textfile_is_never_read(tmp_path):
    """Same hostile topic, canary content changes between renders.

    On main the frames differ (canary text drawn into the video) and the
    render fails once the canary is deleted. Fixed: frames identical,
    render succeeds without the canary.
    """
    canary = tmp_path / "canary.txt"
    topic = (f"a',drawtext=textfile={canary}:fontsize=120:fontcolor=red:"
             f"x=10:y=10,drawtext=text='b")
    label = edit_plan.build_plan(duration_seconds=2, n_clips=1, topic=topic).clips[0].label
    canary.write_text("AAAA")
    a = _frame(_clip(tmp_path, "a", label))
    canary.write_text("BBBB")
    b = _frame(_clip(tmp_path, "b", label))
    assert a == b, "host file content reached the rendered frame"
    canary.unlink()
    _clip(tmp_path, "c", label)  # must not depend on the file existing


def _raw_filter_frames(monkeypatch, tmp_path, label, duration=1.0):
    """Run the exact -vf our code builds through real ffmpeg, losslessly.

    Encoded mp4 frames differ between I and P frames even for a static
    picture, so per-frame equality is checked on raw filter output.
    """
    real = subprocess.check_call
    frames: list = []

    def spy(cmd, **kwargs):
        src = cmd[cmd.index("-i") + 1]
        vf = cmd[cmd.index("-vf") + 1]
        raw = subprocess.check_output(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", src, "-vf", vf,
             "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            cwd=kwargs.get("cwd"), timeout=60,
        )
        size = 320 * 240
        frames.extend(raw[i:i + size] for i in range(0, len(raw), size))
        return real(cmd, **kwargs)

    monkeypatch.setattr(edit_plan.subprocess, "check_call", spy)
    _clip(tmp_path, "raw", label, duration=duration)
    monkeypatch.setattr(edit_plan.subprocess, "check_call", real)
    assert len(frames) >= 10
    return frames


@needs_ffmpeg
def test_real_ffmpeg_percent_sequences_are_not_expanded(monkeypatch, tmp_path):
    frames = _raw_filter_frames(monkeypatch, tmp_path, "100%{pts} #1", duration=1.0)
    assert len(set(frames)) == 1, "%{pts} was expanded: text changes per frame"


@needs_ffmpeg
@pytest.mark.parametrize("a,b", [
    ("Don't panic", "Dont panic"),
    ("x\\\\'y", "x'y"),
    ("100%{pts}", "100"),
    ("A", "B"),
])
def test_real_ffmpeg_draws_label_verbatim(tmp_path, a, b):
    """Labels that differ only in special chars must render differently.

    Proves the label is actually drawn and that quotes / backslashes /
    percent sequences are not swallowed as escapes.
    """
    assert _frame(_clip(tmp_path, "a", a)) != _frame(_clip(tmp_path, "b", b))


@needs_ffmpeg
def test_real_ffmpeg_full_plan_render_with_injection_topic(tmp_path):
    from assembly.ffmpeg_assembler import probe_duration_seconds
    from control_plane.storage import LocalDiskStorage
    storage = LocalDiskStorage(str(tmp_path / "st"))
    plan = edit_plan.build_plan(duration_seconds=2, n_clips=2, aspect_ratio="1:1",
                                topic=HOSTILE_TOPICS["chain-textfile"])
    res = edit_plan.render_plan_to_storage(plan=plan, storage=storage, output_key="o/f.mp4")
    assert res is not None, "render failed"
    d = probe_duration_seconds(storage.path_for("o/f.mp4"))
    assert d is not None and 1.5 <= d <= 2.6


@needs_ffmpeg
def test_real_ffmpeg_stitch_with_hostile_ref_names(tmp_path):
    from assembly import ffmpeg_assembler as fa
    from control_plane.storage import LocalDiskStorage
    storage = LocalDiskStorage(str(tmp_path / "st"))
    src = _clip(tmp_path, "src", "clip", duration=1.0)
    refs = ["clips/it's.mp4", "clips/x'\nfile 'evil.mp4"]
    for ref in refs:
        storage.put(key=ref, data=Path(src).read_bytes(), mime_type="video/mp4")
    res = fa.stitch_scene_clips(storage=storage, clip_refs=refs,
                                output_ref="assembled/x/stitched.mp4",
                                target_duration_seconds=2)
    assert res is not None and res.get("output_ref"), "stitch failed on quoted ref names"
    d = fa.probe_duration_seconds(storage.path_for("assembled/x/stitched.mp4"))
    assert d is not None and 1.5 <= d <= 2.6


# ── 4. API validation + end to end ──────────────────────────────────────


@pytest.fixture()
def api(tmp_path, monkeypatch):
    db, st = tmp_path / "vf.db", tmp_path / "storage"
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(db))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(st))
    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("MAX_COST_PER_VIDEO_USD", "5")
    monkeypatch.setenv("MAX_COST_PER_BATCH_USD", "20")
    from fastapi.testclient import TestClient
    from api.main import create_app
    from core.repo import Repo
    from core.runners import JobRunner, Singleton
    repo = Repo(str(db))
    runner = JobRunner(repo=repo, storage_root=str(st), schema={},
                       budget_per_video_usd=5.0, budget_per_batch_usd=20.0)
    Singleton.set(runner)
    client = TestClient(create_app(repo=repo, runner=runner, storage_root=str(st), schema={}))
    r = client.post("/auth/signup", json={"email": "s1c@video-factory.test",
                                          "password": "TestPass-1234!",
                                          "workspace_name": "S1c Co"})
    assert r.status_code == 200, r.text
    body = r.json()
    client.headers["X-CSRF-Token"] = body["csrf_token"]
    return client, repo, body["workspace_id"]


def _job_body(topic, **extra):
    body = {"topic": topic, "language": "en-US", "duration_seconds": 4,
            "platforms": ["tiktok"]}
    body.update(extra)
    return body


def _post_job(client, wid, route, topic):
    if route == "workspace":
        return client.post(f"/api/v1/workspaces/{wid}/jobs", json=_job_body(topic))
    return client.post("/api/v1/jobs", json=_job_body(topic, company_id=wid))


@pytest.mark.parametrize("route", ["workspace", "legacy"])
@pytest.mark.parametrize("topic", [
    "", "   ", "\x00\x01\x1b", "A" * 201, "B" * 2000, 123, None, ["x"], {"a": 1},
], ids=["empty", "blank", "only-controls", "201", "2000", "int", "null", "list", "dict"])
def test_api_rejects_invalid_topic_without_reflecting_it(api, route, topic):
    client, repo, wid = api
    r = _post_job(client, wid, route, topic)
    assert r.status_code == 422, (r.status_code, r.text[:200])
    if isinstance(topic, str) and len(topic) > 10:
        assert topic[:20] not in r.text, "validation error reflects the topic"
    assert repo.list_jobs(workspace_id=wid) == []


@pytest.mark.parametrize("route", ["workspace", "legacy"])
def test_api_accepts_200_chars_and_strips_control_chars(api, route):
    client, repo, wid = api
    r = _post_job(client, wid, route, "C" * 200)
    assert r.status_code == 200, r.text
    assert r.json()["topic"] == "C" * 200
    r = _post_job(client, wid, route, "  Hello\x00\x1b[31m\nWorld\x7f  ")
    assert r.status_code == 200, r.text
    assert r.json()["topic"] == "Hello[31m World"


def _wait(client, job_id, timeout_s=120):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        j = client.get(f"/api/v1/jobs/{job_id}").json()
        if j["state"] in ("done", "failed"):
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


@needs_ffmpeg
@pytest.mark.parametrize("key", ["apostrophe", "chain-textfile", "percent-expansion",
                                 "backslash-quote", "unicode-emoji"])
def test_api_job_with_hostile_topic_reaches_done(api, caplog, key):
    client, repo, wid = api
    topic = HOSTILE_TOPICS[key]
    caplog.set_level(logging.DEBUG)
    r = client.post(f"/api/v1/workspaces/{wid}/jobs",
                    json=_job_body(topic, aspect_ratio="1:1"))
    assert r.status_code == 200, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["state"] == "done", job["status_message"]
    assert topic not in (job.get("status_message") or "")
    stages = client.get(f"/api/v1/jobs/{job['job_id']}/stages").json()
    assert {s["status"] for s in stages} == {"done"}, stages
    outs = [o for o in client.get(f"/api/v1/jobs/{job['job_id']}/outputs").json()
            if o["kind"] == "final_video"]
    assert len(outs) == 1 and outs[0]["size_bytes"] > 1000
    for rec in caplog.records:
        assert topic not in rec.getMessage(), f"topic in log: {rec.name}"
