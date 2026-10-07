// Shared types for the auth layer. Mirrored on the Python side in
// auth/dependencies.py (AuthContext) + the response shape of
// /auth/me. Keep in sync.

export interface AuthUser {
	user_id: string;
	email: string;
	email_verified: boolean;
	status: string;
}

export interface AuthWorkspace {
	workspace_id: string;
	role: string;
	slug: string;
	name: string;
	plan: string;
}
