"""Stripe SDK wrapper for Video Factory billing (Wave 6a).

A thin facade around the `stripe` Python SDK so the API layer can:

- read `STRIPE_SECRET_KEY` from env at module-import time
  (fail loud when missing, per the brief),
- create a Stripe Checkout Session for a workspace upgrade,
- create a Stripe Customer Portal session for an existing
  customer,
- be fully unit-testable by mocking `stripe.checkout.Session.create`
  and `stripe.billing_portal.Session.create`.

The wrapper is intentionally NOT a singleton: the API factory
calls ``get_stripe_client()`` to obtain a thin client, which is
cheap to build. Tests can call ``reset_stripe_client()`` to clear
the cached instance between tests.

Scope (Wave 6a only):

- ``create_checkout_session(workspace_id, plan, success_url, cancel_url)``
- ``create_portal_session(customer_id, return_url)``

Out of scope (deferred):

- Webhook signature verification (Wave 6c)
- ``stripe_events`` idempotency table (Wave 6c)
- Quota enforcement / live-mode gating (Wave 6b)

Why a thin wrapper and not a Stripe-backed dataclass:

- We need a single seam to monkey-patch in tests; the brief
  calls out `unittest.mock.patch('stripe.checkout.Session.create')`
  specifically.  Wrapping it in a method keeps the patch site
  stable even if Stripe reorganises the SDK.
- The wrapper also gives the API layer one place to enforce
  fail-loud-on-missing-key instead of waiting until the first
  live call.
"""
from __future__ import annotations

import os
from typing import Optional

import stripe
from stripe._error import StripeError


# ── Public constants ──────────────────────────────────────────

# Env var that holds the Stripe secret key (sk_test_* for dev).
STRIPE_SECRET_KEY_ENV = "STRIPE_SECRET_KEY"

# Default Price IDs by plan. Operators can override via env; the
# Wave 6a brief does not pin these, so we use placeholder IDs and
# let the deployment environment set them. Tests assert that
# whichever price_id is passed reaches the Stripe SDK.
DEFAULT_PRO_PRICE_ID = "price_pro_placeholder"
DEFAULT_ENTERPRISE_PRICE_ID = "price_enterprise_placeholder"


# ── Errors ─────────────────────────────────────────────────────


class StripeConfigError(RuntimeError):
    """Raised when STRIPE_SECRET_KEY is missing at import time.

    Per the brief: "Stripe API key read from STRIPE_SECRET_KEY env;
    fail loud if missing at boot when running python -m api.billing".
    """


class StripeCallError(RuntimeError):
    """Raised when the Stripe SDK returns an unexpected shape.

    Catches ``stripe.error.StripeError`` at the wrapper boundary so
    the API layer can decide how to surface the failure (500 vs
    503 vs user-friendly 400).
    """


# ── Client wrapper ────────────────────────────────────────────


