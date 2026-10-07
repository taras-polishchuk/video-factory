// Pure allowlist function for the auth gate.
//
// Kept out of hooks.server.ts so the pytest suite can exercise the
// logic directly without standing up a SvelteKit dev server. The
// behavior is identical to what the hook applies at request time.
//
// Rules:
//   - /api/v1/auth/*  → always allowed (signup, login, csrf, etc.)
//   - /api/v1/webhooks/* → always allowed (Stripe, etc. sign with HMAC)
//   - /healthz / /readyz / /metrics → always allowed
//   - /login / /signup / /forgot / /verify/* → always allowed
//   - everything else requires a session cookie (handled by the hook)
//
// `pathname` is the URL pathname only (no query, no hash).
// Returns true when the request is allowed to proceed without
// authentication; false when the hook should redirect to /login.

const EXACT_PUBLIC: ReadonlyArray<string> = [
	"/login",
	"/signup",
	"/forgot",
	"/healthz",
	"/readyz",
	"/metrics",
];

const PREFIX_PUBLIC: ReadonlyArray<string> = [
	"/api/v1/auth/",
	"/api/v1/webhooks/",
	"/verify/",
	// Wave 4: OIDC sign-in / callback / disconnect — must be
	// reachable without a session cookie. /start and /callback
	// issue the session on success; /disconnect requires one
	// (enforced by the FastAPI handler, not the gate).
	"/auth/oauth/",
];

function normalize(pathname: string): string {
	// Drop trailing slash except for root so "/login" and "/login/"
	// resolve identically. SvelteKit normalizes too, but we don't
	// depend on the order.
	if (pathname.length > 1 && pathname.endsWith("/")) {
		return pathname.slice(0, -1);
	}
	return pathname;
}

export function is_public_path(pathname: string): boolean {
	const p = normalize(pathname);
	if (EXACT_PUBLIC.includes(p)) return true;
	for (const prefix of PREFIX_PUBLIC) {
		if (p === prefix.slice(0, -1)) return true;
		if (p.startsWith(prefix)) return true;
	}
	return false;
}

export function login_redirect_for(pathname: string): string {
	const p = normalize(pathname);
	if (p === "/login" || p === "/signup" || p === "/forgot") return "/";
	// Encode the original path so the login page can bounce back.
	const q = encodeURIComponent(p);
	return `/login?next=${q}`;
}
