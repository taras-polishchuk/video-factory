"""Keyset pagination helpers for list endpoints (MAJ-4).

List endpoints (jobs, assets, audit, publish intents) return the same
JSON array as before; the page boundary travels out-of-band:

* ``?limit=N``   page size, 1..MAX_PAGE_SIZE (larger values are clamped
  to MAX_PAGE_SIZE, values < 1 are rejected with 400).
* ``?cursor=C``  opaque cursor from the previous page.
* ``X-Next-Cursor`` response header: present only when more rows exist.

Without ``limit`` the endpoint returns the newest ``default`` rows
(200 for jobs/assets/publish intents, 100 for audit, matching the old
audit default), so the UI (which never paginates) sees the same array
for any workspace with fewer rows than that.

Ordering is ``(<timestamp> DESC, rowid DESC)``. ISO timestamps have
one-second resolution, so ``rowid`` breaks ties and makes the order
total: no row is skipped or repeated between pages.
"""
from __future__ import annotations

import base64
import json
from typing import Optional, Tuple

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 200
NEXT_CURSOR_HEADER = "X-Next-Cursor"


class InvalidCursor(ValueError):
    """Malformed ``cursor`` / ``limit`` (the API maps ValueError to 400)."""


def clamp_limit(limit: Optional[int], *, default: int = DEFAULT_PAGE_SIZE) -> int:
    if limit is None:
        return min(int(default), MAX_PAGE_SIZE)
    try:
        n = int(limit)
    except (TypeError, ValueError):
        raise InvalidCursor(f"limit must be an integer, got {limit!r}")
    if n < 1:
        raise InvalidCursor("limit must be >= 1")
    return min(n, MAX_PAGE_SIZE)


def encode_cursor(ts: str, rowid: int) -> str:
    raw = json.dumps([ts, int(rowid)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: Optional[str]) -> Optional[Tuple[str, int]]:
    if cursor is None or cursor == "":
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        ts, rowid = json.loads(base64.urlsafe_b64decode(cursor + pad))
        if not isinstance(ts, str) or not isinstance(rowid, int):
            raise TypeError
        return ts, rowid
    except Exception:
        raise InvalidCursor("invalid cursor")


def keyset_page(conn, *, select: str, where: str, params: list,
                ts_col: str, limit: int, cursor: Optional[str],
                row_fn) -> Tuple[list, Optional[str]]:
    """Run one keyset page.

    ``select`` is ``"SELECT <cols> FROM <table>"``; the helper appends
    ``rowid`` as the last selected column. ``where`` is a SQL predicate
    (without ``WHERE``) over ``params``. ``row_fn`` maps the row minus
    the trailing rowid to a dict. Returns ``(items, next_cursor)``.
    """
    q_where = where or "1=1"
    q_params = list(params)
    after = decode_cursor(cursor)
    if after is not None:
        q_where += f" AND ({ts_col} < ? OR ({ts_col} = ? AND rowid < ?))"
        q_params += [after[0], after[0], after[1]]
    sql = (
        select.replace(" FROM ", ", rowid FROM ", 1)
        + f" WHERE {q_where} ORDER BY {ts_col} DESC, rowid DESC LIMIT ?"
    )
    rows = conn.execute(sql, (*q_params, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [row_fn(r[:-1]) for r in rows]
    nxt = None
    if more and rows:
        last = rows[-1]
        ts_index = _ts_index(select, ts_col)
        nxt = encode_cursor(last[ts_index], last[-1])
    return items, nxt


def _ts_index(select: str, ts_col: str) -> int:
    cols = select.split(" FROM ", 1)[0]
    cols = cols[len("SELECT "):] if cols.upper().startswith("SELECT ") else cols
    names = [c.strip().split()[-1].split(".")[-1] for c in cols.split(",")]
    return names.index(ts_col.split(".")[-1])
