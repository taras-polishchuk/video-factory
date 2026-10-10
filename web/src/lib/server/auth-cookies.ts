// Cookie helpers shared across auth form actions.
//
// The FastAPI backend sets the session cookie via Set-Cookie on
// /auth/login and /auth/signup. SvelteKit doesn't auto-forward
// Set-Cookie from fetch responses, so we parse the value and
// re-emit it on the SvelteKit cookie jar. The attributes (HttpOnly,
// SameSite=Strict, Path=/) are appended by FastAPI; we mirror them
// in the SvelteKit options so the cookie is set with the same
// semantics regardless of which path issued it.

import type { Cookies } from "@sveltejs/kit";
import { env } from "$env/dynamic/private";
import { ApiError, Auth } from "./api";
import { fail, redirect } from "@sveltejs/kit";
import { same_origin_path } from "./safe-redirect";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

export function set_session_cookie(cookies: Cookies, set_cookie_header: string): void {
	// Parse the first vf_session=<id> value plus its attribute set.
	// Format produced by auth/session.py:build_set_cookie_value.
	const first = set_cookie_header.split(";")[0] || "";
	const eq = first.indexOf("=");
	if (eq <= 0) return;
	const name = first.slice(0, eq).trim();
	if (name !== "vf_session") return;
	const value = first.slice(eq + 1).trim();
	const attrs = parse_cookie_attrs(set_cookie_header);
	cookies.set(name, value, {
		path: attrs.path || "/",
		httpOnly: true,
		sameSite: "strict",
		secure: attrs.secure,
		maxAge: attrs.max_age,
	});
}

export function clear_session_cookie(cookies: Cookies): void {
	cookies.delete("vf_session", { path: "/" });
}

interface ParsedAttrs {
	path: string;
	max_age?: number;
	secure: boolean;
}

function parse_cookie_attrs(raw: string): ParsedAttrs {
	const out: ParsedAttrs = { path: "/", secure: false };
	for (const part of raw.split(";")) {
		const p = part.trim();
		if (!p) continue;
		const eq = p.indexOf("=");
		const k = (eq >= 0 ? p.slice(0, eq) : p).toLowerCase();
		const v = eq >= 0 ? p.slice(eq + 1).trim() : "";
		if (k === "path") out.path = v || "/";
		else if (k === "max-age") {
			const n = Number(v);
			if (Number.isFinite(n)) out.max_age = n;
		} else if (k === "secure") out.secure = true;
	}
	return out;
}

// Shared "switch workspace" form action used by the
// WorkspaceSwitcher component (POST /login?/switch&back=<path>).
// Calls the API with the session's CSRF token (the endpoint rejects the
// call otherwise), stores the choice, then bounces back to `back` so the
// user stays on the page they were looking at.
export async function switch_workspace_action({ request, cookies, url }: {
	request: Request;
	cookies: Cookies;
	url: URL;
}) {
	const data = await request.formData();
	const workspace_id = (data.get("workspace_id") || "").toString();
	if (!workspace_id) {
		return fail(400, { errors: { _form: "workspace_id required" } });
	}
	const cookie = cookies.get("vf_session");
	if (!cookie) {
		return fail(401, { errors: { _form: "not signed in" } });
	}
	try {
		await Auth.switch_workspace(`vf_session=${cookie}`, workspace_id);
		cookies.set("vf_active_workspace", workspace_id, {
			path: "/",
			sameSite: "lax",
			httpOnly: false, // readable by the client; only an identifier
		});
	} catch (e) {
		if (e instanceof ApiError) {
			return fail(e.status, { errors: { _form: e.message } });
		}
		return fail(503, { errors: { _form: "switch failed" } });
	}
	throw redirect(303, same_origin_path(url.searchParams.get("back")));
}
