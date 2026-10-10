"""Wave 8 follow-up-2 QA gate (t_d0edfb4b): independent adversarial tests.

Written by QA against branch wt/t_9cead677 @ 133302b. The developer did
not write or review this file. Black-box first: the live tests drive a
real ``python -m uvicorn`` subprocess and a fake local Sentry ingest;
the rest go through the public functions with attack variants the dev
tests (``test_wave8_followup2.py``) and the QA pins
(``test_wave8_followup_qa.py``) do not reach.

Tags:
  GUARD   behaviour the fix must keep (passes today).
  DEFECT  pins a real defect found in the fix. These are marked
          ``xfail(strict=True)`` with the defect id, so the suite stays
          green today and flips to a hard failure (XPASS) the moment the
          defect is fixed, which tells the dev to delete the marker.
          Ids are listed in artifacts/t_d0edfb4b-qa-w8f2.md.

    QA-W8F2-D1  double-encoded nested secret (``next=%252F...%253Fcode%253D..``)
                is not redacted (N5 fix only decodes one level).
    QA-W8F2-D2  unencoded ``+`` in an email under a neutral key leaves the
                part before the ``+`` (N3 fix runs after ``+`` -> space).
    QA-W8F2-D3  ``Repo.readiness_probe`` misses a replaced DB file when no
                probe ran before the swap (N2 fix arms on the first probe).
    QA-W8F2-D4  Referer header with ``?code=...`` reaches Sentry verbatim
                (pre-existing, same on main; neighbour of N5).

All emails, ids and secrets below are synthetic.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import http.client
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api import observability as obs  # noqa: E402

VALID_FERNET = "a" * 43 + "="
DB_KEY = "qa-w8f2-db-key-0123456789abcdef"
SESSION_SECRET = "qa-w8f2-session-secret-0123456789abcdef0123456789abcdef"
OLD_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _cpu(fn, *a):
    """CPU seconds of one call (immune to host load, unlike wall time)."""
    t0 = time.process_time()
    fn(*a)
    return time.process_time() - t0


# ────────────────────────────────────────────────────────────────────
# QA-N1: scrubber is linear, and the 8192 cut never leaves a fragment
# ────────────────────────────────────────────────────────────────────

_SECRET_SHAPES = {
    # name: (text, body marker that must not survive in any form)
    "email": ("carolxlongxlocal.part@leakmail.example", "carolx"),
    "pct-email": ("carolxlongxlocal.part%40leakmail.example", "carolx"),
    "bearer": ("Bearer QAbearerFAKE_PREFIXED_deterministic", "QAbear"),
    "basic": ("Basic QAbasicFAKE_PREFIXED_deterministic==", "QAbasi"),
    "stripe": ("sk_test_FAKE_PREFIX_QAstripeSECRET0123456789abc", "QAstri"),
    "whsec": ("whsec_QAwebhookFAKE_PREFIXED_deterministic", "QAwebh"),
    "fernet": ("gAAAAAQAfernetTOKEN0123456789abcdefghijkl==", "QAfern"),
}


@pytest.mark.parametrize("shape", sorted(_SECRET_SHAPES))
def test_qa_w8f2_guard_cut_at_cap_never_leaves_secret_fragment(shape):
    """GUARD N1. A secret that straddles the 8192-char cut at ANY offset
    must not survive as a readable prefix. Walks every start offset from
    well before to just after the cap."""
    secret, marker = _SECRET_SHAPES[shape]
    cap = obs.MAX_SCRUB_CHARS
    seen_truncated = False
    for start in range(cap - len(secret) - 3, cap + 3):
        s = " " * start + secret + " tail words after the secret"
        out = obs.scrub_text(s)
        assert marker not in out, (shape, start, out[-60:])
        seen_truncated = seen_truncated or out.endswith(obs.TRUNCATED)
    assert seen_truncated, "inputs over the cap must carry the [truncated] marker"


_HOSTILE = {
    "alnum": "a",
    "at-pairs": "a@",
    "pct40-pairs": "a%40",
    "dot-pairs": "a.",
    "dot-after-at": "@x" + ".",
    "dash": "-",
    "wks-prefix": "wks_",
    "bearer-words": "Bearer ",
    "sk-prefix": "sk_test_FAKE_PREFIX_",
    "fernet-prefix": "gAAAAA",
    "short-id": "u_1",
    "domain-dots": "a@b.",
}


@pytest.mark.parametrize("shape", sorted(_HOSTILE))
@pytest.mark.parametrize("factor", [1, 25])
def test_qa_w8f2_guard_scrub_text_cpu_bounded_on_hostile_shapes(shape, factor):
    """GUARD N1. 12 hostile repeat-shapes, at the cap and 25x over it.
    Quadratic code (the old regex) needs minutes here; the fix needs
    milliseconds. Threshold is CPU time, 0.25 s, ~50x headroom."""
    unit = _HOSTILE[shape]
    s = unit * (obs.MAX_SCRUB_CHARS * factor // len(unit))
    assert _cpu(obs.scrub_text, s) < 0.25


@pytest.mark.parametrize("builder", [
    lambda n: "a" * n + "@x",            # long local part, no valid domain
    lambda n: "a@" * (n // 2),           # many separators
    lambda n: "a@b" + ".c" * (n // 2),   # long dotted domain run
    lambda n: "@" * n,                   # separators with empty local parts
    lambda n: "a%40" * (n // 4),         # percent-encoded separators
    lambda n: "x@y." + "-" * n,          # domain run that never ends in letters
], ids=["long-local", "many-at", "dotted-domain", "bare-at", "pct40", "dash-domain"])
def test_qa_w8f2_guard_email_scanner_itself_is_linear_without_the_cap(builder):
    """GUARD N1. The 8192 cap alone would bound a quadratic scanner to
    ~0.1 s per string, so the scanner has to be proven linear on its own:
    call it directly at 60 000 chars (the old ``re.sub`` needs seconds
    here, the scanner needs milliseconds)."""
    s = builder(60000)
    sep = "%40" if "%40" in s else "@"
    assert _cpu(obs._scrub_emails, s, sep) < 0.5        # noqa: SLF001
    # same answer as the old regex on a size where the old regex is still cheap
    small = builder(1500)
    if sep == "@":
        assert obs._scrub_emails(small, "@") == OLD_EMAIL_RE.sub("[email]", small)  # noqa: SLF001


def test_qa_w8f2_guard_scrub_event_cpu_bounded_for_many_hostile_strings():
    """GUARD N1. The event processor walks every string in the event
    (exception, message, breadcrumbs, frame source lines): the cost must
    stay bounded by the number of strings, not by their content."""
    big = "a@" * 60000
    ev = {
        "message": big,
        "exception": {"values": [{"type": "E", "value": big,
                                  "stacktrace": {"frames": [
                                      {"context_line": big} for _ in range(10)]}}]},
        "breadcrumbs": {"values": [{"message": big} for _ in range(30)]},
        "request": {"query_string": "x=" + big[:16000], "headers": {"X-A": big}},
    }
    assert _cpu(obs.scrub_event, ev) < 2.0


def test_qa_w8f2_guard_scrub_text_matches_old_regex_on_other_seeds():
    """GUARD N1. Differential fuzz, different seed and alphabet than the
    dev (adds ``\\n``, tabs, ``%40``, ``@@``, IDN-ish chars, ``-.``)."""
    rnd = random.Random(20261010)
    alpha = list("aZ09._%+- @") + ["@@", "-.", "..", "\n", "\t", "é", "x-y",
                                   "a.b", "co", "com", "x.c", ".c"]
    for _ in range(60000):
        s = "".join(rnd.choice(alpha) for _ in range(rnd.randint(0, 30)))
        if "@" not in s:
            continue
        assert obs._scrub_emails(s, "@") == OLD_EMAIL_RE.sub("[email]", s), repr(s)


# ────────────────────────────────────────────────────────────────────
# QA-N3 / QA-N5: query redaction, attack variants
# ────────────────────────────────────────────────────────────────────

_SENT = "QASENTINEL9f3a"
_SENSITIVE_KEYS = [
    "code", "state", "token", "access_token", "refresh_token", "id_token",
    "password", "passwd", "pwd", "pass", "secret", "client_secret", "api_key",
    "apikey", "email", "mail", "session", "sessionid", "auth", "authorization",
    "sig", "signature", "jwt", "otp", "totp", "login_hint", "id_token_hint",
    "bearer", "nonce", "ticket", "credential", "saml", "SAMLResponse", "CODE",
    "Token", "X-Amz-Signature", "X-Amz-Credential",
]


def _pct_first_char(k: str) -> str:
    return "%%%02X" % ord(k[0]) + k[1:]


@pytest.mark.parametrize("key", _SENSITIVE_KEYS)
def test_qa_w8f2_guard_sensitive_key_hidden_in_every_spelling_and_depth(key):
    """GUARD N5. Plain / percent-encoded key / percent-encoded value, bare
    and nested 1..6 levels deep (each level URL-encoded once). The depth
    cap (3) must degrade to a whole-value filter, never to a leak."""
    for form in ("plain", "enckey", "encval"):
        kk = _pct_first_char(key) if form == "enckey" else key
        vv = quote(_SENT) if form == "encval" else _SENT
        for levels in range(0, 7):
            q = f"{kk}={vv}"
            for _ in range(levels):
                q = "next=" + quote("/cb?" + q, safe="")
            out = obs.redact_query_string("a=1&" + q + "&z=9")
            assert _SENT not in out, (key, form, levels, out)
            assert out.startswith("a=1&") and out.endswith("&z=9") or "[Filtered]" in out


@pytest.mark.parametrize("qs,expect_kept", [
    ("page=2&sort=name&q=coffee+beans", "page=2&sort=name&q=coffee+beans"),
    ("next=/a?b=1&page=2", None),                 # nested innocent: no [Filtered]
    ("t=YWJj==", None),                           # base64 padding is not nesting
    ("a=b%3Dc", None),
])
def test_qa_w8f2_guard_innocent_queries_are_not_over_redacted(qs, expect_kept):
    out = obs.redact_query_string(qs)
    assert "[Filtered]" not in out and "[email]" not in out and "[id]" not in out
    if expect_kept:
        assert out == expect_kept


def test_qa_w8f2_guard_access_log_line_is_one_json_line_for_hostile_query():
    """GUARD D2 neighbour. Hostile query strings through the real access
    formatter: always one parseable JSON line, no forged record, no email
    (plain or %40) and no secret."""
    fmt = obs.JsonLogFormatter()

    def line(path: str) -> str:
        rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                                '%s - "%s %s HTTP/%s" %d',
                                ("203.0.113.9", "GET", path, "1.1", 200), None)
        return fmt.format(rec)

    paths = [
        "/x?contact=zed%40leakcheck.example&n=1",
        "/x?a=%0a%7B%22forged%22%3A%22record%22%7D",
        "/x?a=%0d%0aSet-Cookie%3Ax",
        "/x?a=%00b&c=%E2%80%A8d",
        "/x?a=%ff%fe%ED%A0%80",
        "/x?code=" + _SENT + "&a=" + "b" * 16000,
        "/x?%22=%22&&&=&=",
    ]
    for p in paths:
        out = line(p)
        assert "\n" not in out and "\r" not in out
        rec = json.loads(out)
        assert isinstance(rec, dict) and "forged" not in rec
        assert "zed" not in out and "leakcheck" not in out and _SENT not in out


@pytest.mark.xfail(strict=True, reason="QA-W8F2-D1: nested secret that is "
                   "percent-encoded twice is not decoded to a query; only one "
                   "decode level is applied before the '=' test")
def test_qa_w8f2_defect_d1_double_encoded_nested_secret_is_redacted():
    """DEFECT D1 (minor). ``next=<url-encoded twice: /cb?code=SECRET>`` is
    what a redirect chain produces when an intermediary re-encodes the
    parameter. parse_qsl decodes once -> ``%2Fcb%3Fcode%3DSECRET`` has no
    ``=`` -> the nested pass is skipped -> the code is logged/sent as is."""
    inner = quote("/cb?code=" + _SENT, safe="")        # one level
    outer = quote(inner, safe="")                      # two levels
    out = obs.redact_query_string("next=" + outer)
    assert _SENT not in out, out


@pytest.mark.xfail(strict=True, reason="QA-W8F2-D2: '+' is turned into a space "
                   "by parse_qsl before the email scan, so the part of the "
                   "local part before the '+' is kept")
def test_qa_w8f2_defect_d2_plus_tagged_email_under_neutral_key():
    """DEFECT D2 (minor). ``?contact=taras+test@leak.example.com``. The old
    full-string regex would have removed ``taras+test@...``; the decode
    step now leaves ``taras `` in front of ``[email]``."""
    out = obs.redact_query_string("contact=taras+test@leak.example.com&q=1")
    assert "taras" not in out, out


@pytest.mark.xfail(strict=True, reason="QA-W8F2-D4: only request.query_string is "
                   "query-redacted; URL-bearing headers (Referer, X-Original-URL) "
                   "go through scrub_text, which knows nothing about code=/token=. "
                   "Pre-existing on main.")
def test_qa_w8f2_defect_d4_referer_query_secret_not_in_sentry_event():
    """DEFECT D4 (minor, pre-existing). Browsers send the full same-origin
    URL as Referer (strict-origin-when-cross-origin): a page opened at
    ``/x?code=...`` makes its XHR carry ``Referer: .../x?code=...``."""
    ev = {"request": {"url": "http://h/api/x", "headers": {
        "Referer": "https://app.example/cb?code=" + _SENT + "&state=S"}}}
    raw = json.dumps(obs.scrub_event(ev))
    assert _SENT not in raw, raw


# ────────────────────────────────────────────────────────────────────
# QA-N6: request id
# ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hostile", [
    "abcdefgh\n", "abcdefgh\r\n", "\nabcdefgh", "abc\ndefgh", "abcdefgh\x00",
    "abcdefg", "a" * 65, " abcdefgh", "abcdefgh ", "abcdefg\u0663h",
    "\uff41bcdefgh", "abcdefgh\u2028", "abcd/efgh", "abcd efgh", "",
])
def test_qa_w8f2_guard_request_id_rejects_hostile_and_keeps_valid(hostile):
    got = obs.request_id_from(hostile)
    assert got != hostile
    assert re.fullmatch(r"[0-9a-f]{32}", got)
    for ok in ("abcdefgh", "A" * 64, "req-1234.abc_DEF"):
        assert obs.request_id_from(ok) == ok


# ────────────────────────────────────────────────────────────────────
# QA-N2: /readyz db probe
# ────────────────────────────────────────────────────────────────────

def _plain_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDEO_FACTORY_DB_KEY", raising=False)
    from core.repo import Repo
    return Repo(db_path=str(tmp_path / "vf.db"))


@pytest.mark.xfail(strict=True, reason="QA-W8F2-D3: the probe remembers the DB "
                   "file only on the probe thread's own first connection; a swap "
                   "that happens before any probe ran is invisible")
def test_qa_w8f2_defect_d3_replaced_file_detected_without_prior_probe(tmp_path, monkeypatch):
    """DEFECT D3 (minor). Request threads keep writing to the OLD, now
    unlinked inode after a restore/copy-over; the data never reaches the
    path. ``readiness_probe`` says OK when it was never called before the
    swap (and ``db_file_replaced`` when it was), so /readyz is green while
    writes are being lost."""
    repo = _plain_repo(tmp_path, monkeypatch)
    path = tmp_path / "vf.db"
    with repo.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS t(x)")
        c.execute("INSERT INTO t VALUES (1)")
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(path) + ".new")
    src.backup(dst)
    src.close()
    dst.close()
    for suf in ("-wal", "-shm"):
        try:
            os.unlink(str(path) + suf)
        except FileNotFoundError:
            pass
    os.replace(str(path) + ".new", str(path))
    with repo.tx() as c:                       # lands on the unlinked inode
        c.execute("INSERT INTO t VALUES (2)")
    assert sqlite3.connect(str(path)).execute("SELECT count(*) FROM t").fetchone()[0] == 1
    pool = ThreadPoolExecutor(max_workers=1)   # what create_app does
    try:
        assert pool.submit(repo.readiness_probe).result() == "db_file_replaced"
    finally:
        pool.shutdown()


def test_qa_w8f2_guard_probe_follows_a_real_key_rotation(tmp_path, monkeypatch):
    """GUARD N2. Real ``key_rotation.rotate`` (file swap + epoch bump), with
    the probe thread already armed on the OLD file: the probe must go back
    to OK, from the dedicated thread, afterwards. A manual ``_epoch += 1``
    (what the dev test does) does not prove the real wiring."""
    monkeypatch.delenv("VIDEO_FACTORY_DB_KEY", raising=False)
    from core.repo import Repo
    from control_plane import key_rotation
    path = str(tmp_path / "vf.db")
    repo = Repo(db_path=path, db_key="old-key")
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        assert pool.submit(repo.readiness_probe).result() is None   # armed on OLD inode
        st = key_rotation.rotate(repo=repo, old_key="old-key", new_key="new-key",
                                 actor_user_id="qa-actor")
        assert st["completed"] is True
        # from the same dedicated thread, repeatedly: never a stuck "replaced"
        assert [pool.submit(repo.readiness_probe).result() for _ in range(3)] == [None] * 3
        # and a real write through a request thread still works afterwards
        with repo.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS qa_t(x)")
    finally:
        pool.shutdown()


def test_qa_w8f2_guard_readyz_503_never_echoes_path_or_key_and_recovers(tmp_path, monkeypatch):
    """GUARD N2. Through the real app: deleted DB -> 503 ``db_file_missing``
    and the body leaks neither the filesystem path nor the DB key; a
    different, unrelated check keeps its own verdict."""
    from starlette.testclient import TestClient
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", VALID_FERNET)
    monkeypatch.setenv("VIDEO_FACTORY_DB_KEY", DB_KEY)
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("VIDEO_FACTORY_ENV", raising=False)
    from api.main import create_app
    client = TestClient(create_app())
    assert client.get("/readyz").status_code == 200
    for suf in ("", "-wal", "-shm"):
        try:
            os.unlink(f"{tmp_path}/vf.db{suf}")
        except FileNotFoundError:
            pass
    r = client.get("/readyz")
    assert r.status_code == 503
    j = r.json()
    assert j["checks"]["db"] is False and j["errors"]["db"] == "db_file_missing"
    assert str(tmp_path) not in r.text and DB_KEY not in r.text
    assert j["checks"]["vault_key"] is True


def test_qa_w8f2_guard_readyz_never_blocks_the_loop_while_probes_queue_behind_a_lock(tmp_path, monkeypatch):
    """GUARD N2. 10 concurrent /readyz while another connection holds the
    write lock: every answer is 503 ``db_locked`` (never an error class),
    ``/healthz`` stays fast while they queue on the single probe thread,
    and the first probe after the lock is released is 200 again."""
    import httpx
    repo = _plain_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("VIDEO_FACTORY_SECRET_KEY", VALID_FERNET)
    monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path / "vf.db"))
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("VIDEO_FACTORY_ENV", raising=False)
    from api.main import create_app
    app = create_app(repo=repo)
    seen_threads: list[int] = []
    real_probe = repo.readiness_probe

    def spy(*a, **kw):
        seen_threads.append(threading.get_ident())
        return real_probe(*a, **kw)

    repo.readiness_probe = spy            # instance attribute wins over the method
    loop_thread = threading.get_ident()   # asyncio.run below runs on this thread

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t", timeout=30) as c:
            assert (await c.get("/readyz")).status_code == 200
            other = sqlite3.connect(str(tmp_path / "vf.db"), isolation_level=None, timeout=0)
            other.execute("BEGIN EXCLUSIVE")
            try:
                probes = [asyncio.create_task(c.get("/readyz")) for _ in range(10)]
                await asyncio.sleep(0.05)
                t0 = time.perf_counter()
                hz = await c.get("/healthz")
                hz_ms = (time.perf_counter() - t0) * 1000
                results = await asyncio.gather(*probes)
            finally:
                other.execute("ROLLBACK")
                other.close()
            after = await c.get("/readyz")
            return results, hz.status_code, hz_ms, after

    results, hz_status, hz_ms, after = asyncio.run(scenario())
    assert {r.status_code for r in results} == {503}
    assert {r.json()["errors"]["db"] for r in results} == {"db_locked"}
    assert hz_status == 200 and hz_ms < 400, f"/healthz stalled {hz_ms:.0f} ms behind queued probes"
    assert after.status_code == 200
    # every probe ran off the event-loop thread, all on the SAME dedicated thread
    assert len(seen_threads) == 12                       # 1 warm + 10 locked + 1 after
    assert loop_thread not in seen_threads
    assert len(set(seen_threads)) == 1, "probe connection is not reused across probes"


def test_qa_w8f2_guard_probe_does_not_starve_a_concurrent_writer(tmp_path, monkeypatch):
    """GUARD N2. The probe takes the write lock (BEGIN IMMEDIATE) on every
    call. A tight probe loop on its own thread must not make another
    thread's real writes fail or crawl."""
    repo = _plain_repo(tmp_path, monkeypatch)
    with repo.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS w(x)")
    stop = threading.Event()
    probe_results: list = []

    def prober():
        while not stop.is_set():
            probe_results.append(repo.readiness_probe(busy_timeout_ms=100))

    th = threading.Thread(target=prober)
    th.start()
    try:
        t0 = time.perf_counter()
        for i in range(150):
            with repo.tx() as c:
                c.execute("INSERT INTO w VALUES (?)", (i,))
        elapsed = time.perf_counter() - t0
    finally:
        stop.set()
        th.join(5)
    assert not th.is_alive()
    # NOT asserted: how many probes ran or how many said db_locked. Against a
    # saturated writer the probe legitimately loses the lock race (that is
    # observation O-2 in the QA report), and the count depends on disk speed.
    assert probe_results
    assert elapsed < 30.0, f"150 writes took {elapsed:.1f}s next to a probe loop"
    with repo.tx() as c:
        assert c.execute("SELECT count(*) FROM w").fetchone()[0] == 150
    # a probe never reports a lock held by itself, and leaves none behind
    assert set(probe_results) <= {None, "db_locked"}
    assert repo._conn().in_transaction is False  # noqa: SLF001


