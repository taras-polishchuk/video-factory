// See https://svelte.dev/docs/kit/types#app
import type { AuthWorkspace, AuthUser } from "$lib/server/types";

declare global {
	namespace App {
		interface Locals {
			user: AuthUser;
			workspaces: AuthWorkspace[];
			active_workspace: AuthWorkspace | null;
		}
		// PageData is inferred from +layout.server.ts and +page.server.ts.
		interface PageData {
			user?: AuthUser;
			workspaces?: AuthWorkspace[];
			activeWorkspace?: AuthWorkspace | null;
			firstCredentialExists?: boolean;
		}
		// interface PageState {}
		// interface Platform {}
		interface Error {
			message: string;
			code?: string;
		}
	}
}

export {};
