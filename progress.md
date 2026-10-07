# Wave 5 follow-up — progress log (t_abf264b4)

## Summary

Fixed 2 BLOCKER + 3 MAJOR defects in the Wave 5 SQLCipher + master-key
rotation flow. All 6 QA adversarial tests now pass (was 5 pass + 1 fail).
Added 8 new follow-up tests pinning the fix in place.

## Test results

```
tests/test_wave5_qa.py: 14/14 passed (was 5/6)
tests/test_wave5_cipher.py: 20/20 passed
Full suite: 268 passed + 1 pre-existing fail + 3 skipped = 272 total
```

The 1 pre-existing failure is `test_existing_211_tests_still_green` in
`tests/test_wave4_oidc.py:755` which depends on `tests/test_wave3_qa.py`
not being on this branch. It was failing before my changes (verified
on the baseline run) and is out of Wave 5 scope.

## Defects fixed

### BLOCKER-1: Repo.db_key not refreshed after rotation swap

**File:line**: `core/repo.py:286-291` (`_conn` opens with `self.db_key`)
and `control_plane/key_rotation.py:444-450` (rotation's `db_key = new_key`
+ `close()` step).

**Fix**: After `os.replace` the rotation function:
1. Sets `repo.db_key = new_key` (so the next `_conn()` opens with the NEW
   key against the NEW-cipher file).
2. Calls `repo.close()` (so the cached OLD-key per-thread connection is
   dropped before any thread can reuse it).

### BLOCKER-2: row-count verify false-negative under concurrent writers

**File:line**: `control_plane/key_rotation.py:299-303` (whole-body lock
acquisition).

**Fix**: The entire `rotate()` body now runs under `repo._lock` (an
`RLock`). The OLD-row-counts snapshot, the copy, and the verify all
execute inside the critical section; no other `Repo.tx()` can interleave.
The same lock also addresses BLOCKER-1 by serialising all writes
through the Repo during the swap.

### MAJOR-1: returned state said completed=True but DB row said in_progress=1

**File:line**: `control_plane/key_rotation.py:454-462` (post-swap
`UPDATE key_rotation SET rotation_in_progress=0, rotation_completed=1`
runs through `repo.tx()` which now opens with the NEW key).

**Fix**: Resolves automatically with BLOCKER-1. The post-swap UPDATE
now uses a connection that has PRAGMA-key'd with the NEW key against
the NEW-cipher file, so the row is actually written.

### MAJOR-2: runner_audit in NEW-cipher DB only had `key.rotation_started`

**File:line**: `control_plane/key_rotation.py:464-466` (the
`_audit(repo, event=EVENT_COMPLETED, ...)` call after the swap).

**Fix**: Same root cause as MAJOR-1. The audit call now writes
through a connection that PRAGMA-key'd with the NEW key, so the
`key.rotation_completed` row lands in the NEW-cipher file.

### MAJOR-3: only `Repo.upsert_credential` was gated by rotation

**File:line**: `core/repo.py:889-907` (added `writes_allowed` check to
`Repo.tx()`).

**Fix**: `Repo.tx()` now checks `key_rotation.writes_allowed(self)` at
the top. Any write through the canonical `with repo.tx() as c:` path
raises `RotationInProgress` while the flag is set. `Repo.upsert_credential`
keeps its own gate so the API's 503 mapping still fires. Internal
callers (e.g. `key_rotation.rotate` itself) pass
`_skip_rotation_check=True` so the rotation can write its own
bookkeeping rows.

## New tests added (8)

In `tests/test_wave5_qa.py` after the original 6 adversarial tests:

7. `test_repo_db_key_is_refreshed_after_rotation`
8. `test_repo_uses_new_key_for_post_rotation_writes`
9. `test_rotation_state_actually_clears_in_progress_flag`
10. `test_runner_audit_has_both_started_and_completed_events`
11. `test_writes_allowed_returns_false_during_rotation`
12. `test_upsert_credential_still_raises_during_rotation`
13. `test_concurrent_writers_blocked_during_rotation` (5rps manual
    harness equivalent)
14. `test_rotation_failure_does_not_corrupt_repo_state`

## Changed files

- `core/repo.py` — added `tx()` rotation check + `_skip_rotation_check`
  kwarg