# ────────────────────────────────────────────────────────────────────
# QA-N4: docs vs /metrics, strict in both directions
# ────────────────────────────────────────────────────────────────────

def test_qa_w8f2_guard_docs_name_every_emitted_series_exactly():
    """GUARD N4, stricter than the QA pin (which accepts a bare substring or
    any prose that mentions counters and ``_created``). Exercise EVERY
    metric with EVERY label dimension, then require each emitted sample
    name to be a backticked token in docs/observability.md, and each
    ``video_factory_*`` series token in the docs to exist."""
    obs.JOBS_TOTAL.labels(state="done").inc()
    obs.LIVE_SUBMITS_TOTAL.labels(provider="mock", outcome="ok").inc()
    obs.WEBHOOK_RECEIPTS_TOTAL.labels(source="stripe").inc()
    obs.ERRORS_TOTAL.labels(**{"class": "runtime"}).inc()
    obs.REQUEST_DURATION.labels(route="/healthz", method="GET", status="200").observe(0.01)
    text = obs.metrics_response().decode()
    samples = set(re.findall(r"^(video_factory_[A-Za-z0-9_:]+)[{ ]", text, flags=re.M))
    assert samples, "no video_factory_ samples emitted"
    doc = (REPO_ROOT / "docs" / "observability.md").read_text()
    tokens = set(re.findall(r"`(video_factory_[A-Za-z0-9_]+)`", doc))
    group = {"_bucket", "_count", "_sum"}

    def documented(name: str) -> bool:
        if name in tokens:
            return True
        for suf in group:                       # documented as a group for the histogram
            if name.endswith(suf) and name[: -len(suf)] in tokens:
                return True
        return False

    missing = sorted(n for n in samples if not documented(n))
    assert not missing, f"emitted, not documented as a `token`: {missing}"
    # reverse: no stale / misspelled series name in the docs
    emitted_bases = {re.sub(r"_(bucket|count|sum)$", "", n) for n in samples} | samples
    stale = sorted(t for t in tokens if t not in emitted_bases)
    assert not stale, f"documented but never emitted: {stale}"


