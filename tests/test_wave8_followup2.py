"""Wave 8 follow-up-2 (t_9cead677): developer regression tests.

The QA pins for QA-N1..N7 live in ``tests/test_wave8_followup_qa.py``.
This file pins the details of the fixes that those pins do not reach:

* the linear email scanner gives exactly the old regex result
  (differential, seeded), and ``scrub_text`` caps its input;
* ``redact_query_string`` leaves innocent nested values alone and
  handles fragments / depth;
* ``Repo.readiness_probe`` on a real file: write lock, deleted file,
  file replaced underneath the process, busy timeout restored, no
  transaction left open, fast answer while locked.

All data is synthetic.
"""
from __future__ import annotations

import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api import observability as obs  # noqa: E402

OLD_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ────────────────────────────────────────────────────────────────────
# QA-N1: linear scrubber, same answers as the old regex
# ────────────────────────────────────────────────────────────────────


def test_email_scanner_matches_old_regex_exactly():
    rnd = random.Random(20261010)
    alphabet = "ab1.-_%+@ xZ9\n#/=?&"
    for _ in range(20000):
        s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 40)))
        assert obs._scrub_emails(s, "@") == OLD_EMAIL_RE.sub("[email]", s), repr(s)


@pytest.mark.parametrize("s,want", [
    ("mail alice@example.com now", "mail [email] now"),
    ("a@b.cc.dd@e.ff", "[email]@e.ff"),
    ("x@y.co1abc@z.com", "[email][email]"),
    ("<bob.smith+tag@mail.example.org>", "<[email]>"),
    ("contact=alice%40example.com&q=1", "contact=[email]&q=1"),
    ("a%2Bb%40ex.io", "[email]"),
    ("no at sign here", "no at sign here"),
    ("trailing@", "trailing@"),
    ("@leading.com", "@leading.com"),
])
def test_scrub_text_email_cases(s, want):
    assert obs.scrub_text(s) == want


@pytest.mark.parametrize("payload", [
    "a" * 200_000, "a@" * 100_000, "@a" * 100_000, "a%40" * 50_000,
    "a@" + "b." * 100_000, "u_" * 100_000, "bearer " + " " * 200_000,
])
def test_scrub_text_is_bounded_on_hostile_input(payload):
    t0 = time.perf_counter()
    out = obs.scrub_text(payload)
    took = time.perf_counter() - t0
    assert len(out) <= obs.MAX_SCRUB_CHARS + len(obs.TRUNCATED)
    assert took < 0.25, f"{took:.3f}s"


