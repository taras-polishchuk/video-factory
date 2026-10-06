"""Role hierarchy + membership helpers.

Single source of truth for the `owner > admin > editor > viewer`
ordering. The API layer imports `require_min_role()` and
`has_min_role()` from here so the order cannot drift between
endpoints.

Roles:

- `owner` — created the workspace, can do anything including
  deleting the workspace and rotating the master key.
- `admin` — can manage members and credentials.
- `editor` — can create and submit jobs.
- `viewer` — can read; cannot write.
"""
from __future__ import annotations


ROLE_LEVELS = {
    "viewer": 1,
    "editor": 2,
    "admin": 3,
    "owner": 4,
}

ROLE_NAMES = tuple(ROLE_LEVELS.keys())


def has_min_role(actual: str, required: str) -> bool:
    """True iff `actual` is at least as privileged as `required`."""
    a = ROLE_LEVELS.get(actual, 0)
    r = ROLE_LEVELS.get(required, 0)
    return a >= r


def highest_role(roles: list[str]) -> str:
    """Return the most privileged role in the list, or 'viewer'."""
    best = "viewer"
    for r in roles:
        if ROLE_LEVELS.get(r, 0) > ROLE_LEVELS.get(best, 0):
            best = r
    return best


def is_valid_role(role: str) -> bool:
    return role in ROLE_LEVELS
