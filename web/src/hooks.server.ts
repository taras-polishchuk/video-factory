// SvelteKit server hook: enforce session-based auth on every request.
//
// Reads the session cookie (set by POST /auth/login or /auth/signup
// on the FastAPI side). When the cookie is missing or invalid on a
// non-public path, redirects to /login?next=<original>. When the
// cookie is valid, the upstream user + workspaces are fetched from
// GET /auth/me and attached to event.locals for the layout + page
// server loaders to consume.
//
// All API URLs come from $env/static/private so tests can override
// them. The default targets the FastAPI backend on the same host
// at port 8000.

import type { Handle } from "@sveltejs/kit";
import { env } from "$env/dynamic/private";
import { is_public_path, login_redirect_for } from "$lib/server/auth-allowlist";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

interface AuthMeResponse {
	user?: {
		user_id: string;
		email: string;
		email_verified: 0 | 1 | boolean;
		status: string;
	};
	workspaces?: Array<{
		workspace_id: string;
		role: string;
		slug: string;
		name: string;
		plan: string;
	}>;
}

export const handle: Handle = async ({ event, resolve }) => {
	const path = event.url.pathname;

	// 1. Public paths: pass through without auth.
	if (is_public_path(path)) {
		return resolve(event);
	}

	// 2. Read the session cookie (set by FastAPI on /auth/login,
	//    /auth/signup). SameSite=Strict + HttpOnly so this is safe
	//    to forward to the upstream API.
	const session = event.cookies.get("vf_session");
	if (!session) {
		// For API routes return 401 JSON; for pages redirect to /login.
		if (path.startsWith("/api/")) {
			return new Response(
				JSON.stringify({ error: "unauthenticated" }),
				{ status: 401, headers: { "content-type": "application/json" } },
			);
		}
		return new Response(null, {
			status: 303,
			headers: { Location: login_redirect_for(path) },
		});
	}

	// 3. Validate the cookie against the upstream /auth/me endpoint.
	//    On any failure (expired, revoked, server down) we treat the
	//    user as unauthenticated and clear the cookie.
	let me: AuthMeResponse | null = null;
	try {
		const r = await fetch(API_BASE + "/auth/me", {
			headers: { cookie: `vf_session=${session}` },
		});
		if (r.ok) {
			me = (await r.json()) as AuthMeResponse;
		} else if (r.status === 401 || r.status === 423) {
			event.cookies.delete("vf_session", { path: "/" });
		}
	} catch {
		// Backend unreachable: treat as unauthenticated rather than
		// crash the page. The page can re-render an error state.
		me = null;
	}

	if (!me || !me.user) {
		if (path.startsWith("/api/")) {
			return new Response(
				JSON.stringify({ error: "unauthenticated" }),
				{ status: 401, headers: { "content-type": "application/json" } },
			);
		}
		return new Response(null, {
			status: 303,
			headers: { Location: login_redirect_for(path) },
		});
	}

	// 4. Attach user + workspaces to locals so layout / +page.server
	//    loaders can read them without re-fetching.
	event.locals.user = {
		user_id: me.user.user_id,
		email: me.user.email,
		email_verified: !!me.user.email_verified,
		status: me.user.status,
	};
	event.locals.workspaces = (me.workspaces || []).map((w) => ({
		workspace_id: w.workspace_id,
		role: w.role,
		slug: w.slug,
		name: w.name,
		plan: w.plan,
	}));

	// 5. Resolve active workspace. Cookie "vf_active_workspace" wins
	//    (set by the switcher); otherwise the first membership.
	const activeCookie = event.cookies.get("vf_active_workspace");
	const validActive = activeCookie
		? event.locals.workspaces.find((w) => w.workspace_id === activeCookie)
		: null;
	event.locals.active_workspace = validActive || event.locals.workspaces[0] || null;

	return resolve(event);
};