def test_scrub_text_cut_drops_half_token():
    """An email straddling the cap must not survive as a fragment."""
    pad = "x " * ((obs.MAX_SCRUB_CHARS - 6) // 2)
    s = pad + "alice.leaky@example.com and more"
    out = obs.scrub_text(s)
    assert out.endswith(obs.TRUNCATED)
    assert "alice" not in out and "leaky" not in out


def test_scrub_event_long_query_is_cheap():
    event = {"request": {"query_string": "x=" + "a@" * 8000 + "&code=c1",
                         "url": "http://h/p", "headers": {}},
             "exception": {"values": [{"type": "E", "value": "b@" * 8000}]}}
    t0 = time.perf_counter()
    out = obs.scrub_event(event)
    assert time.perf_counter() - t0 < 0.25
    assert "c1" not in out["request"]["query_string"]


# ────────────────────────────────────────────────────────────────────
# QA-N3 / QA-N5: query redaction details
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("qs,secret", [
    ("next=https%3A%2F%2Fx%2Fcb%23access_token%3DFRAGSECRET", "FRAGSECRET"),
    ("next=%2Fa%3Fnext%3D%252Fb%253Fcode%253DDEEPSECRET", "DEEPSECRET"),
    ("id_token_hint=HINTSECRET", "HINTSECRET"),
    ("passphrase=PPSECRET", "PPSECRET"),
    ("nonce=NONCESECRET", "NONCESECRET"),
    ("Contact=carol%2540leak.example", "carol"),
    ("x=wks_1a2b3c4d5e6f", "wks_1a2b3c4d5e6f"),
])
def test_redact_query_string_more_variants(qs, secret):
    assert secret not in obs.redact_query_string(qs)


@pytest.mark.parametrize("qs", [
    "page=3", "q=hello+world", "next=%2Fa%3Fb%3D1%26page%3D2", "a=b%3Dc",
])
def test_redact_query_string_keeps_innocent_values(qs):
    assert obs.redact_query_string(qs) == qs


def test_redact_query_string_depth_cap_filters():
    inner = "code=DEEP"
    for _ in range(5):
        inner = "next=" + inner.replace("%", "%25").replace("=", "%3D").replace("&", "%26")
    assert "DEEP" not in obs.redact_query_string(inner)


def test_request_id_from_rejects_crlf_and_keeps_valid():
    assert obs.request_id_from("abcdefgh") == "abcdefgh"
    for bad in ("abcdefgh\n", "abcdefgh\r\n", "\nabcdefgh", "abc defgh"):
        assert obs.request_id_from(bad) != bad


# ────────────────────────────────────────────────────────────────────
# QA-N2: Repo.readiness_probe on a real file
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def plain_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEO_FACTORY_DB_KEY", raising=False)
    from core.repo import Repo
    path = tmp_path / "vf.db"
    return Repo(db_path=str(path)), path


def test_probe_ok_restores_busy_timeout_and_leaves_no_tx(plain_repo):
    repo, _ = plain_repo
    c = repo._conn()  # noqa: SLF001
    before = c.execute("PRAGMA busy_timeout").fetchone()[0]
    assert repo.readiness_probe() is None
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == before
    assert c.in_transaction is False
    # A normal write still works right after the probe.
    with repo.tx() as tc:
        tc.execute("CREATE TABLE IF NOT EXISTS _probe_t (x)")


def test_probe_reports_write_lock_fast(plain_repo):
    repo, path = plain_repo
    assert repo.readiness_probe() is None
    other = sqlite3.connect(str(path), isolation_level=None, timeout=0)
    other.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.perf_counter()
        assert repo.readiness_probe() == "db_locked"
        assert time.perf_counter() - t0 < 1.0
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert repo.readiness_probe() is None


def test_probe_reports_deleted_then_replaced_file(plain_repo):
    repo, path = plain_repo
    assert repo.readiness_probe() is None
    for suf in ("", "-wal", "-shm"):
        try:
            os.unlink(f"{path}{suf}")
        except FileNotFoundError:
            pass
    assert repo.readiness_probe() == "db_file_missing"
    # Something recreates an empty file at the same path.
    sqlite3.connect(str(path)).close()
    path.touch()
    assert repo.readiness_probe() == "db_file_replaced"


def test_probe_on_fresh_connection_to_empty_file(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEO_FACTORY_DB_KEY", raising=False)
    from core.repo import Repo
    repo = Repo(db_path=str(tmp_path / "vf.db"))
    repo.close()  # next _conn() is fresh
    os.unlink(tmp_path / "vf.db")
    for suf in ("-wal", "-shm"):
        try:
            os.unlink(f"{tmp_path / 'vf.db'}{suf}")
        except FileNotFoundError:
            pass
    (tmp_path / "vf.db").touch()
    assert repo.readiness_probe() == "db_schema_missing"


def test_probe_rearms_after_epoch_bump(plain_repo):
    """Key rotation swaps the file and bumps the epoch: the probe must
    follow the new connection instead of reporting a replaced file."""
    repo, path = plain_repo
    assert repo.readiness_probe() is None
    copy = path.with_suffix(".new")
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(copy))
    src.backup(dst)
    src.close()
    dst.close()
    os.replace(copy, path)
    assert repo.readiness_probe() == "db_file_replaced"
    repo._epoch += 1  # noqa: SLF001 - what key_rotation does after the swap
    assert repo.readiness_probe() is None