- `control_plane/key_rotation.py` — hold `repo._lock` for whole
  rotation; refresh `db_key` + `close()` after swap; added
  `writes_allowed()` (kept `credential_writes_allowed` as alias)
- `tests/test_wave5_qa.py` — 8 new tests
- `tests/test_wave5_cipher.py` — fixed `test_key_rotation_concurrent_reads_blocked`
  mock to produce a realistic NEW-cipher file (the original mock wrote
  an empty file, which the new `tx()` post-swap UPDATE could not write
  to because it opens with the NEW key)

## Out of scope

- `tests/test_wave3_qa.py` is missing from this branch (was untracked
  in the main repo). The pre-existing `test_existing_211_tests_still_green`
  test fails because of this dependency. Not touched.
- Wave 1..4 tests untouched.
- No commits to remote.
- No new dependencies added.

---

# Wave 5 follow-up-2 — progress log (t_282da1ca)

## Summary

Fixed the cross-thread connection-cache BLOCKER uncovered by QA
in t_68649331. After a master-key rotation, threads that had
called `repo._conn()` before the swap kept their stale OLD-key
per-thread connections, causing either permanent
`RotationInProgress` (Symptom A) or silent data corruption via
`repo._conn().execute()` bypass (Symptom B). Now every thread
closes-and-rebuilds on its next `_conn()` access via a single
Repo-level epoch counter.

## Test results

```
tests/test_wave5_followup_xthread.py: 2/2 passed (was 0/2 — both reproduced the BLOCKER pre-fix)
tests/test_wave5_qa.py:              14/14 passed (unchanged)
tests/test_wave5_cipher.py:          20/20 passed (unchanged)
Full suite:                          270 passed + 1 pre-existing fail + 3 skipped = 274 total
```

The 1 pre-existing failure is the same `test_existing_211_tests_still_green`
in `tests/test_wave4_oidc.py:755` flagged by the prior t_68649331
QA report — depends on `tests/test_wave3_qa.py` (untracked, not on
this branch). Was failing on baseline; not a Wave 5 regression.

## Fix (Option A — epoch counter)

### Symptom A — permanent RotationInProgress

`repo._conn()` on the writer thread returned a connection that
was PRAGMA-key'd with the OLD key before the swap. That stale
conn read the `key_rotation` table as it stood on its OWN cipher
state — which still said `rotation_in_progress=1` because the
post-swap `UPDATE` was issued by the rotating thread's NEW-key
conn (different cipher state). Every `repo.tx()` from the writer
therefore raised `RotationInProgress` forever.

### Symptom B — silent corruption via `_conn()` bypass

If a writer bypassed `tx()` and called `repo._conn().execute()`
directly, writes were encrypted with the OLD key into the
NEW-cipher file. A fresh NEW-key connection found zero rows.

### Resolution

Add `Repo._epoch: int` and per-thread `_local.conn_epoch` in
`core/repo.py`. In `_conn()`, if the cached per-thread conn was
opened at an earlier epoch, close it and rebuild with
`self.db_key` (the only sqlcipher3-documented way to switch the
page-encryption key on a connection). In
`control_plane/key_rotation.py`, increment `repo._epoch` after
assigning `repo.db_key = new_key` and before `repo.close()` so
every other thread's next access picks up the new key.

Trade-off vs. Option B (walk `threading.enumerate()`): no global
conn registry needed, no risk of missing threads, identical cost
(microseconds for the rebuild on the next access). Trade-off vs.
Option C (refuse writes + supervisor restart): preserves the
"rotate without downtime" invariant from the brief.

## Files changed

- `core/repo.py` — added `self._epoch`, `_local.conn_epoch`
  tracking, drop-stale-conn path in `_conn()`
- `control_plane/key_rotation.py` — increment `repo._epoch`
  immediately after `repo.db_key = new_key` and before
  `repo.close()`
- `tests/test_wave5_followup_xthread.py` — cherry-picked QA's 2
  adversarial cross-thread tests; replaced the hardcoded
  `WORKTREE` sys.path with a self-resolving parent-dir fallback
  so the file runs from any checkout.

## Stability

Cross-thread tests ran deterministically 3/3 (4.36s, 4.47s,
4.75s).

## Out of scope

- `tests/test_wave3_qa.py` still missing on this branch — same
  pre-existing condition as before.
- Wave 1..4 tests untouched.
- No commits to remote.
- No new dependencies added.
