// Server-only API client. Proxies to the FastAPI backend using the
// incoming request's session cookie. NEVER reads or stores the
// cookie in a place that survives the request — the cookie is
// forwarded on every call and never persisted server-side.
//
// Used by +page.server.ts and form actions. NOT for use in the
// browser bundle (will fail at build time if accidentally imported
// from a .svelte file because of the $server import marker).

import { env } from "$env/dynamic/private";
import { error } from "@sveltejs/kit";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

export interface ApiRequestOpts {
	method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
	body?: unknown;
	cookie?: string; // raw "vf_session=..." value
	headers?: Record<string, string>;
	// Pass-through for OIDC /start which returns a 302.
	// We forward this to fetch() so SvelteKit never follows
	// the redirect (the browser needs the Location URL).
	redirect?: RequestRedirect;
}

export class ApiError extends Error {
	status: number;
	constructor(status: number, message: string) {
		super(message);
		this.status = status;
	}
}

export async function api_request<T = unknown>(
	path: string,
	opts: ApiRequestOpts = {},
): Promise<T> {
	const headers: Record<string, string> = { ...(opts.headers || {}) };
	if (opts.cookie) headers["cookie"] = opts.cookie;
	if (opts.body !== undefined && !(opts.body instanceof FormData)) {
		headers["content-type"] = "application/json";
	}
	const init: RequestInit = {
		method: opts.method || "GET",
		headers,
		redirect: opts.redirect || "follow",
	};
	if (opts.body !== undefined) {
		init.body = opts.body instanceof FormData
			? opts.body
			: JSON.stringify(opts.body);
	}
	const r = await fetch(API_BASE + path, init);
	if (r.status >= 300 && r.status < 400) {
		// OIDC /start returns 302 to the provider authorize URL.
		// Surface the Location header to the caller as
		// `{ location: "..." }` so the UI can decide whether to
		// follow it (window.location = url) or open in a popup.
		if (opts.redirect === "manual") {
			return { location: r.headers.get("location") || "" } as T;
		}
	}
	if (!r.ok) {
		let detail = r.statusText;
		try {
			const j = await r.json();
			detail = (j && (j.error || j.detail)) || detail;
		} catch {
			try { detail = await r.text(); } catch { /* keep statusText */ }
		}
		throw new ApiError(r.status, detail);
	}
	const ct = r.headers.get("content-type") || "";
	if (ct.includes("application/json")) return r.json() as Promise<T>;
	return (await r.text()) as unknown as T;
}

// Thin wrappers used by the +page.server.ts files below. Each
// returns the parsed body and accepts the raw session cookie so
// the caller never has to think about cookie mechanics.

// Note (QA-S1b-07): the four pre-login wrappers (signup/login/
// forgot/verify) were removed. They were unused on the Svelte side
// (only `api_request` is called directly via auth-cookies.ts for the
// post-login paths) and they did not forward an upstream Origin, which
// would have surfaced as 403 `origin_missing` in production.

export const Auth = {
	logout: (cookie: string) =>
		api_request<{ ok: true }>("/auth/logout", {
			method: "POST", cookie,
			headers: { "x-csrf-token": csrf_for_cookie(cookie) },
		}),
	me: (cookie: string) =>
		api_request<{
			user: { user_id: string; email: string; email_verified: boolean; status: string };
			workspaces: Array<{ workspace_id: string; role: string; slug: string; name: string; plan: string }>;
		}>("/auth/me", { cookie }),
	switch_workspace: (cookie: string, workspace_id: string) =>
		api_request<{ active_workspace_id: string }>("/auth/switch-workspace", {
			method: "POST", cookie, body: { workspace_id },
			headers: { "x-csrf-token": csrf_for_cookie(cookie) },
		}),
	// Wave 4 OIDC: trigger the Google / GitHub authorize redirect.
	// The browser follows the 302 returned by /start; this wrapper
	// returns the Location URL so the UI can either open it
	// directly (window.location = url) or open it in a popup.
	oidc_start: (provider: "google" | "github", cookie?: string) =>
		api_request<{ location: string }>(
			`/auth/oauth/${provider}/start`,
			{ method: "GET", cookie, redirect: "manual" },
		),
	// Wave 4 OIDC: list every linked provider for the current user.
	oidc_identities: (cookie: string) =>
		api_request<{
			identities: Array<{ provider: string; provider_sub: string; created_at: string }>;
		}>("/auth/oauth/identities", { cookie }),
	// Wave 4 OIDC: disconnect a previously linked identity.
	oidc_disconnect: (cookie: string, provider: string, provider_sub: string) =>
		api_request<{ ok: true }>(
			`/auth/oauth/${provider}/disconnect`, {
				method: "POST", cookie,
				body: { provider_sub },
				headers: { "x-csrf-token": csrf_for_cookie(cookie) },
			}),
};

// Mirrors the FastAPI HMAC: csrftok = hmac_sha256(secret, session_id).
// We don't have access to VIDEO_FACTORY_SESSION_SECRET here, so the
// server-side call simply forwards the session cookie. The FastAPI
// /auth/logout and /auth/switch-workspace require X-CSRF-Token, so
// we compute the HMAC from the session id server-side.
import crypto from "node:crypto";

function csrf_for_cookie(cookie: string): string {
	const session_id = read_cookie(cookie, "vf_session");
	const secret = env.VIDEO_FACTORY_SESSION_SECRET || "";
	if (!session_id || !secret) return "";
	return crypto
		.createHmac("sha256", secret)
		.update(session_id, "utf-8")
		.digest("hex");
}

function read_cookie(raw: string, name: string): string {
	for (const part of raw.split(";")) {
		const [k, ...rest] = part.trim().split("=");
		if (k === name) return rest.join("=");
	}
	return "";
}

// Re-export so form actions can throw 4xx without importing
// @sveltejs/kit directly.
export { error };
