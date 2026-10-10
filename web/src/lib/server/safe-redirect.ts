// Same-origin redirect target guard shared by form actions (?next= on
// login, ?back= on workspace switch).
//
// Only a path on this site is allowed. Browsers read "/\\evil.com" and
// "/<TAB>/evil.com" as "//evil.com", so a plain startsWith("/") check is
// not enough: re-parse against a dummy origin and reject anything that
// leaves it. Returns the normalised path+query+hash, or `fallback`.

const DUMMY = "http://vf.invalid";

export function same_origin_path(target: string | null | undefined, fallback = "/"): string {
	if (!target || !target.startsWith("/") || target.startsWith("//")) return fallback;
	try {
		const u = new URL(target, DUMMY);
		return u.origin === DUMMY ? u.pathname + u.search + u.hash : fallback;
	} catch {
		return fallback;
	}
}
