// Login form action. Validates the email/password shape, calls
// POST /auth/login on the FastAPI backend, and on success sets the
// vf_session cookie on the SvelteKit response. The cookie's
// attributes match the FastAPI side (HttpOnly, SameSite=Strict,
// Path=/). On 200 the user is bounced to ?next= (or "/").
//
// Signup / forgot / verify have sibling files with the same shape.

import { fail, redirect } from "@sveltejs/kit";
import type { Actions, PageServerLoad } from "./$types";
import { env } from "$env/dynamic/private";
import { upstream_headers } from "$lib/server/upstream";
import { set_session_cookie, switch_workspace_action } from "$lib/server/auth-cookies";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export const load: PageServerLoad = async ({ url }) => {
	return {
		next: url.searchParams.get("next") || "/",
	};
};

export const actions: Actions = {
	default: async ({ request, cookies, url }) => {
		const data = await request.formData();
		const email = (data.get("email") || "").toString().trim();
		const password = (data.get("password") || "").toString();
		const next = (data.get("next") || url.searchParams.get("next") || "/").toString();

		const errors: Record<string, string> = {};
		if (!email) errors.email = "Email is required";
		else if (!EMAIL_RE.test(email)) errors.email = "Invalid email";
		if (!password) errors.password = "Password is required";
		if (Object.keys(errors).length) {
			return fail(400, { errors, email });
		}

		try {
			const r = await fetch(API_BASE + "/auth/login", {
				method: "POST",
				headers: { "content-type": "application/json", ...upstream_headers() },
				body: JSON.stringify({ email, password }),
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({} as Record<string, unknown>));
				const detail = (j.error || j.detail || "Login failed") as string;
				// Map "locked" 423 and rate-limit 429 distinctly.
				return fail(r.status, { errors: { _form: detail }, email });
			}
			// Forward the Set-Cookie header from FastAPI to the browser.
			const set_cookie = r.headers.get("set-cookie") || "";
			set_session_cookie(cookies, set_cookie);
		} catch (e) {
			const msg = e instanceof Error ? e.message : "Login failed";
			return fail(503, { errors: { _form: msg }, email });
		}

		// Validate next: only allow same-origin relative paths.
		const target = next.startsWith("/") && !next.startsWith("//") ? next : "/";
		throw redirect(303, target);
	},

	switch: switch_workspace_action,
};
