// Headers the SvelteKit server adds to its own calls to the FastAPI
// pre-session /auth/* routes (signup, login, forgot, forgot/consume,
// verify). S1b / A1 F5, F7, F26.
//
// 1. Same-origin browser request (Origin absent or equal to this
//    server's own origin, i.e. ORIGIN under adapter-node):
//    x-vf-internal-token = HMAC-SHA256(VIDEO_FACTORY_SESSION_SECRET,
//    "vf-internal-caller:v1"), mirrored by auth/session.py
//    derive_internal_token(). The API's Origin check (auth/origin.py)
//    admits a server-side call carrying it. Browsers never see it.
// 2. Cross-site browser request (Origin present and foreign): NO token;
//    the browser's Origin is forwarded as-is so the API refuses it with
//    403 in every mode. This does not rely on kit.csrf.checkOrigin
//    (not enforced by `vite dev`), so a form action can never be used
//    as a CSRF proxy into the API.
// 3. x-forwarded-for: the chain this server received (from Caddy)
//    plus the address SvelteKit saw (event.getClientAddress()). The
//    API only trusts it from VIDEO_FACTORY_TRUSTED_PROXIES and takes
//    the rightmost untrusted hop, so per-IP rate limits key on the
//    real browser instead of one shared web-container bucket.

import crypto from "node:crypto";
import { env } from "$env/dynamic/private";
import { getRequestEvent } from "$app/server";

interface ClientEvent {
	request: Request;
	url: URL;
	getClientAddress: () => string;
}

export function internal_token(): string {
	const secret = (env.VIDEO_FACTORY_SESSION_SECRET || "").trim();
	if (!secret) return "";
	return crypto
		.createHmac("sha256", secret)
		.update("vf-internal-caller:v1", "utf-8")
		.digest("hex");
}

export function forwarded_for(event: ClientEvent): string {
	const hops: string[] = [];
	const incoming = event.request.headers.get("x-forwarded-for");
	if (incoming) {
		for (const h of incoming.split(",")) {
			const v = h.trim();
			if (v) hops.push(v);
		}
	}
	let addr = "";
	try {
		addr = event.getClientAddress();
	} catch {
		addr = "";
	}
	if (addr) hops.push(addr);
	return hops.join(", ");
}

// Returns the foreign Origin of a cross-site browser request, or "".
export function cross_site_origin(event: ClientEvent): string {
	const origin = event.request.headers.get("origin");
	if (origin === null) return "";
	return origin === event.url.origin ? "" : origin;
}

// Call inside a form action / load function. Pass the event
// explicitly or let getRequestEvent() find the current one.
export function upstream_headers(event?: ClientEvent): Record<string, string> {
	const h: Record<string, string> = {};
	let ev: ClientEvent | undefined = event;
	if (!ev) {
		try {
			ev = getRequestEvent();
		} catch {
			ev = undefined;
		}
	}
	const foreign = ev ? cross_site_origin(ev) : "";
	if (foreign) {
		h["origin"] = foreign;
	} else {
		const token = internal_token();
		if (token) h["x-vf-internal-token"] = token;
	}
	if (ev) {
		const xff = forwarded_for(ev);
		if (xff) h["x-forwarded-for"] = xff;
	}
	return h;
}
