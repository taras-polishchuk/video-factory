// Signup form action. Validates client-side shape, calls
// POST /auth/signup {email, password, workspace_name} on the
// FastAPI backend, sets vf_session on success, and bounces to the
// empty-state onboarding. Server-side validation here is the
// minimum needed to give a useful 400; the canonical rules live
// in the API.

import { fail, redirect } from "@sveltejs/kit";
import type { Actions, PageServerLoad } from "./$types";
import { env } from "$env/dynamic/private";
import { upstream_headers } from "$lib/server/upstream";
import { set_session_cookie } from "$lib/server/auth-cookies";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PASSWORD_MIN = 8;

export const load: PageServerLoad = async () => {
	return {};
};

export const actions: Actions = {
	default: async ({ request, cookies }) => {
		const data = await request.formData();
		const email = (data.get("email") || "").toString().trim();
		const password = (data.get("password") || "").toString();
		const workspace_name = (data.get("workspace_name") || "").toString().trim();

		const errors: Record<string, string> = {};
		if (!email) errors.email = "Email is required";
		else if (!EMAIL_RE.test(email)) errors.email = "Invalid email";
		if (!password) errors.password = "Password is required";
		else if (password.length < PASSWORD_MIN) errors.password = `At least ${PASSWORD_MIN} characters`;
		if (!workspace_name) errors.workspace_name = "Workspace name is required";
		else if (workspace_name.length > 80) errors.workspace_name = "Up to 80 characters";
		if (Object.keys(errors).length) {
			return fail(400, { errors, email, workspace_name });
		}

		try {
			const r = await fetch(API_BASE + "/auth/signup", {
				method: "POST",
				headers: { "content-type": "application/json", ...upstream_headers() },
				body: JSON.stringify({ email, password, workspace_name }),
			});
			if (!r.ok) {
				const j = await r.json().catch(() => ({} as Record<string, unknown>));
				const detail = (j.error || j.detail || "Signup failed") as string;
				// Surface duplicate email distinctly so the form can show it.
				if (r.status === 409) {
					return fail(409, { errors: { email: detail }, email, workspace_name });
				}
				if (r.status === 400) {
					// Map backend field errors back to the form when possible.
					if (/password/i.test(detail)) {
						return fail(400, { errors: { password: detail }, email, workspace_name });
					}
				}
				return fail(r.status, { errors: { _form: detail }, email, workspace_name });
			}
			const set_cookie = r.headers.get("set-cookie") || "";
			set_session_cookie(cookies, set_cookie);
		} catch (e) {
			const msg = e instanceof Error ? e.message : "Signup failed";
			return fail(503, { errors: { _form: msg }, email, workspace_name });
		}

		// Signup = first workspace. Bounce to the home page, which
		// renders the empty-state CTA until a provider key is added.
		throw redirect(303, "/");
	},
};