class StripeClient:
    """Thin wrapper around the official `stripe` Python SDK.

    Holds the configured API key + default price ids.  Methods
    return plain Python dicts shaped for the API response body.
    """

    def __init__(self, *, api_key: str,
                 pro_price_id: Optional[str] = None,
                 enterprise_price_id: Optional[str] = None) -> None:
        if not api_key:
            raise StripeConfigError(
                f"missing Stripe API key (set {STRIPE_SECRET_KEY_ENV})"
            )
        self._api_key = api_key
        self._pro_price_id = pro_price_id or os.environ.get(
            "STRIPE_PRO_PRICE_ID", DEFAULT_PRO_PRICE_ID,
        )
        self._enterprise_price_id = enterprise_price_id or os.environ.get(
            "STRIPE_ENTERPRISE_PRICE_ID", DEFAULT_ENTERPRISE_PRICE_ID,
        )

    @property
    def api_key(self) -> str:
        return self._api_key

    def price_id_for_plan(self, plan: str) -> str:
        """Return the configured Stripe Price ID for a plan name.

        Unknown plan names raise ValueError — the API layer
        translates to 400.
        """
        if plan == "pro":
            return self._pro_price_id
        if plan == "enterprise":
            return self._enterprise_price_id
        raise ValueError(f"unknown plan: {plan!r}")

    def create_checkout_session(
        self,
        *,
        workspace_id: str,
        plan: str,
        success_url: str,
        cancel_url: str,
        customer_id: Optional[str] = None,
    ) -> dict:
        """Create a Stripe Checkout Session for a workspace upgrade.

        Returns ``{"url": "...", "session_id": "cs_test_...",
        "customer_id": "cus_..."}``.  When ``customer_id`` is
        None, the SDK will create a fresh Customer on the session
        (and the response's ``customer`` field will hold the new
        customer id).

        For testability, this method calls ``stripe.checkout.Session.create``
        with the configured API key. Tests patch that symbol.
        """
        if plan not in ("pro", "enterprise"):
            raise ValueError(
                f"checkout requires plan='pro' or 'enterprise', got {plan!r}"
            )
        price_id = self.price_id_for_plan(plan)
        params = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            # client_reference_id is what our webhook (Wave 6c)
            # will use to map back to a workspace_id.
            "client_reference_id": workspace_id,
            "metadata": {"workspace_id": workspace_id, "plan": plan},
        }
        if customer_id:
            params["customer"] = customer_id
        try:
            # Pin the API key for this call so tests that patch
            # ``stripe.checkout.Session.create`` don't need to also
            # configure the global module key.
            stripe.api_key = self._api_key
            sess = stripe.checkout.Session.create(**params)
        except StripeError as e:
            raise StripeCallError(str(e)) from e
        return {
            "url": getattr(sess, "url", None) or "",
            "session_id": getattr(sess, "id", None) or "",
            "customer_id": getattr(sess, "customer", None) or "",
        }

    def create_portal_session(
        self,
        *,
        customer_id: str,
        return_url: str,
    ) -> dict:
        """Create a Stripe Customer Portal session.

        Returns ``{"url": "...", "session_id": "bps_..."}``.
        Requires a real Stripe customer id; the API layer maps
        'no customer yet' to 404.
        """
        if not customer_id:
            raise ValueError("customer_id required for portal session")
        try:
            stripe.api_key = self._api_key
            sess = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=return_url,
            )
        except StripeError as e:
            raise StripeCallError(str(e)) from e
        return {
            "url": getattr(sess, "url", None) or "",
            "session_id": getattr(sess, "id", None) or "",
        }


# ── Module-level client cache ─────────────────────────────────

_client: Optional[StripeClient] = None


def get_stripe_client() -> StripeClient:
    """Return the process-singleton StripeClient.

    Reads ``STRIPE_SECRET_KEY`` from the environment on first
    call.  Subsequent calls return the cached instance.  Tests
    call ``reset_stripe_client()`` to drop the cache between
    cases.
    """
    global _client
    if _client is None:
        key = os.environ.get(STRIPE_SECRET_KEY_ENV)
        if not key:
            raise StripeConfigError(
                f"missing Stripe API key (set {STRIPE_SECRET_KEY_ENV})"
            )
        _client = StripeClient(api_key=key)
    return _client


def reset_stripe_client() -> None:
    """Drop the cached StripeClient.  Test helper only."""
    global _client
    _client = None


def configure_stripe_client(api_key: str,
                              pro_price_id: Optional[str] = None,
                              enterprise_price_id: Optional[str] = None
                              ) -> StripeClient:
    """Build + cache a StripeClient from explicit args.  Test helper."""
    global _client
    _client = StripeClient(
        api_key=api_key,
        pro_price_id=pro_price_id,
        enterprise_price_id=enterprise_price_id,
    )
    return _client


# ── CLI smoke entry point ─────────────────────────────────────


def _main() -> int:  # pragma: no cover - CLI smoke only
    """CLI smoke: ``python -m api.billing`` exits non-zero if the
    Stripe API key is missing.  Per the brief.
    """
    try:
        client = get_stripe_client()
    except StripeConfigError as e:
        print(f"api.billing: FATAL {e}", flush=True)
        return 2
    print(f"api.billing: OK api_key={client.api_key[:8]}*** "
          f"pro_price_id={client.price_id_for_plan('pro')}",
          flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())