"""JOBS S3: job pipeline correctness under concurrency, restarts, growth.

Covers the t_7a443c2f fix list:

1. BLK-1   concurrent submits (threads, one Repo or one Repo per thread)
           cannot exceed the FREE quota or the budget ceiling; unknown
           cost is refused synchronously (402) before any row exists.
2.         runner concurrency cap, FIFO queue, F-32 slot cleanup,
           Prometheus gauges.
3. F-30/10 stale ``running`` jobs re-queued once then failed; startup
           re-submits queued jobs FIFO; idempotency store seeded
           deterministically.
4.         ffmpeg thread caps on every encode.
5. F-11    schema_migrations ledger: each migration once, concurrent
           start (processes + threads), upgrade from the oldest
           supported schema (V0 tables + Wave 1 auth migrations).
6. F-07/42 retention deletes only old artifacts / audit rows.
7. MAJ-4   pagination bounds (limit, cursor, max 200, header).
8. MAJ-1/2 budget_enforcer shim gone; one audit_log writer; events land.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
V0_SCHEMA = Path(__file__).resolve().parent / "fixtures" / "schema_v0_c3cb72e.sql"

_JOB = dict(topic="s3 topic", language="en", duration_seconds=6,
            platforms=["tiktok"])
_EPOCH = "1970-01-01T00:00:00Z"
_OLD_ISO = "2020-01-01T00:00:00Z"


def _mk(tmp_path, *, max_jobs=None, video=5.0, batch=20.0, storage="storage",
        repo=None):
    from core.repo import Repo
    from core.runners import JobRunner
    repo = repo or Repo(str(tmp_path / "vf.db"))
    runner = JobRunner(
        repo=repo, storage_root=str(tmp_path / storage), schema={},
        budget_per_video_usd=video, budget_per_batch_usd=batch,
        max_concurrent_jobs=max_jobs,
    )
    return repo, runner


def _ws(repo, slug="s3", plan="free"):
    return repo.create_workspace(slug=slug, name=slug.upper(), plan=plan)


def _job_count(repo, wid):
    return repo.count_jobs_since(workspace_id=wid, since_iso=_EPOCH)


def _race(fns):
    """Run every fn at the same instant; return their results/exceptions."""
    barrier = threading.Barrier(len(fns))
    out = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        try:
            out[i] = ("ok", fn())
        except Exception as e:  # noqa: BLE001
            out[i] = ("err", e)

    ts = [threading.Thread(target=wrap, args=(i, f), name=f"s3-race-{i}")
          for i, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
        assert not t.is_alive(), "race thread hung"
    return out


# ── 1. BLK-1 atomic admission ────────────────────────────────────


@pytest.mark.parametrize("repo_per_thread", [False, True],
                         ids=["one-repo", "repo-per-thread"])
def test_concurrent_submits_cannot_exceed_free_quota(tmp_path, monkeypatch,
                                                     repo_per_thread):
    from core.admission import JobAdmissionRefused
    from core.quotas import FREE_JOB_LIMIT
    from core.repo import Repo

    repo, runner = _mk(tmp_path)
    ws = _ws(repo)
    wid = ws["workspace_id"]
    used = FREE_JOB_LIMIT - 2
    for _ in range(used):
        repo.create_job(workspace_id=wid, **_JOB)

    # Widen the count -> insert window: a non-atomic check-then-insert
    # lets every thread see `used` jobs and all of them pass.
    orig = Repo.count_jobs_since

    def slow_count(self, **kw):
        n = orig(self, **kw)
        time.sleep(0.02)
        return n

    monkeypatch.setattr(Repo, "count_jobs_since", slow_count)

    n = 8
    if repo_per_thread:
        # Separate Repo objects = separate locks + connections, like
        # separate processes: only SQLite's write lock serialises them.
        runners = [_mk(tmp_path, storage=f"s{i}",
                       repo=Repo(str(tmp_path / "vf.db")))[1] for i in range(n)]
    else:
        runners = [runner] * n
    results = _race([
        (lambda r=r: r.create_job_admitted(workspace=ws, endpoint="test", **_JOB))
        for r in runners
    ])
    ok = [v for k, v in results if k == "ok"]
    refused = [v for k, v in results if k == "err"]
    assert len(ok) == 2, results
    assert all(isinstance(e, JobAdmissionRefused) and e.status_code == 429
               for e in refused), refused
    assert len(refused) == n - 2
    assert _job_count(repo, wid) == FREE_JOB_LIMIT
    assert refused[0].body["jobs_remaining"] == 0
    assert refused[0].audit_event == "quota.exceeded"


def test_concurrent_submits_cannot_exceed_budget(tmp_path, monkeypatch):
    from core.admission import JobAdmissionRefused
    from render_plane.provider import CostEstimate

    repo, runner = _mk(tmp_path, video=5.0, batch=10.0)
    ws = _ws(repo, plan="pro")  # no job quota: only the budget limits
    monkeypatch.setattr(runner, "estimate_cost",
                        lambda profile: CostEstimate(amount_usd=3.0))
    n = 8
    results = _race([
        (lambda: runner.create_job_admitted(workspace=ws, endpoint="t", **_JOB))
        for _ in range(n)
    ])
    ok = [v for k, v in results if k == "ok"]
    refused = [v for k, v in results if k == "err"]
    assert len(ok) == 3, results  # 3 x 3.0 <= 10.0 < 4 x 3.0
    assert len(refused) == n - 3
    for e in refused:
        assert isinstance(e, JobAdmissionRefused)
        assert e.status_code == 402
        assert e.body["reason_code"] == "ceiling"
    assert runner._budget.spent == pytest.approx(9.0)
    assert _job_count(repo, ws["workspace_id"]) == 3


def test_unknown_cost_refused_before_row_is_written(tmp_path):
    from core.admission import JobAdmissionRefused

    repo, runner = _mk(tmp_path)
    ws = _ws(repo, plan="pro")
    with pytest.raises(JobAdmissionRefused) as ei:
        runner.create_job_admitted(workspace=ws, endpoint="t",
                                   render_profile="no_such_profile", **_JOB)
    assert ei.value.status_code == 402
    assert ei.value.body["reason_code"] == "unknown_cost"
    assert ei.value.audit_event == "budget.refused"
    assert _job_count(repo, ws["workspace_id"]) == 0
    assert runner._budget.spent == 0.0


def _api(tmp_path, monkeypatch, *, max_jobs=None):
    from api.main import create_app
    from core.runners import Singleton
    from tests.test_api import _AuthedClient

    monkeypatch.setenv("VIDEO_FACTORY_SESSION_SECRET", "x" * 64)
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.setenv("VIDEO_FACTORY_STORAGE", str(tmp_path / "storage"))
    repo, runner = _mk(tmp_path, max_jobs=max_jobs)
    Singleton.set(runner)
    app = create_app(repo=repo, runner=runner,
                     storage_root=str(tmp_path / "storage"), schema={})
    client = _AuthedClient(TestClient(app))
    client._ensure_signed_up()
    wid = repo._conn().execute(
        "SELECT workspace_id FROM workspaces").fetchone()[0]
    return client, repo, runner, wid, app


def test_api_unknown_cost_is_402_and_audited(tmp_path, monkeypatch):
    from render_plane.provider import CostEstimate

    client, repo, runner, wid, _app = _api(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "estimate_cost", lambda p: CostEstimate(
        amount_usd=None, unknown=True, assumptions="live price unknown"))
    r = client.post(f"/api/v1/workspaces/{wid}/jobs", json=dict(_JOB))
    assert r.status_code == 402, r.text
    assert r.json()["reason_code"] == "unknown_cost"
    assert repo.list_jobs(workspace_id=wid) == []
    rows = [a for a in repo.list_audit(workspace_id=wid)
            if a["event"] == "budget.refused"]
    assert len(rows) == 1
    assert rows[0]["context"]["reason_code"] == "unknown_cost"
    assert rows[0]["actor"] == "tester@video-factory.test"


def test_api_quota_refusal_is_429_and_audited(tmp_path, monkeypatch):
    from core.quotas import FREE_JOB_LIMIT

    client, repo, runner, wid, _app = _api(tmp_path, monkeypatch)
    for _ in range(FREE_JOB_LIMIT):
        repo.create_job(workspace_id=wid, **_JOB)
    r = client.post(f"/api/v1/workspaces/{wid}/jobs", json=dict(_JOB))
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["jobs_remaining"] == 0 and body["jobs_used"] == FREE_JOB_LIMIT
    assert _job_count(repo, wid) == FREE_JOB_LIMIT
    rows = [a for a in repo.list_audit(workspace_id=wid)
            if a["event"] == "quota.exceeded"]
    assert len(rows) == 1 and rows[0]["context"]["admission"] == "atomic"


# ── 2. Runner concurrency cap ────────────────────────────────────


def _blocking_run(runner, monkeypatch):
    gate = threading.Event()
    lock = threading.Lock()
    st = {"now": 0, "peak": 0, "order": []}
    started = threading.Semaphore(0)

    def fake_run(job_id):
        with lock:
            st["now"] += 1
            st["peak"] = max(st["peak"], st["now"])
            st["order"].append(job_id)
        started.release()
        try:
            gate.wait(30)
        finally:
            with lock:
                st["now"] -= 1

    monkeypatch.setattr(runner, "_run", fake_run)
    return gate, started, st


def test_queue_cap_honored_excess_stays_queued(tmp_path, monkeypatch):
    from api.observability import REGISTRY
    from core.runners import Singleton

    _repo, runner = _mk(tmp_path, max_jobs=2)
    Singleton.set(runner)
    gate, started, st = _blocking_run(runner, monkeypatch)
    ids = [f"job_cap{i}" for i in range(5)]
    for j in ids:
        runner.submit(j)  # never rejected
    try:
        assert started.acquire(timeout=10) and started.acquire(timeout=10)
        assert runner.active_count() == 2
        assert runner.queue_depth() == 3
        assert sorted(st["order"]) == sorted(ids[:2])
        # duplicate submit of a queued / running job is a no-op
        runner.submit(ids[0])
        runner.submit(ids[4])
        assert runner.queue_depth() == 3
        # Prometheus gauges read the live runner
        assert REGISTRY.get_sample_value("video_factory_job_queue_depth") == 3.0
        assert REGISTRY.get_sample_value("video_factory_jobs_active") == 2.0
        assert REGISTRY.get_sample_value(
            "video_factory_jobs_concurrency_limit") == 2.0
    finally:
        gate.set()
    assert runner.wait_all(timeout=30)
    assert st["peak"] == 2
    assert sorted(st["order"]) == sorted(ids)
    assert runner.active_count() == 0 and runner.queue_depth() == 0
    assert REGISTRY.get_sample_value("video_factory_jobs_active") == 0.0


def test_queue_is_fifo(tmp_path, monkeypatch):
    _repo, runner = _mk(tmp_path, max_jobs=1)
    gate, started, st = _blocking_run(runner, monkeypatch)
    ids = [f"job_fifo{i}" for i in range(6)]
    for j in ids:
        runner.submit(j)
    assert started.acquire(timeout=10)
    assert runner.queue_depth() == 5
    gate.set()
    assert runner.wait_all(timeout=30)
    assert st["order"] == ids
    assert st["peak"] == 1


def test_max_concurrent_jobs_env(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEO_FACTORY_MAX_CONCURRENT_JOBS", raising=False)
    assert _mk(tmp_path, storage="a")[1].max_concurrent_jobs == 2
    monkeypatch.setenv("VIDEO_FACTORY_MAX_CONCURRENT_JOBS", "3")
    assert _mk(tmp_path, storage="b")[1].max_concurrent_jobs == 3
    monkeypatch.setenv("VIDEO_FACTORY_MAX_CONCURRENT_JOBS", "garbage")
    assert _mk(tmp_path, storage="c")[1].max_concurrent_jobs == 2


def test_active_slot_released_when_run_raises(tmp_path, monkeypatch):
    """F-32: a crashed pipeline must not leave its job in _active_jobs."""
    _repo, runner = _mk(tmp_path, max_jobs=1)
    calls = []

    def boom(job_id):
        calls.append(job_id)
        raise RuntimeError("pipeline bug")

    monkeypatch.setattr(runner, "_run", boom)
    runner.submit("job_boom")
    runner.submit("job_next")  # queued behind the crashing one
    assert runner.wait_all(timeout=10)
    assert runner.active_count() == 0 and runner.queue_depth() == 0
    runner.submit("job_boom")  # resubmittable
    assert runner.wait("job_boom", timeout=10)
    assert calls == ["job_boom", "job_next", "job_boom"]


# ── 3. Restart safety ────────────────────────────────────────────


def _make_stale(repo, job_id):
    repo._conn().execute(
        "UPDATE jobs SET state='running', updated_at=? WHERE job_id=?",
        (_OLD_ISO, job_id),
    )


def test_stale_running_job_requeued_once_then_failed(tmp_path):
    repo, runner = _mk(tmp_path)
    wid = _ws(repo)["workspace_id"]
    jid = repo.create_job(workspace_id=wid, **_JOB)["job_id"]
    fresh = repo.create_job(workspace_id=wid, **_JOB)["job_id"]
    _make_stale(repo, jid)
    repo.update_job(fresh, state="running")  # updated_at = now
    repo.upsert_stage(job_id=jid, stage_key="assemble", ordinal=7,
                      status="running", started_at=_OLD_ISO)

    r1 = runner.recover_stale_jobs(older_than_seconds=600)
    assert r1 == {"requeued": [jid], "failed": []}
    j = repo.get_job(jid)
    assert j["state"] == "queued"
    assert "recovered after restart" in j["status_message"]
    assert repo.get_job_recovery_count(jid) == 1
    st = [s for s in repo.list_stages(jid) if s["stage_key"] == "assemble"][0]
    assert st["status"] == "failed" and "interrupted" in (st["error"] or "")
    assert repo.get_job(fresh)["state"] == "running"  # not stale: untouched

    _make_stale(repo, jid)  # crashed again after the requeue
    r2 = runner.recover_stale_jobs(older_than_seconds=600)
    assert r2 == {"requeued": [], "failed": [jid]}
    j = repo.get_job(jid)
    assert j["state"] == "failed"
    assert "not retried again" in j["status_message"]
    assert runner.recover_stale_jobs(older_than_seconds=600) == {
        "requeued": [], "failed": []}
    events = [r[0] for r in repo._conn().execute(
        "SELECT event FROM runner_audit WHERE job_id=? ORDER BY created_at, rowid",
        (jid,)).fetchall()]
    assert events == ["job.recovered_requeued", "job.recovered_failed"]


def test_recovery_skips_jobs_this_runner_is_executing(tmp_path, monkeypatch):
    repo, runner = _mk(tmp_path)
    wid = _ws(repo)["workspace_id"]
    jid = repo.create_job(workspace_id=wid, **_JOB)["job_id"]
    gate, started, _st = _blocking_run(runner, monkeypatch)
    runner.submit(jid)
    assert started.acquire(timeout=10)
    _make_stale(repo, jid)
    try:
        assert runner.recover_stale_jobs(older_than_seconds=600) == {
            "requeued": [], "failed": []}
    finally:
        gate.set()
    assert runner.wait_all(timeout=10)


def test_startup_resubmits_queued_jobs_fifo(tmp_path, monkeypatch):
    repo, _old = _mk(tmp_path)
    wid = _ws(repo)["workspace_id"]
    ids = [repo.create_job(workspace_id=wid, **_JOB)["job_id"] for _ in range(4)]
    _make_stale(repo, ids[1])  # crashed mid-run in the previous process
    repo.update_job(ids[3], state="done")
    # "new process": a fresh runner on the same DB
    _repo, runner = _mk(tmp_path, max_jobs=1, storage="storage2", repo=repo)
    ran = []
    monkeypatch.setattr(runner, "_run", lambda j: ran.append(j))
    res = runner.recover_on_startup(older_than_seconds=600)
    assert runner.wait_all(timeout=10)
    assert res["requeued"] == [ids[1]]
    assert res["resubmitted"] == ids[:3]
    assert ran == ids[:3]


def test_lifespan_runs_startup_recovery(tmp_path, monkeypatch):
    client, repo, runner, wid, app = _api(tmp_path, monkeypatch)
    jid = repo.create_job(workspace_id=wid, **_JOB)["job_id"]
    ran = []
    monkeypatch.setattr(runner, "_run", lambda j: ran.append(j))
    with TestClient(app):
        assert runner.wait_all(timeout=10)
    assert ran == [jid]
    assert app.state.startup_recovery["resubmitted"] == [jid]
    assert app.state.retention_sweeper.enabled() is False

    ran.clear()
    monkeypatch.setenv("VIDEO_FACTORY_RECOVER_ON_STARTUP", "0")
    with TestClient(app):
        pass
    assert ran == []


def test_idempotency_store_seeded_deterministically(tmp_path):
    from control_plane.idempotency import (
        DurableIdempotencyStore, ProviderSubmitReceipt,
    )

    storage = tmp_path / "storage"
    storage.mkdir()
    durable = DurableIdempotencyStore(str(storage / "idem.db"))
    for k in ("k3", "k1", "k2"):
        durable.save(k, ProviderSubmitReceipt(provider_job_id=f"p-{k}",
                                              accepted_metadata={"k": k}))
    expected = [r[0] for r in sqlite3.connect(str(storage / "idem.db")).execute(
        "SELECT idem_key FROM idem_receipts ORDER BY created_at, idem_key")]
    assert [k for k, _ in durable.all_receipts()] == expected

    repo, runner = _mk(tmp_path)
    store = runner._idem_for("job_a")
    assert store is runner._idem_for("job_b")  # one store per runner
    for k in ("k1", "k2", "k3"):
        assert store.lookup(k).provider_job_id == f"p-{k}"
    _repo2, runner2 = _mk(tmp_path, repo=repo)  # restart: same contents
    for k in ("k1", "k2", "k3"):
        assert runner2._idem_for("x").lookup(k).accepted_metadata == {"k": k}


def test_durable_submit_once_single_winner_across_threads(tmp_path):
    from control_plane.idempotency import (
        DurableIdempotencyStore, ProviderSubmitReceipt,
    )

    store = DurableIdempotencyStore(str(tmp_path / "idem.db"))
    results = _race([
        (lambda i=i: store.submit_once(
            "same-key", ProviderSubmitReceipt(provider_job_id=f"p{i}",
                                             accepted_metadata={})))
        for i in range(8)
    ])
    winners = {v.provider_job_id for k, v in results if k == "ok"}
    assert len(winners) == 1, results


# ── 4. ffmpeg memory bound ───────────────────────────────────────


def test_ffmpeg_encodes_are_thread_capped(monkeypatch, tmp_path):
    from core import edit_plan

    cmds = []
    monkeypatch.setattr(edit_plan.subprocess, "check_call",
                        lambda cmd, **kw: cmds.append(cmd) or 0)
    monkeypatch.delenv("VIDEO_FACTORY_FFMPEG_THREADS", raising=False)
    edit_plan._ffmpeg_synthetic_clip(out=str(tmp_path / "c.mp4"), duration=1.0,
                                     width=1080, height=1920, color="blue",
                                     label="x")
    edit_plan._ffmpeg_concat(concat=str(tmp_path / "l.txt"),
                             out=str(tmp_path / "o.mp4"), width=1080,
                             height=1920, duration=1)
    for cmd in cmds:
        assert cmd[cmd.index("-threads") + 1] == "2"
        assert cmd[cmd.index("-filter_threads") + 1] == "1"
        assert cmd.index("-threads") < len(cmd) - 1  # output option
    monkeypatch.setenv("VIDEO_FACTORY_FFMPEG_THREADS", "1")
    assert edit_plan.ffmpeg_thread_args() == ["-threads", "1", "-filter_threads", "1"]
    monkeypatch.setenv("VIDEO_FACTORY_FFMPEG_THREADS", "bogus")
    assert edit_plan.ffmpeg_thread_args()[1] == "2"


# ── 5. Migrations ledger ─────────────────────────────────────────


def _all_ids():
    from core.migrations import load_migrations
    return sorted(m[0] for m in load_migrations())


def _ledger(db):
    c = sqlite3.connect(str(db))
    try:
        return c.execute(
            "SELECT id, COUNT(*) FROM schema_migrations GROUP BY id ORDER BY id"
        ).fetchall()
    finally:
        c.close()


def test_migrations_applied_once_and_idempotent(tmp_path, monkeypatch):
    import core.migrations as mig
    from core.repo import Repo

    db = tmp_path / "m.db"
    repo = Repo(str(db))
    conn = repo._conn()
    assert mig.applied_migration_ids(conn) == _all_ids()
    assert mig.apply_migrations(conn) == 0
    Repo(str(db))  # second boot
    assert _ledger(db) == [(i, 1) for i in _all_ids()]

    # A NON-idempotent migration runs exactly once thanks to the ledger.
    monkeypatch.setattr(mig, "MIGRATIONS", list(mig.MIGRATIONS) + [(
        "999", "test-only non idempotent",
        "CREATE TABLE t999 (x INTEGER);\nINSERT INTO t999 VALUES (1);",
    )])
    assert mig.apply_migrations(conn) == 1
    assert mig.apply_migrations(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM t999").fetchone()[0] == 1


def test_failed_migration_rolls_back_and_is_not_recorded(tmp_path, monkeypatch):
    import core.migrations as mig
    from core.repo import Repo

    repo = Repo(str(tmp_path / "m.db"))
    conn = repo._conn()
    monkeypatch.setattr(mig, "MIGRATIONS", list(mig.MIGRATIONS) + [(
        "998", "test-only broken",
        "CREATE TABLE t998 (x INTEGER);\nTHIS IS NOT SQL;",
    )])
    with pytest.raises(sqlite3.OperationalError):
        mig.apply_migrations(conn)
    assert "998" not in mig.applied_migration_ids(conn)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='t998'"
                        ).fetchone() is None


def test_concurrent_start_two_processes(tmp_path):
    db = tmp_path / "c.db"
    code = ("import sys; from core.repo import Repo; "
            "Repo(sys.argv[1]); print('ok')")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    env.pop("VIDEO_FACTORY_DB_KEY", None)
    procs = [subprocess.Popen([sys.executable, "-c", code, str(db)], cwd=str(ROOT),
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
             for _ in range(3)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err[-2000:]
        assert out.strip().endswith("ok")
    assert _ledger(db) == [(i, 1) for i in _all_ids()]


def test_concurrent_start_threads(tmp_path):
    from core.repo import Repo

    db = tmp_path / "t.db"
    results = _race([(lambda: Repo(str(db))) for _ in range(6)])
    assert all(k == "ok" for k, _ in results), results
    assert _ledger(db) == [(i, 1) for i in _all_ids()]


def _oldest_supported_db(path):
    """V0 tables (c3cb72e) + Wave 1 auth migrations 001-004, applied the
    pre-ledger way (executescript, no schema_migrations table). This is
    the shape of the oldest production snapshot
    (artifacts/video_factory.db.bak): `company_id` columns, no ledger.
    A pure V0 DB without the Wave 1 tables never booted (migration 005
    needs `workspaces`), so it is not a supported starting point."""
    from core.migrations import load_migrations

    c = sqlite3.connect(str(path))
    c.executescript(V0_SCHEMA.read_text())
    for mid, _d, sql in load_migrations():
        if mid <= "004":
            c.executescript(sql)
    t = "2026-01-01T00:00:00Z"
    c.execute("INSERT INTO companies VALUES ('cmp_v0','acme','Acme','',?,?)",
              (t, t))
    c.execute(
        "INSERT INTO jobs(job_id, company_id, topic, language, "
        "duration_seconds, platforms_json, created_at, updated_at, state) "
        "VALUES ('job_v0','cmp_v0','legacy topic','en',6,'[]',?,?,'running')",
        (t, t))
    c.execute("INSERT INTO audit_log(audit_id, event, context_json, created_at)"
              " VALUES ('aud_v0','legacy.event','{}',?)", (t,))
    c.commit()
    c.close()


def test_upgrade_from_oldest_supported_schema(tmp_path):
    from core.migrations import applied_migration_ids, apply_migrations
    from core.repo import Repo

    db = tmp_path / "old.db"
    _oldest_supported_db(db)
    repo = Repo(str(db))
    assert applied_migration_ids(repo._conn()) == _all_ids()
    job = repo.get_job("job_v0")
    assert job["topic"] == "legacy topic"
    assert job["workspace_id"] == repo.workspace_id_for_company("cmp_v0")
    assert repo.get_job_recovery_count("job_v0") == 0
    items, nxt = repo.list_jobs_page(workspace_id=job["workspace_id"])
    assert [j["job_id"] for j in items] == ["job_v0"] and nxt is None
    assert apply_migrations(repo._conn()) == 0
    Repo(str(db))  # reboot: nothing re-applied
    assert _ledger(db) == [(i, 1) for i in _all_ids()]


# ── 6. Retention ─────────────────────────────────────────────────


def _touch(path, *, days_old, data=b"x" * 10):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time() - days_old * 86400
    os.utime(path, (t, t))
    return path


def test_artifact_retention_deletes_only_old_job_artifacts(tmp_path):
    from core.retention import sweep_artifacts

    root = tmp_path / "storage"
    old = _touch(root / "jobs/job_old/final/a.mp4", days_old=40)
    old_sidecar = _touch(root / "jobs/job_old/final/a.mp4.hmac", days_old=40)
    new = _touch(root / "jobs/job_new/final/b.mp4", days_old=1)
    upload = _touch(root / "workspaces/acme/assets/logo.png", days_old=400)
    idem = _touch(root / "idem.db", days_old=400)

    assert sweep_artifacts(str(root), 0)["files_deleted"] == 0  # off
    assert old.exists()
    dry = sweep_artifacts(str(root), 30, dry_run=True)
    assert dry["files_deleted"] == 2 and old.exists()
    res = sweep_artifacts(str(root), 30)
    assert res["files_deleted"] == 2 and res["bytes_deleted"] == 20
    assert not old.exists() and not old_sidecar.exists()
    assert not (root / "jobs/job_old").exists()  # emptied dirs pruned
    assert new.exists() and upload.exists() and idem.exists()
    assert (root / "jobs").is_dir()


def test_audit_retention_deletes_only_old_rows(tmp_path):
    from control_plane.repo_helpers import append_runner_audit
    from core.retention import sweep_audit

    repo, _runner = _mk(tmp_path)
    wid = _ws(repo)["workspace_id"]
    repo.append_audit(event="recent.event", workspace_id=wid)
    append_runner_audit(repo, job_id="j1", event="recent.runner")
    c = repo._conn()
    c.execute("INSERT INTO audit_log(audit_id, event, context_json, "
              "workspace_id, created_at) VALUES ('old1','old.event','{}',?,?)",
              (wid, _OLD_ISO))
    c.execute("INSERT INTO runner_audit(audit_id, job_id, event, context_json, "
              "created_at) VALUES ('old2','j1','old.runner','{}',?)", (_OLD_ISO,))

    assert sweep_audit(repo, 0)["rows_deleted"] == {}
    dry = sweep_audit(repo, 90, dry_run=True)
    assert dry["rows_deleted"] == {"audit_log": 1, "runner_audit": 1}
    res = sweep_audit(repo, 90)
    assert res["rows_deleted"] == {"audit_log": 1, "runner_audit": 1}
    events = {a["event"] for a in repo.list_audit(workspace_id=wid)}
    assert "recent.event" in events and "old.event" not in events
    runner_events = {r[0] for r in c.execute("SELECT event FROM runner_audit")}
    assert "recent.runner" in runner_events and "old.runner" not in runner_events


def test_retention_cli_and_sweeper(tmp_path, monkeypatch):
    from core.repo import Repo
    from core.retention import RetentionSweeper

    db = tmp_path / "vf.db"
    root = tmp_path / "storage"
    Repo(str(db))
    old = _touch(root / "jobs/j/final/a.mp4", days_old=10)
    env = {**os.environ, "PYTHONPATH": str(ROOT),
           "ARTIFACTS_RETENTION_DAYS": "7", "AUDIT_LOG_RETENTION_DAYS": "0"}
    env.pop("VIDEO_FACTORY_DB_KEY", None)
    p = subprocess.run([sys.executable, "-m", "cli.maintenance", "retention",
                        "--db", str(db), "--storage", str(root)],
                       cwd=str(ROOT), env=env, capture_output=True, text=True,
                       timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    out = json.loads(p.stdout)
    assert out["artifacts"]["files_deleted"] == 1
    assert out["audit"]["enabled"] is False
    assert not old.exists()

    # In-process sweeper: no thread at all when both knobs are 0.
    repo = Repo(str(db))
    monkeypatch.delenv("ARTIFACTS_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("AUDIT_LOG_RETENTION_DAYS", raising=False)
    assert RetentionSweeper(repo=repo, storage_root=str(root)).start() is False
    old2 = _touch(root / "jobs/k/final/b.mp4", days_old=10)
    monkeypatch.setenv("ARTIFACTS_RETENTION_DAYS", "7")
    sw = RetentionSweeper(repo=repo, storage_root=str(root), interval=3600)
    assert sw.start() is True
    deadline = time.monotonic() + 10
    while sw.last_result is None and time.monotonic() < deadline:
        time.sleep(0.02)
    sw.stop()
    assert sw.last_result["artifacts"]["files_deleted"] == 1
    assert not old2.exists()


# ── 7. Pagination ────────────────────────────────────────────────


def test_repo_pagination_bounds_and_ties(tmp_path):
    from core.pagination import MAX_PAGE_SIZE, InvalidCursor

    repo, _runner = _mk(tmp_path)
    wid = _ws(repo)["workspace_id"]
    ids = [repo.create_job(workspace_id=wid, **_JOB)["job_id"] for _ in range(7)]
    # identical timestamps: rowid must break the tie
    repo._conn().execute("UPDATE jobs SET created_at=?", (_OLD_ISO,))
    seen, cursor, pages = [], None, 0
    while True:
        items, cursor = repo.list_jobs_page(workspace_id=wid, limit=3,
                                            cursor=cursor)
        pages += 1
        seen += [j["job_id"] for j in items]
        if cursor is None:
            break
    assert pages == 3
    assert seen == list(reversed(ids))  # newest (highest rowid) first
    for bad in (0, -1):
        with pytest.raises(InvalidCursor):
            repo.list_jobs_page(workspace_id=wid, limit=bad)
    with pytest.raises(InvalidCursor):
        repo.list_jobs_page(workspace_id=wid, cursor="not-a-cursor")
    assert MAX_PAGE_SIZE == 200
    assert repo.list_jobs_page(workspace_ids=[]) == ([], None)
    assert len(repo.list_jobs_page(workspace_ids=[wid])[0]) == 7


def test_repo_pagination_max_200(tmp_path):
    repo, _runner = _mk(tmp_path)
    wid = _ws(repo)["workspace_id"]
    c = repo._conn()
    c.execute("BEGIN")
    for i in range(205):
        c.execute(
            "INSERT INTO jobs(job_id, workspace_id, topic, language, "
            "duration_seconds, platforms_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"job_bulk{i:03d}", wid, "t", "en", 6, "[]",
             f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z", _OLD_ISO))
    c.execute("COMMIT")
    items, nxt = repo.list_jobs_page(workspace_id=wid, limit=10_000)
    assert len(items) == 200 and nxt is not None
    rest, nxt2 = repo.list_jobs_page(workspace_id=wid, limit=10_000, cursor=nxt)
    assert len(rest) == 5 and nxt2 is None
    assert {j["job_id"] for j in items} | {j["job_id"] for j in rest} == {
        f"job_bulk{i:03d}" for i in range(205)}
    # default page (no limit) is the max page too
    assert len(repo.list_jobs_page(workspace_id=wid)[0]) == 200


def test_repo_pagination_assets_audit_publish_intents(tmp_path):
    repo, _runner = _mk(tmp_path)
    wid = _ws(repo)["workspace_id"]
    for i in range(5):
        repo.create_asset(workspace_id=wid, kind="logo", filename=f"f{i}.png",
                          storage_key=f"k{i}", mime_type="image/png",
                          size_bytes=1, checksum="c")
        repo.append_audit(event=f"e{i}", workspace_id=wid)
    jid = repo.create_job(workspace_id=wid, **_JOB)["job_id"]
    for i in range(5):
        repo.create_publish_intent(job_id=jid, company_id=wid, action="draft",
                                   platforms=["tiktok"], caption=str(i))
    for fn in (lambda **kw: repo.list_assets_page(wid, **kw),
               lambda **kw: repo.list_audit_page(workspace_id=wid, **kw),
               lambda **kw: repo.list_publish_intents_page(jid, **kw)):
        first, cur = fn(limit=2)
        second, cur2 = fn(limit=2, cursor=cur)
        third, cur3 = fn(limit=2, cursor=cur2)
        assert [len(first), len(second), len(third)] == [2, 2, 1]
        assert cur3 is None
        flat = first + second + third
        assert len({json.dumps(x, sort_keys=True) for x in flat}) == 5
    # audit default page stays 100 (pre-pagination default)
    for i in range(120):
        repo.append_audit(event="bulk", workspace_id=wid)
    assert len(repo.list_audit_page(workspace_id=wid)[0]) == 100


def test_api_pagination_header_and_bounds(tmp_path, monkeypatch):
    client, repo, runner, wid, _app = _api(tmp_path, monkeypatch)
    ids = [repo.create_job(workspace_id=wid, **_JOB)["job_id"] for _ in range(5)]
    r = client.get(f"/api/v1/workspaces/{wid}/jobs")
    assert r.status_code == 200
    assert [j["job_id"] for j in r.json()] == list(reversed(ids))
    assert "x-next-cursor" not in r.headers  # UI default: one full page
    r1 = client.get(f"/api/v1/workspaces/{wid}/jobs", params={"limit": 2})
    assert len(r1.json()) == 2 and r1.headers["x-next-cursor"]
    got = [j["job_id"] for j in r1.json()]
    cur = r1.headers["x-next-cursor"]
    while cur:
        rn = client.get(f"/api/v1/workspaces/{wid}/jobs",
                        params={"limit": 2, "cursor": cur})
        assert rn.status_code == 200
        got += [j["job_id"] for j in rn.json()]
        cur = rn.headers.get("x-next-cursor")
    assert got == list(reversed(ids))
    assert client.get(f"/api/v1/workspaces/{wid}/jobs",
                      params={"limit": 0}).status_code == 400
    assert client.get(f"/api/v1/workspaces/{wid}/jobs",
                      params={"cursor": "garbage"}).status_code == 400
    assert client.get(f"/api/v1/workspaces/{wid}/assets",
                      params={"limit": 1}).status_code == 200
    ra = client.get(f"/api/v1/workspaces/{wid}/audit", params={"limit": 1})
    assert ra.status_code == 200 and len(ra.json()) == 1
    rl = client.get("/api/v1/jobs", params={"company_id": wid, "limit": 3})
    assert len(rl.json()) == 3 and rl.headers["x-next-cursor"]
    rp = client.get(f"/api/v1/jobs/{ids[0]}/publish", params={"limit": 5})
    assert rp.status_code == 200 and rp.json() == []


# ── 8. Dead duplicates ───────────────────────────────────────────


def test_budget_enforcer_shim_removed():
    assert importlib.util.find_spec("core.budget_enforcer") is None
    assert not (ROOT / "core" / "budget_enforcer.py").exists()


def test_single_audit_log_writer_and_all_paths_land(tmp_path):
    writers = []
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", ".venv/", "web/", ".worktrees/")):
            continue
        if "INSERT INTO audit_log" in p.read_text(errors="ignore"):
            writers.append(rel)
    assert writers == ["core/repo.py"], writers

    from auth.audit_log import append as auth_append
    from control_plane.repo_helpers import append_runner_audit
    from core.audit import emit

    repo, _runner = _mk(tmp_path)
    wid = _ws(repo)["workspace_id"]
    auth_append(repo, event="via.auth", workspace_id=wid, actor="a@x.test",
                context={"k": 1})
    emit(repo, event="via.emit", workspace_id=wid, context={"k": 2})
    append_runner_audit(repo, job_id="j1", event="via.runner", workspace_id=wid,
                        context={"k": 3})
    repo.append_audit(event="via.repo", workspace_id=wid)
    rows = {a["event"]: a for a in repo.list_audit(workspace_id=wid)}
    assert {"via.auth", "via.emit", "via.runner", "via.repo"} <= set(rows)
    assert rows["via.auth"]["actor"] == "a@x.test"
    assert rows["via.runner"]["context"] == {"k": 3}
