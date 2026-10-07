// /logout — server-side logout. Calls FastAPI's POST /auth/logout
// to invalidate the session, then clears the cookie locally. The
// form lives in the layout; this file just handles the POST.

import { redirect } from "@sveltejs/kit";
import type { Actions } from "./$types";
import { env } from "$env/dynamic/private";
import { clear_session_cookie, set_session_cookie } from "$lib/server/auth-cookies";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

export const actions: Actions = {
	default: async ({ cookies }) => {
		const session = cookies.get("vf_session");
		if (session) {
			// Best-effort: invalidate the session server-side. Even
			// if the backend is unreachable, we still clear the
			// local cookie below.
			try {
				// Derive the CSRF token from the session id so the
				// /auth/logout endpoint accepts this call.
				const crypto = await import("node:crypto");
				const secret = env.VIDEO_FACTORY_SESSION_SECRET || "";
				const csrf = secret
					? crypto.createHmac("sha256", secret).update(session, "utf-8").digest("hex")
					: "";
				const r = await fetch(API_BASE + "/auth/logout", {
					method: "POST",
					headers: {
						cookie: `vf_session=${session}`,
						"x-csrf-token": csrf,
					},
				});
				// If the response carries a Set-Cookie, mirror it.
				const set_cookie = r.headers.get("set-cookie") || "";
				if (set_cookie) set_session_cookie(cookies, set_cookie);
			} catch {
				// ignored — clear local cookie below regardless
			}
		}
		clear_session_cookie(cookies);
		cookies.delete("vf_active_workspace", { path: "/" });
		throw redirect(303, "/login");
	},
};
