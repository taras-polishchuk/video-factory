// /forgot — request a magic link for password reset. The endpoint
// is a no-op-by-design on the FastAPI side (always returns 200 to
// prevent email enumeration); we mirror that here so the form is
// honest about it.

import { fail } from "@sveltejs/kit";
import type { Actions, PageServerLoad } from "./$types";
import { env } from "$env/dynamic/private";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export const load: PageServerLoad = async () => ({});

export const actions: Actions = {
	default: async ({ request }) => {
		const data = await request.formData();
		const email = (data.get("email") || "").toString().trim();
		if (!email || !EMAIL_RE.test(email)) {
			return fail(400, { errors: { email: "Invalid email" }, email });
		}
		try {
			await fetch(API_BASE + "/auth/forgot", {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify({ email }),
			});
		} catch {
			// Network error: still 200 to the user; the email side
			// is best-effort. We log nothing because the email is
			// sensitive and we're not the canonical store.
		}
		// Always land on a "check your inbox" success view, never
		// reveal whether the email exists.
		return { ok: true, email };
	},
};
