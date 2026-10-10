// Billing helpers for the /billing page. Pure functions only (no
// SvelteKit imports) so the logic can be read and tested on its own.
//
// The API answers 503 {"detail": "billing not configured"} on every
// billing route while no Stripe key is set (BILLING B1). The page
// turns every non-2xx answer into one sentence a customer can act on
// and never shows a raw JSON body or an upstream error text.

export type BillingAction = "checkout" | "portal";

export function billing_error_message(
	status: number,
	action: BillingAction,
	code?: string,
): string {
	if (status === 503) {
		return "Billing is not set up on this deployment yet. Ask the operator to configure it.";
	}
	if (status === 404 && code === "no_stripe_customer") {
		return "This workspace has no subscription yet. Upgrade first, then you can manage it here.";
	}
	if (status === 409) {
		return "This workspace is already on a paid plan. Use Manage subscription.";
	}
	if (status === 403) {
		return "Only workspace owners and admins can manage billing.";
	}
	if (status === 401) {
		return "Your session has expired. Log in again to continue.";
	}
	if (status === 502) {
		return action === "checkout"
			? "The payment provider did not answer. Nothing was charged. Try again in a moment."
			: "The payment provider did not answer. Try again in a moment.";
	}
	if (status === 400) {
		return "That request was not valid. Reload the page and try again.";
	}
	return "Something went wrong. Try again.";
}
