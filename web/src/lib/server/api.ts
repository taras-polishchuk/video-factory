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
	};
	if (opts.body !== undefined) {
		init.body = opts.body instanceof FormData
			? opts.body
			: JSON.stringify(opts.body);
	}
	const r = await fetch(API_BASE + path, init);
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

export const Auth = {
	signup: (body: { email: string; password: string; workspace_name: string }) =>
		api_request<{ user: { user_id: string; email: string }; workspace_id: string }>(
			"/auth/signup", { method: "POST", body },
		),
	login: (body: { email: string; password: string }) =>
		api_request<{ user: { user_id: string; email: string }; workspace_id: string; workspace_slug: string }>(
			"/auth/login", { method: "POST", body },
		),
	logout: (cookie: string) =>
		api_request<{ ok: true }>("/auth/logout", {
			method: "POST", cookie,
			headers: { "x-csrf-token": csrf_for_cookie(cookie) },
		}),
	forgot: (email: string) =>
		api_request<{ ok: true }>("/auth/forgot", {
			method: "POST", body: { email },
		}),
	verify: (token: string) =>
		api_request<{ ok: true; email_verified: true }>("/auth/verify", {
			method: "POST", body: { token },
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
