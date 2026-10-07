"""One-time migration script: plaintext -> SQLCipher.

Wave 5 §3.3 — operators upgrading an existing plaintext deployment
to encrypted-at-rest run this once:

    VIDEO_FACTORY_DB_KEY=<newkey> python scripts/migrate_db_to_cipher.py

The script refuses to run unless:

  * the env var VIDEO_FACTORY_DB_KEY is set
  * VIDEO_FACTORY_DB points to a valid plaintext DB

It writes the cipher DB next to the plaintext file with a `.enc`
suffix.  The original plaintext file is NEVER deleted by this
script — the operator inspects the new cipher DB, then deletes
the plaintext manually.  Reversal is the separate
`decrypt_db_down.py`.

Idempotent: re-running with the same key + the original plaintext
intact will overwrite the previous `.enc` only if the operator
removes it first.  We never silently clobber work.

See core.cipher_db.migrate_plaintext_to_cipher for the actual
implementation.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _run() -> int:
    from core.cipher_db import (
        PlaintextDBRefused,
        detect_plaintext,
        migrate_plaintext_to_cipher,
    )

    key = os.environ.get("VIDEO_FACTORY_DB_KEY", "").strip()
    if not key:
        print(
            "ERROR: VIDEO_FACTORY_DB_KEY is not set.  Refusing to "
            "migrate without a key.  Generate one with "
            "`python -c 'import secrets; print(secrets.token_urlsafe(32))'` "
            "and re-run.",
            file=sys.stderr,
        )
        return 2

    plain = os.environ.get(
        "VIDEO_FACTORY_DB",
        str(Path(__file__).resolve().parent.parent
            / "artifacts" / "video_factory.db"),
    )
    if not Path(plain).exists():
        print(
            f"ERROR: {plain} does not exist.  Refusing to migrate.",
            file=sys.stderr,
        )
        return 2
    if not detect_plaintext(plain):
        print(
            f"ERROR: {plain} is not a plaintext SQLite DB.  If this is "
            f"already encrypted, no migration is needed.  Aborting.",
            file=sys.stderr,
        )
        return 2

    cipher = str(Path(plain).with_suffix(Path(plain).suffix + ".enc"))
    print(f"Migrating {plain} -> {cipher}")
    print(f"Key fingerprint: {fp(key)}")

    try:
        result = migrate_plaintext_to_cipher(
            plain_path=plain,
            cipher_path=cipher,
            key=key,
            verify=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"FAILED: {e}", file=sys.stderr)
        return 1

    print("OK")
    for t, counts in result.get("tables", {}).items():
        print(f"  {t}: {counts['plaintext_rows']} -> {counts['cipher_rows']}")
    print()
    print(f"Plaintext copy preserved at {plain}.  Inspect the cipher DB "
          f"at {cipher}, run a smoke test, and only then delete the "
          f"plaintext copy.  Reversal: python scripts/decrypt_db_down.py")
    return 0


def fp(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":
    sys.exit(_run())