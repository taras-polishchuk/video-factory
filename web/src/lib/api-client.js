// Browser-side API helper. Used by the existing page components
// (bible, assets, jobs, credentials, etc.) that haven't been
// converted to +page.server.ts yet. The session cookie is sent
// automatically by the browser on same-origin fetch, so the
// FastAPI auth layer picks it up without any code on our side.
//
// SSR note: SvelteKit renders these pages on the server first; on
// the server `fetch` is a relative URL, so the call goes through
// the Vite proxy (vite.config.js) to the FastAPI backend. The
// session cookie is forwarded by the load function via
// `event.cookies` if needed. We don't pass the cookie here because
// SvelteKit's load() runs on the server and already forwards
// cookies when `credentials: 'include'` is set (or when
// `event.fetch` is used).

const BASE = '/api/v1';

// CSRF (SECURITY S1a): every state-changing API route checks
// X-CSRF-Token against the session. The session cookie is HttpOnly,
// so the token comes from GET /auth/csrf (same origin; Caddy and the
// Vite dev proxy forward /auth/* to FastAPI). Cached per page load;
// refetched once if the API rejects it (e.g. after a re-login).
const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
let csrfToken = null;

async function getCsrf(force = false) {
	if (csrfToken && !force) return csrfToken;
	const r = await fetch('/auth/csrf', { credentials: 'same-origin' });
	if (!r.ok) throw new Error('session expired, please log in again');
	csrfToken = (await r.json()).csrf_token;
	return csrfToken;
}

async function send(url, opts, headers) {
	const method = (opts.method || 'GET').toUpperCase();
	if (!UNSAFE.has(method)) {
		return fetch(url, { ...opts, headers, credentials: 'same-origin' });
	}
	for (const force of [false, true]) {
		headers['X-CSRF-Token'] = await getCsrf(force);
		const r = await fetch(url, { ...opts, headers, credentials: 'same-origin' });
		if (r.status !== 403 || force) return r;
		const detail = await r.clone().text();
		if (!/csrf/i.test(detail)) return r;
	}
}

async function req(path, opts = {}) {
	const headers = { ...(opts.headers || {}) };
	if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== 'string') {
		headers['Content-Type'] = 'application/json';
		opts.body = JSON.stringify(opts.body);
	}
	const r = await send(BASE + path, opts, headers);
	if (!r.ok) {
		const text = await r.text();
		let detail;
		try { detail = JSON.parse(text).error || JSON.parse(text).detail; } catch { detail = text; }
		throw new Error(detail || `${r.status} ${r.statusText}`);
	}
	const ct = r.headers.get('content-type') || '';
	if (ct.includes('application/json')) return r.json();
	return r;
}

export const api = {
	health: () => req('/health'),
	integrations: () => req('/integrations'),
	// companies (legacy alias)
	listCompanies: () => req('/companies'),
	createCompany: (b) => req('/companies', { method: 'POST', body: b }),
	getCompany: (id) => req('/companies/' + id),
	updateCompany: (id, b) => req('/companies/' + id, { method: 'PATCH', body: b }),
	// bibles
	listBibles: (companyId) => req('/workspaces/' + companyId + '/bibles'),
	createBible: (companyId, payload, notes='') => req('/workspaces/' + companyId + '/bibles', {
		method: 'POST', body: { payload, notes }
	}),
	getBible: (id) => req('/bibles/' + id),
	approveBible: (id) => req('/bibles/' + id + '/approve', { method: 'POST' }),
	// assets
	listAssets: (companyId) => req('/workspaces/' + companyId + '/assets'),
	uploadAsset: async (companyId, file, kind='misc') => {
		const fd = new FormData();
		fd.append('file', file);
		return req('/workspaces/' + companyId + '/assets?kind=' + encodeURIComponent(kind),
			{ method: 'POST', body: fd });
	},
	assetDownloadUrl: (id) => BASE + '/assets/' + id + '/download',
	// jobs
	listJobs: (companyId) => req('/workspaces/' + companyId + '/jobs'),
	getJob: (id) => req('/jobs/' + id),
	// Workspace-scoped route (S1a): same handler semantics as the
	// legacy POST /jobs, membership checked against the URL wid.
	createJob: (b) => req('/workspaces/' + b.company_id + '/jobs', { method: 'POST', body: b }),
	jobStages: (id) => req('/jobs/' + id + '/stages'),
	jobOutputs: (id) => req('/jobs/' + id + '/outputs'),
	retryJob: (id) => req('/jobs/' + id + '/retry', { method: 'POST' }),
	outputDownloadUrl: (jobId, outId) => BASE + '/jobs/' + jobId + '/outputs/' + outId + '/download',
	// publishing
	publish: (jobId, body) => req('/jobs/' + jobId + '/publish', { method: 'POST', body }),
	publishList: (jobId) => req('/jobs/' + jobId + '/publish'),
	// credentials
	listCredentials: (companyId) => req('/workspaces/' + companyId + '/credentials'),
	putCredential: (companyId, provider, value) => {
		return req('/workspaces/' + companyId + '/credentials/' + provider, {
			method: 'PUT', body: { value },
		});
	},
	deleteCredential: (companyId, provider) => {
		return req('/workspaces/' + companyId + '/credentials/' + provider, {
			method: 'DELETE',
		});
	},
	// workspaces
	listWorkspaces: () => req('/workspaces'),
};