# ────────────────────────────────────────────────────────────────────
# QA-N7: production compose
# ────────────────────────────────────────────────────────────────────

def _api_environment() -> list[tuple[str, str]]:
    text = (REPO_ROOT / "docker-compose.production.yml").read_text().splitlines()
    out: list[tuple[str, str]] = []
    in_api = in_env = False
    for ln in text:
        if re.match(r"^  api:\s*$", ln):
            in_api, in_env = True, False
            continue
        if in_api and re.match(r"^  [A-Za-z0-9_-]+:\s*$", ln):
            break
        if in_api and re.match(r"^    environment:\s*$", ln):
            in_env = True
            continue
        if in_env:
            if re.match(r"^    \S", ln):          # next sibling key of `environment`
                in_env = False
                continue
            m = re.match(r"^      ([A-Za-z0-9_]+):\s*(.*?)\s*$", ln)
            if m:
                out.append((m.group(1), m.group(2)))
    return out


def test_qa_w8f2_guard_compose_api_env_no_duplicates_optional_sentry_secrets_intact():
    """GUARD N7. Duplicate YAML keys are rejected by compose (hence the
    deliberate OPS S2 conflict); Sentry vars must be OPTIONAL (``:-``,
    not ``:?``) so an operator without Sentry still boots; the three
    required secrets must keep their ``:?`` guard (no weakening)."""
    env = _api_environment()
    assert env, "could not parse the api environment block"
    keys = [k for k, _ in env]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    assert not dup, f"duplicate keys: {dup}"
    d = dict(env)
    assert d["VIDEO_FACTORY_ENV"].strip("'\"") == "production"
    assert re.fullmatch(r"\$\{SENTRY_DSN:-\}", d["SENTRY_DSN"])
    assert re.fullmatch(r"\$\{SENTRY_ENVIRONMENT:-[^}]*\}", d["SENTRY_ENVIRONMENT"])
    assert re.fullmatch(r"\$\{SENTRY_RELEASE:-[^}]*\}", d["SENTRY_RELEASE"])
    for k in ("VIDEO_FACTORY_SECRET_KEY", "VIDEO_FACTORY_DB_KEY", "VIDEO_FACTORY_SESSION_SECRET",
              "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
        assert f"${{{k}:?" in d[k], f"{k} lost its required-var guard"
    # the python DSN must not be shipped to the web service
    text = (REPO_ROOT / "docker-compose.production.yml").read_text()
    web = text.split("\n  web:", 1)[1] if "\n  web:" in text else ""
    assert "SENTRY_DSN" not in web


# ────────────────────────────────────────────────────────────────────
# QA-N1 live: real uvicorn + fake Sentry, hostile 500s
# ────────────────────────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _FakeSentry:
    def __init__(self):
        self.bodies: list[bytes] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("content-length", 0))
                b = self.rfile.read(n)
                if self.headers.get("content-encoding") == "gzip":
                    b = gzip.decompress(b)
                outer.bodies.append(b)
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):  # noqa: D401
                pass

        self.port = _free_port()
        self.srv = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def raw(self) -> str:
        return b"\n".join(self.bodies).decode("utf-8", "replace")

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


