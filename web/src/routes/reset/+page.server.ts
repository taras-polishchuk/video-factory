// /reset?token=... — choose a new password from the emailed reset
// link (EMAIL E1). The link is built by core/mailer.py as
// {VIDEO_FACTORY_PUBLIC_BASE_URL}/reset?token=<token>. The form posts
// back to this page; the action calls POST /auth/forgot/consume
// {token, new_password} on the FastAPI side with upstream_headers()
// (S1b Origin rule + X-Forwarded-For), like the other pre-session
// pages.
//
// The token is copied into a hidden form field so the POST does not
// depend on the query string. The page sets referrer=same-origin so
// the token never leaves in a Referer header to another origin (not
// no-referrer: that makes browsers send `Origin: null` on the POST,
// which the S1b Origin check refuses). Nothing here logs the token.

import { fail } from "@sveltejs/kit";
import type { Actions, PageServerLoad } from "./$types";
import { env } from "$env/dynamic/private";
import { upstream_headers } from "$lib/server/upstream";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

// secrets.token_urlsafe(24) = 32 chars of [A-Za-z0-9_-].
const TOKEN_RE = /^[A-Za-z0-9_-]{16,128}$/;

// Mirrors auth.is_strong_password (12..256 chars, lower, upper, digit).
function weak_reason(pw: string): string | null {
	if (pw.length < 12) return "At least 12 characters";
	if (pw.length > 256) return "At most 256 characters";
	if (!/[a-z]/.test(pw) || !/[A-Z]/.test(pw) || !/[0-9]/.test(pw)) {
		return "Use upper-case, lower-case letters and a digit";
	}
	return null;
}

export const load: PageServerLoad = async ({ url }) => {
	const raw = (url.searchParams.get("token") || "").trim();
	const valid = TOKEN_RE.test(raw);
	return { token: valid ? raw : "", missing: !valid };
};

export const actions: Actions = {
	default: async ({ request }) => {
		const data = await request.formData();
		const token = (data.get("token") || "").toString().trim();
		const new_password = (data.get("new_password") || "").toString();
		const confirm = (data.get("confirm_password") || "").toString();

		if (!TOKEN_RE.test(token)) {
			return fail(400, { errors: { _form: "This reset link is invalid. Ask for a new one." }, expired: true });
		}
		const errors: Record<string, string> = {};
		const weak = weak_reason(new_password);
		if (!new_password) errors.new_password = "Password is required";
		else if (weak) errors.new_password = weak;
		if (new_password && confirm !== new_password) errors.confirm_password = "Passwords do not match";
		if (Object.keys(errors).length) {
			return fail(400, { errors });
		}

		let r: Response;
		try {
			r = await fetch(API_BASE + "/auth/forgot/consume", {
				method: "POST",
				headers: { "content-type": "application/json", ...upstream_headers() },
				body: JSON.stringify({ token, new_password }),
			});
		} catch {
			return fail(503, { errors: { _form: "Service unavailable, try again in a minute." } });
		}
		if (r.ok) {
			return { ok: true };
		}
		const j = (await r.json().catch(() => ({}))) as Record<string, unknown>;
		const detail = String(j.detail || j.error || "");
		if (r.status === 404 || /already used|expired/i.test(detail)) {
			return fail(400, {
				errors: { _form: "This reset link has expired or was already used. Ask for a new one." },
				expired: true,
			});
		}
		if (r.status === 429) {
			return fail(429, { errors: { _form: "Too many attempts. Try again later." } });
		}
		if (/password/i.test(detail)) {
			return fail(400, { errors: { new_password: detail } });
		}
		return fail(r.status, { errors: { _form: "Could not reset the password." } });
	},
};
