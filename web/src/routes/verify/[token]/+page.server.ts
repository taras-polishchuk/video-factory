// /verify/[token] — consume an email-verification token on page
// load. The FastAPI endpoint is POST /auth/verify {token} and
// returns {ok, email_verified} on success. The SvelteKit loader
// runs server-side, so a GET on this URL is enough to verify +
// render the result. We call the API with the token in the body
// even though it's a GET-shaped URL.

import { error } from "@sveltejs/kit";
import type { PageServerLoad } from "./$types";
import { env } from "$env/dynamic/private";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

export const load: PageServerLoad = async ({ params }) => {
	const token = (params.token || "").trim();
	if (!token) {
		throw error(400, "missing token");
	}
	try {
		const r = await fetch(API_BASE + "/auth/verify", {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ token }),
		});
		if (r.status === 404) {
			throw error(404, "token not found");
		}
		if (r.status === 400) {
			const j = await r.json().catch(() => ({} as Record<string, unknown>));
			const detail = (j.error || j.detail || "token invalid") as string;
			// Surface "token expired" and "token already used"
			// distinctly so the user knows whether to ask for a
			// new link.
			throw error(400, detail);
		}
		if (!r.ok) {
			throw error(r.status, "verification failed");
		}
		const j = (await r.json()) as { ok: true; email_verified: true };
		return { ok: j.ok, email_verified: j.email_verified };
	} catch (e) {
		if (e && typeof e === "object" && "status" in e) throw e;
		throw error(503, "verification backend unreachable");
	}
};
