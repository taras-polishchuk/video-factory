"""Stripe webhook handler + idempotency (Wave 6c).

Receives events from Stripe, verifies the signature using
``STRIPE_WEBHOOK_SECRET`` via the official ``stripe.Webhook.construct_event``
helper, and translates the four billing events we care about into
workspace plan / customer_id / subscription_id / current_period_end
updates.  Every event is recorded in the ``stripe_events`` table
BEFORE processing for idempotency, then stamped with
``processed_at`` after success.

Stripe-side expectations (https://stripe.com/docs/webhooks/signatures):

  * The handler MUST verify the ``Stripe-Signature`` header on every
    request. We refuse the request (401) if the signature is missing
    or invalid.
  * Stripe expects a 2xx response for any event it sends. We use
    ``200 {duplicate: true}`` for replays, ``200 {ignored: true}`` for
    events we don't act on, and ``200 {ok: true}`` for events we
    processed.
  * We deliberately do NOT raise 5xx for unknown event types: Stripe
    would retry them and we'd loop forever.

Config:

  * ``STRIPE_WEBHOOK_SECRET`` env var (required at boot). The handler
    refuses ALL requests if this is unset.
  * ``STRIPE_PRICE_PRO`` and ``STRIPE_PRICE_ENTERPRISE`` (optional):
    when set, the handler maps a subscription's price to the
    matching plan on ``customer.subscription.updated``. When
    unset, any active subscription maps to ``pro`` (preserves the
    pre-Wave-6c upgrade semantics for the test sandbox).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request


_logger = logging.getLogger("api.webhooks_stripe")
if not _logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s"
    ))
    _logger.addHandler(_h)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


# Event types we handle. Unknown types are acked with {ignored: true}.
EVENT_CHECKOUT_COMPLETED = "checkout.session.completed"
EVENT_SUB_UPDATED = "customer.subscription.updated"
EVENT_SUB_DELETED = "customer.subscription.deleted"
EVENT_INVOICE_PAYMENT_FAILED = "invoice.payment_failed"

HANDLED_EVENT_TYPES = frozenset({
    EVENT_CHECKOUT_COMPLETED,
    EVENT_SUB_UPDATED,
    EVENT_SUB_DELETED,
    EVENT_INVOICE_PAYMENT_FAILED,
})


# Plan ranking for downgrade-guard (F-W6C-001).  Higher number =
# higher tier.  Anything missing from this map is treated as
# "free" (rank 0) for safety — never allow a webhook to demote a
# workspace to an unknown plan.
PLAN_RANK: dict[str, int] = {
    "free": 0,
    "pro": 1,
    "enterprise": 2,
}


def _plan_rank(plan: Optional[str]) -> int:
    """Return the numeric rank for a plan name (unknown = free)."""
    if not plan:
        return PLAN_RANK["free"]
    return PLAN_RANK.get(plan, PLAN_RANK["free"])


class StripeWebhookNotConfigured(Exception):
    """Raised at boot when STRIPE_WEBHOOK_SECRET is missing."""


def _secret() -> Optional[str]:
    return os.environ.get("STRIPE_WEBHOOK_SECRET")


def require_secret_at_boot() -> None:
    """Fail loud at boot if the webhook secret is missing.

    The handler refuses ALL requests when the secret is unset
    (matches the HeyGen pattern in ``webhooks_heygen.py``).
    Tests that exercise this codepath set the env var via
    ``monkeypatch.setenv`` BEFORE importing this module's
    router builder.
    """
    if not _secret():
        raise StripeWebhookNotConfigured(
            "STRIPE_WEBHOOK_SECRET is not set; the Stripe webhook "
            "endpoint refuses all requests until it is configured"
        )


def _plan_for_price_id(price_id: Optional[str]) -> str:
    """Map a Stripe price id to a workspace plan.

    Default: any non-empty price id maps to ``pro``. The
    STRIPE_PRICE_ENTERPRISE env var upgrades the price -> plan
    mapping so prod tenants with a dedicated enterprise SKU
    land on the ``enterprise`` plan.
    """
    if not price_id:
        return "pro"
    enterprise_price = os.environ.get("STRIPE_PRICE_ENTERPRISE", "").strip()
    if enterprise_price and price_id == enterprise_price:
        return "enterprise"
    return "pro"


def _plan_for_subscription(subscription: Dict[str, Any]) -> str:
    """Pick the plan for a subscription object.

    Inspects the subscription's first item's price id. Falls
    back to ``pro`` if the structure is missing.
    """
    items = (subscription.get("items") or {}).get("data") or []
    if not items:
        return "pro"
    price = (items[0].get("price") or {})
    return _plan_for_price_id(price.get("id"))


def _workspace_from_checkout_session(
    payload: Dict[str, Any],
) -> Optional[str]:
    """Extract the workspace_id the checkout is for.

    Stripe Checkout sessions carry arbitrary metadata; we expect
    ``client_reference_id`` (Stripe's standard field for the
    application's own id) to be the workspace_id. Falls back to
    the ``metadata.workspace_id`` field for sessions that don't
    use client_reference_id.
    """
    cid = payload.get("client_reference_id")
    if cid:
        return str(cid)
    md = payload.get("metadata") or {}
    return md.get("workspace_id")


def _customer_id_from(obj: Dict[str, Any]) -> Optional[str]:
    """Pull a customer id from a subscription / invoice object."""
    cust = obj.get("customer")
    if isinstance(cust, str):
        return cust
    if isinstance(cust, dict):
        return cust.get("id")
    return None


def build_router(repo, *, audit=None) -> APIRouter:
    """Build the Stripe webhook router bound to a repo + optional audit.

    ``repo`` MUST be a ``core.repo.Repo`` instance; the handler
    calls ``update_workspace_billing`` and
    ``insert_stripe_event`` on it. ``audit`` is an optional
    ``auth.audit_log`` writer used for the human-readable audit
    trail — the handler never blocks on it.
    """
    require_secret_at_boot()
    secret = _secret()  # snapshot for the closure
    assert secret is not None  # type guard for the type checker

    # Import lazily so test environments that don't have the
    # stripe SDK still import this module. The constructor call
    # below is what actually requires the SDK at runtime; if
    # STRIPE_WEBHOOK_SECRET is set but stripe import fails the
    # require_secret_at_boot() call above already raised.
    import stripe as _stripe  # noqa: E402  (lazy)

    router = APIRouter()

    def _write_audit(event: str, context: Dict[str, Any],
                       workspace_id: Optional[str] = None) -> None:
        if audit is None:
            return
        try:
            from auth.audit_log import append as _audit_append
            _audit_append(repo, event=event, context=context,
                          actor="stripe_webhook",
                          workspace_id=workspace_id)
        except Exception:
            # Never let an audit write break the webhook path.
            pass

    @router.post("/api/v1/webhooks/stripe")
    async def stripe_webhook(
        request: Request,
        stripe_signature: Optional[str] = Header(default=None,
                                                    alias="Stripe-Signature"),
    ):
        if not secret:
            # Defense in depth: even though require_secret_at_boot()
            # already raised, a re-import of the module without
            # the env set should still refuse.
            raise HTTPException(503, "stripe webhook not configured")
        if not stripe_signature:
            raise HTTPException(401, "missing Stripe-Signature header")

        body = await request.body()
        if not body:
            raise HTTPException(400, "empty body")

        # ── signature verification ─────────────────────────────
        try:
            event = _stripe.Webhook.construct_event(
                body, stripe_signature, secret,
            )
            # Convert to a plain dict so the rest of the handler
            # can use ``.get()`` ergonomics without sprinkling
            # type assertions everywhere.  ``to_dict()`` recurses
            # into nested Stripe objects.
            event = event.to_dict() if hasattr(event, "to_dict") else event
        except _stripe.error.SignatureVerificationError:
            _write_audit("webhook.stripe.rejected",
                          {"reason": "signature_invalid"})
            raise HTTPException(401, "invalid signature")
        except ValueError as e:
            # Malformed payload.
            raise HTTPException(400, f"invalid payload: {e}")

        event_id = event.get("id")
        event_type = event.get("type") or ""
        if not event_id:
            raise HTTPException(400, "event missing id")

        # ── idempotency: record the event BEFORE processing ─────
        raw_json = json.dumps(event, sort_keys=True, default=str)
        try:
            recorded = repo.insert_stripe_event(
                event_id=event_id, event_type=event_type,
                raw_json=raw_json,
            )
        except Exception as e:
            # Database failure on the idempotency table is fatal
            # for the webhook — better to fail loud and let Stripe
            # retry than to process without dedup.
            _logger.warning("stripe_events insert failed: %s", e)
            raise HTTPException(503, "idempotency store unavailable")
        if not recorded:
            # Duplicate replay. Stripe expects 2xx + idempotent.
            _write_audit("webhook.stripe.duplicate",
                          {"event_id": event_id, "type": event_type})
            return {"received": True, "duplicate": True,
                    "event_id": event_id, "type": event_type}

        # ── dispatch by event type ──────────────────────────────
        data_obj = event.get("data") or {}
        inner = data_obj.get("object") or {}

        if event_type == EVENT_CHECKOUT_COMPLETED:
            workspace_id = _workspace_from_checkout_session(inner)
            if not workspace_id:
                _write_audit("webhook.stripe.ignored",
                              {"event_id": event_id,
                               "type": event_type,
                               "reason": "no_workspace_ref"})
                repo.mark_stripe_event_processed(event_id)
                return {"received": True, "ignored": True,
                        "event_id": event_id, "type": event_type,
                        "reason": "checkout session has no workspace reference"}
            customer_id = _customer_id_from(inner)
            subscription_id = inner.get("subscription")
            try:
                with repo.tx() as c:
                    repo.update_workspace_billing(
                        workspace_id,
                        plan="pro",
                        stripe_customer_id=customer_id,
                        stripe_subscription_id=subscription_id,
                    )
                _write_audit(
                    f"billing.{event_type}",
                    {"event_id": event_id, "workspace_id": workspace_id,
                     "stripe_customer_id": customer_id,
                     "stripe_subscription_id": subscription_id,
                     "plan": "pro"},
                    workspace_id=workspace_id,
                )
            except Exception as e:
                _write_audit(
                    "webhook.stripe.error",
                    {"event_id": event_id, "type": event_type,
                     "workspace_id": workspace_id, "error": str(e)},
                    workspace_id=workspace_id,
                )
                raise
            repo.mark_stripe_event_processed(event_id)
            return {"received": True, "ok": True,
                    "event_id": event_id, "type": event_type,
                    "workspace_id": workspace_id, "plan": "pro"}

        if event_type == EVENT_SUB_UPDATED:
            customer_id = _customer_id_from(inner)
            subscription_id = inner.get("id")
            period_end = inner.get("current_period_end")
            plan = _plan_for_subscription(inner)
            workspace = (
                repo.find_workspace_by_stripe_customer(customer_id)
                if customer_id else None
            )
            if not workspace:
                _write_audit("webhook.stripe.ignored",
                              {"event_id": event_id, "type": event_type,
                               "reason": "unknown_customer",
                               "customer_id": customer_id})
                repo.mark_stripe_event_processed(event_id)
                return {"received": True, "ignored": True,
                        "event_id": event_id, "type": event_type,
                        "reason": "no workspace bound to this customer"}

            # F-W6C-001 downgrade guard: a customer.subscription.updated
            # event whose mapped plan is LOWER than the workspace's
            # current plan is treated as a Stripe-side replay / stray
            # event.  We must not silently demote an enterprise tenant
            # to pro (or pro to free) because a price id flipped on
            # Stripe's side.  The audit row tells the operator exactly
            # what happened, and we ack the event with 200 so Stripe
            # doesn't retry-loop.
            existing_plan = (workspace or {}).get("plan") or "free"
            new_rank = _plan_rank(plan)
            existing_rank = _plan_rank(existing_plan)
            if new_rank < existing_rank:
                _write_audit(
                    "billing.subscription_downgrade_blocked",
                    {"event_id": event_id,
                     "workspace_id": workspace["workspace_id"],
                     "stripe_subscription_id": subscription_id,
                     "existing_plan": existing_plan,
                     "attempted_plan": plan,
                     "current_period_end": period_end},
                    workspace_id=workspace["workspace_id"],
                )
                _logger.warning(
                    "refusing subscription.downgrade for workspace=%s "
                    "existing=%s attempted=%s event=%s",
                    workspace["workspace_id"], existing_plan, plan,
                    event_id,
                )
                repo.mark_stripe_event_processed(event_id)
                return {"received": True, "ok": True,
                        "event_id": event_id, "type": event_type,
                        "workspace_id": workspace["workspace_id"],
                        "plan": existing_plan,
                        "downgrade_refused": True}

            try:
                with repo.tx() as c:
                    repo.update_workspace_billing(
                        workspace["workspace_id"],
                        plan=plan,
                        stripe_subscription_id=subscription_id,
                        current_period_end=period_end,
                    )
                _write_audit(
                    f"billing.{event_type}",
                    {"event_id": event_id,
                     "workspace_id": workspace["workspace_id"],
                     "stripe_subscription_id": subscription_id,
                     "plan": plan,
                     "current_period_end": period_end},
                    workspace_id=workspace["workspace_id"],
                )
            except Exception as e:
                _write_audit(
                    "webhook.stripe.error",
                    {"event_id": event_id, "type": event_type,
                     "workspace_id": workspace["workspace_id"],
                     "error": str(e)},
                    workspace_id=workspace["workspace_id"],
                )
                raise
            repo.mark_stripe_event_processed(event_id)
            return {"received": True, "ok": True,
                    "event_id": event_id, "type": event_type,
                    "workspace_id": workspace["workspace_id"],
                    "plan": plan}

        if event_type == EVENT_SUB_DELETED:
            customer_id = _customer_id_from(inner)
            subscription_id = inner.get("id")
            workspace = (
                repo.find_workspace_by_stripe_customer(customer_id)
                if customer_id else None
            )
            if not workspace:
                _write_audit("webhook.stripe.ignored",
                              {"event_id": event_id, "type": event_type,
                               "reason": "unknown_customer",
                               "customer_id": customer_id})
                repo.mark_stripe_event_processed(event_id)
                return {"received": True, "ignored": True,
                        "event_id": event_id, "type": event_type,
                        "reason": "no workspace bound to this customer"}
            try:
                with repo.tx() as c:
                    repo.update_workspace_billing(
                        workspace["workspace_id"],
                        plan="free",
                        clear_subscription=True,
                    )
                _write_audit(
                    f"billing.{event_type}",
                    {"event_id": event_id,
                     "workspace_id": workspace["workspace_id"],
                     "stripe_subscription_id": subscription_id,
                     "plan": "free"},
                    workspace_id=workspace["workspace_id"],
                )
            except Exception as e:
                _write_audit(
                    "webhook.stripe.error",
                    {"event_id": event_id, "type": event_type,
                     "workspace_id": workspace["workspace_id"],
                     "error": str(e)},
                    workspace_id=workspace["workspace_id"],
                )
                raise
            repo.mark_stripe_event_processed(event_id)
            return {"received": True, "ok": True,
                    "event_id": event_id, "type": event_type,
                    "workspace_id": workspace["workspace_id"],
                    "plan": "free"}

        if event_type == EVENT_INVOICE_PAYMENT_FAILED:
            customer_id = _customer_id_from(inner)
            workspace = (
                repo.find_workspace_by_stripe_customer(customer_id)
                if customer_id else None
            )
            wid = workspace["workspace_id"] if workspace else None
            # No plan change yet — dunning policy is out of scope
            # for Wave 6c (per the brief). We only write the
            # audit row so operators can see the failure event.
            _write_audit(
                f"billing.{event_type}",
                {"event_id": event_id, "workspace_id": wid,
                 "stripe_customer_id": customer_id,
                 "invoice_id": inner.get("id"),
                 "amount_due": inner.get("amount_due"),
                 "currency": inner.get("currency")},
                workspace_id=wid,
            )
            repo.mark_stripe_event_processed(event_id)
            return {"received": True, "ok": True,
                    "event_id": event_id, "type": event_type,
                    "workspace_id": wid}

        # Unknown event type — ack with 200 so Stripe stops retrying.
        _write_audit("webhook.stripe.ignored",
                      {"event_id": event_id, "type": event_type,
                       "reason": "unknown_type"})
        repo.mark_stripe_event_processed(event_id)
        return {"received": True, "ignored": True,
                "event_id": event_id, "type": event_type}

    return router
