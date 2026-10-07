// Layout server loader.
//
// Reads the auth state attached by hooks.server.ts and exposes it
// to every child page via the PageData contract. No client-side
// fetch — every byte on the page is rendered server-side first,
// then hydrated by the client bundle.
//
// Also queries the credentials endpoint for the active workspace so
// the empty-state CTA ("paste your first provider key to get
// started") can hide itself once a key has been saved. When the
// credentials endpoint errors (e.g. backend offline) we default
// to true so the CTA is suppressed in the worst case.

import type { LayoutServerLoad } from "./$types";
import { env } from "$env/dynamic/private";

const API_BASE = env.VIDEO_FACTORY_API_URL || "http://127.0.0.1:8000";

export const load: LayoutServerLoad = async ({ locals, cookies }) => {
	let firstCredentialExists = true;
	if (locals.active_workspace) {
		const session = cookies.get("vf_session");
		if (session) {
			try {
				const r = await fetch(
					API_BASE + "/api/v1/workspaces/" + locals.active_workspace.workspace_id + "/credentials",
					{ headers: { cookie: `vf_session=${session}` } },
				);
				if (r.ok) {
					const items = (await r.json()) as unknown[];
					firstCredentialExists = items.length > 0;
				}
				// 401 / 403 / 5xx: keep the default (true) so the
				// empty state is suppressed rather than shown
				// incorrectly.
			} catch {
				// network: same as above
			}
		}
	}
	return {
		user: locals.user,
		workspaces: locals.workspaces,
		activeWorkspace: locals.active_workspace,
		firstCredentialExists,
	};
};
