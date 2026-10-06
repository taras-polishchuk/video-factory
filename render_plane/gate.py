"""Single gate for live-provider access.

Replaces the per-adapter "build body then unconditionally raise" pattern.
A live adapter that respects this gate:

  def submit(self, ...):
      ensure_live_unlocked("heygen_direct")  # raises ProviderDisabled if locked
      # ... actually call the provider

This module owns the LIVE_PROVIDER_TESTS + SMOKE_TEST_PASSED semantics.
Adapters only need to call `ensure_live_unlocked(profile)`.

Why one gate:

* Per-adapter duplication of the gate logic drifted (audit §2.1): the
  bodies were built then `raise ProviderDisabled` was unconditional,
  which made the gate read "an env var could enable me" while the code
  said "no env var enables me". One module, one truth.

* Audit + future tests can probe `live_path_unlocked()` directly.

* Operators flip `SMOKE_TEST_PASSED=true` once after a dedicated smoke
  test on a separate (non-billing) account confirms the contract.
  After that, the existing `LIVE_PROVIDER_TESTS=true` enables the live
  path with the BudgetGuard preflight still refusing on cost overruns.
"""

from __future__ import annotations

import os

from control_plane.budget import live_path_unlocked
from control_plane.errors import ProviderDisabled


def ensure_live_unlocked(profile: str) -> None:
    """Raise ProviderDisabled unless LIVE_PROVIDER_TESTS=true AND SMOKE_TEST_PASSED=true.

    Both flags are read from env at call time (L-12). No mutation of
    state. Safe to call repeatedly; the gate is idempotent.
    """
    if live_path_unlocked():
        return
    live_set = os.environ.get("LIVE_PROVIDER_TESTS", "").lower() == "true"
    smoke_set = os.environ.get("SMOKE_TEST_PASSED", "").lower() == "true"
    if not live_set and not smoke_set:
        raise ProviderDisabled(
            f"{profile}: set LIVE_PROVIDER_TESTS=true AND SMOKE_TEST_PASSED=true "
            f"to enable live submit. Default is offline/mock-only."
        )
    if not live_set:
        raise ProviderDisabled(
            f"{profile}: LIVE_PROVIDER_TESTS=true required (SMOKE_TEST_PASSED is set)."
        )
    if not smoke_set:
        raise ProviderDisabled(
            f"{profile}: SMOKE_TEST_PASSED=true required after a dedicated "
            f"smoke test on a separate account confirmed the contract. "
            f"LIVE_PROVIDER_TESTS=true is set but the smoke gate is not."
        )