_BOOM_APP = textwrap.dedent('''
    from api.main import app

    @app.get("/__qa/boom")
    async def _boom():
        raise RuntimeError("boom")

    @app.get("/__qa/boom_long")
    async def _boom_long(n: int = 1000, kind: str = "a"):
        pad = {"a": "a", "at": "a@", "pct": "a%40", "dot": "a."}[kind]
        raise RuntimeError("boom contact zed@leakcheck.example " + pad * n)
''')


def test_qa_w8f2_live_hostile_500s_do_not_stall_the_loop_and_leak_nothing(tmp_path):
    """GUARD N1/N3, black-box. Real ``python -m uvicorn`` with Sentry on
    (fake local ingest). Hostile 500s: 200 KB exception message made of
    ``a@`` pairs, 15 KB query of ``a@`` pairs with a %40 email, 400 KB
    ``a%40`` message. Each must answer 500 quickly (the old code needs
    seconds to minutes), ``/healthz`` must not stall meanwhile, and the
    bytes Sentry received must contain no email, plain or encoded.

    The server's stdout/stderr go to FILES, not pipes: the access line
    carries the whole query, and an undrained pipe blocks the loop after
    ~64 KB (that is a harness artefact, not the product)."""
    (tmp_path / "qa_boom_app.py").write_text(_BOOM_APP)
    sentry = _FakeSentry()
    port = _free_port()
    env = dict(os.environ)
    for k in ("VIDEO_FACTORY_ENV", "SENTRY_DSN", "VF_HTTPS"):
        env.pop(k, None)
    env.update({
        "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{tmp_path}",
        "PYTHONUNBUFFERED": "1",
        "VIDEO_FACTORY_DB": str(tmp_path / "vf.db"),
        "VIDEO_FACTORY_STORAGE": str(tmp_path / "storage"),
        "VIDEO_FACTORY_SECRET_KEY": VALID_FERNET,
        "VIDEO_FACTORY_DB_KEY": DB_KEY,
        "VIDEO_FACTORY_SESSION_SECRET": SESSION_SECRET,
        "SENTRY_DSN": f"http://k@127.0.0.1:{sentry.port}/1",
    })
    out_f = open(tmp_path / "stdout.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "qa_boom_app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-config", "api/uvicorn_log_config.json"],
        cwd=str(REPO_ROOT), env=env, stdout=out_f, stderr=subprocess.STDOUT)

    def req(path, timeout=20):
        t0 = time.perf_counter()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", path)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status, (time.perf_counter() - t0) * 1000

    try:
        deadline = time.time() + 40
        while True:
            assert time.time() < deadline and proc.poll() is None, "uvicorn did not start"
            try:
                if req("/healthz")[0] == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        req("/__qa/boom")                                     # warm the Sentry client
        cases = [
            "/__qa/boom_long?n=100000&kind=at",
            "/__qa/boom_long?n=100000&kind=pct",
            "/__qa/boom?x=" + "a@" * 7500 + "&contact=zed%40leakcheck.example",
        ]
        for path in cases:
            hz: list[float] = []
            stop = threading.Event()

            def spam():
                while not stop.is_set():
                    hz.append(req("/healthz")[1])
                    time.sleep(0.03)

            th = threading.Thread(target=spam)
            th.start()
            try:
                status, ms = req(path)
            finally:
                stop.set()
                th.join()
            assert status == 500, path[:40]
            assert ms < 5000, f"{path[:40]}... took {ms:.0f} ms"
            assert hz and max(hz) < 2500, f"/healthz stalled {max(hz):.0f} ms during {path[:40]}"
        time.sleep(3)
        raw = sentry.raw()
        assert sentry.bodies, "Sentry received nothing; the test proves nothing"
        for needle in ("leakcheck", "zed@", "zed%40"):
            assert needle not in raw, needle
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(20)
            except subprocess.TimeoutExpired:
                proc.kill()
        out_f.close()
        sentry.close()
        shutil.rmtree(tmp_path / "storage", ignore_errors=True)
