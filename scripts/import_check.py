#!/usr/bin/env python3
"""Import every application module; exit non-zero on the first failure.

Run by the Dockerfile at build time (A2 F-26 / staging VF-STG-01: the
old image could not even import api.main). Also usable on a host:

    PYTHONPATH=. VIDEO_FACTORY_DB=/tmp/x.db VIDEO_FACTORY_STORAGE=/tmp/x \
        python scripts/import_check.py

Covers every .py module under the application packages, plus the
third-party modules that are imported lazily (only on some code paths)
so a missing wheel is caught here and not on the first live request.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("api", "auth", "core", "control_plane", "render_plane", "assembly", "cli")
# Imported lazily inside functions: cipher DB, Sentry, password hashing,
# OIDC JOSE, Stripe. A top-level import of every app module does not
# reach these, so check them explicitly.
LAZY_THIRD_PARTY = (
    "sqlcipher3.dbapi2",
    "sentry_sdk",
    "argon2",
    "bcrypt",
    "joserfc.jwt",
    "stripe",
    "cryptography.fernet",
    "httpx",
    "multipart",
    "prometheus_client",
    "jsonschema",
)


def app_modules(root: Path = ROOT) -> list[str]:
    mods: list[str] = []
    for pkg in PACKAGES:
        for path in sorted((root / pkg).rglob("*.py")):
            rel = path.relative_to(root).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            mods.append(".".join(parts))
    return mods


def main() -> int:
    sys.path.insert(0, str(ROOT))
    names = list(LAZY_THIRD_PARTY) + app_modules()
    failed: list[tuple[str, str]] = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 (report every failure)
            failed.append((name, f"{type(exc).__name__}: {exc}"))
    for name, err in failed:
        print(f"IMPORT FAIL {name}: {err}", file=sys.stderr)
    ok = len(names) - len(failed)
    print(f"import-check: {ok}/{len(names)} modules OK")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
