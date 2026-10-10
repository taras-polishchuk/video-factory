// /billing: upgrade to a paid plan / open the Stripe customer portal.
//
// The page asks GET /api/v1/billing/status (booleans only, no secrets)
// whether this deployment can take payments at all. When it cannot,
// the buttons are disabled and a plain message is shown instead of a
// 503 after the click. The actions call the API server-side, so the
// CSRF token is derived here (same HMAC as /logout) and the browser
// never needs a client-side token fetch.

import { fail, redirect } from "@sveltejs/kit";
import crypto from "node:crypto";
import type { Actions, PageServerLoad } from "./$types";
import { env } from "$env/dynamic/private";
import { login_redirect_for } from "$lib/server/auth-allowlist";
import { billing_error_message, type BillingAction } from "$lib/server/billing";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

function csrf_for_session(session: string): string {
	const secret = env.VIDEO_FACTORY_SESSION_SECRET || "";
	if (!session || !secret) return "";
	return crypto.createHmac("sha256", secret).update(session, "utf-8").digest("hex");
}

export const load: PageServerLoad = async ({ cookies }) => {
	// null = unknown (API unreachable or status route failed). The
	// page treats unknown like "off" so nobody clicks into an error.
	let billingEnabled: boolean | null = null;
	const session = cookies.get("vf_session");
	if (session) {
		try {
			const r = await fetch(API_BASE + "/api/v1/billing/status", {
				headers: { cookie: `vf_session=${session}` },
			});
			if (r.ok) {
				const j = (await r.json()) as { enabled?: unknown };
				billingEnabled = j.enabled === true;
			}
		} catch {
			// network: stay unknown
		}
	}
	return { billingEnabled };
};

async function call_billing(
	action: BillingAction,
	cookies: import("@sveltejs/kit").Cookies,
	workspace_id: string,
	body: Record<string, unknown>,
) {
	const session = cookies.get("vf_session");
	if (!session) throw redirect(303, login_redirect_for("/billing"));
	if (!workspace_id) {
		return fail(400, { message: "Select a workspace first." });
	}
	let r: Response;
	try {
		r = await fetch(API_BASE + `/api/v1/billing/${action}`, {
			method: "POST",
			headers: {
				cookie: `vf_session=${session}`,
				"content-type": "application/json",
				"x-csrf-token": csrf_for_session(session),
			},
			body: JSON.stringify({ workspace_id, ...body }),
		});
	} catch {
		return fail(502, { message: billing_error_message(502, action) });
	}
	let j: { url?: unknown; code?: unknown } = {};
	try {
		j = await r.json();
	} catch {
		// non-JSON body: handled below by status only
	}
	if (r.ok && typeof j.url === "string" && j.url) {
		// External Stripe URL: a plain 303 makes the browser leave.
		throw redirect(303, j.url);
	}
	const status = r.ok ? 502 : r.status;
	const code = typeof j.code === "string" ? j.code : undefined;
	return fail(status, { message: billing_error_message(status, action, code) });
}

export const actions: Actions = {
	checkout: async ({ cookies, locals }) =>
		call_billing("checkout", cookies, locals.active_workspace?.workspace_id ?? "", {
			plan: "pro",
		}),
	portal: async ({ cookies, locals }) =>
		call_billing("portal", cookies, locals.active_workspace?.workspace_id ?? "", {}),
};
