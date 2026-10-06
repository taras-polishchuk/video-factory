"""Password hashing façade.

Re-exports the canonical password helpers from `auth/__init__.py`
so that callers can `from auth.passwords import hash_password`.
The actual implementation lives in `auth/__init__.py` so the
module is a single, focused surface.

We keep the implementation in `auth/__init__.py` (rather than
`auth/passwords.py`) because the helpers are pure and stateless;
putting them in `__init__.py` avoids the per-test re-import cost
and prevents the double-import confusion that comes with package
modules that grow into namespace packages.
"""
from auth import (  # noqa: F401
    PasswordHashingError,
    algorithm_of,
    hash_password,
    is_strong_password,
    verify_password,
)
