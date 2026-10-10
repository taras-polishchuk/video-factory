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

// Error thrown for every non-2xx answer. `status` lets pages choose a
// friendlier message (see describeError) instead of showing raw text.
export class ApiError extends Error {
	constructor(status, message) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
	}
}

// Turn any thrown value into a sentence a customer can act on. Never
// returns a stack trace or a bare JSON blob.
export function describeError(e) {
	const status = e && typeof e === 'object' && 'status' in e ? e.status : 0;
	if (status === 401) return 'Your session has expired. Log in again to continue.';
	if (status === 403) return e.message && !/^\s*[{<]/.test(e.message)
		? e.message
		: "You don't have access to this workspace.";
	if (status === 404) return 'That item was not found.';
	if (status >= 500) return 'The server hit a problem. Try again in a moment.';
	if (e instanceof TypeError) return "Can't reach the server. Check your connection and try again.";
	const msg = e && e.message ? String(e.message) : '';
	return msg && !/^\s*[{<]/.test(msg) ? msg : 'Something went wrong. Try again.';
}

// State-changing calls need the session's CSRF token in X-CSRF-Token
// (the API rejects them with 403 "invalid CSRF token" otherwise). The
// token comes from GET /auth/csrf and is cached for the page lifetime.
// If the lookup itself fails we still send the request: routes that do
// not require the token keep working and the API explains the rest.
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
let csrfPromise = null;

function csrfToken(force = false) {
	if (force || !csrfPromise) {
		csrfPromise = fetch('/auth/csrf', {
			credentials: 'same-origin',
			headers: { Accept: 'application/json' },
		})
			.then(async (r) => {
				if (!r.ok) throw new ApiError(r.status, 'csrf token unavailable');
				const j = await r.json();
				return j.csrf_token || '';
			})
			.catch((e) => {
				csrfPromise = null;
				throw e;
			});
	}
	return csrfPromise;
}

async function req(path, opts = {}, retriedCsrf = false) {
	const headers = { ...(opts.headers || {}) };
	let body = opts.body;
	if (body && !(body instanceof FormData) && typeof body !== 'string') {
		headers['Content-Type'] = 'application/json';
		body = JSON.stringify(body);
	}
	const method = (opts.method || 'GET').toUpperCase();
	if (!SAFE_METHODS.has(method)) {
		try {
			const token = await csrfToken(retriedCsrf);
			if (token) headers['X-CSRF-Token'] = token;
		} catch { /* send without; the API decides */ }
	}
	const r = await fetch(BASE + path, {
		...opts,
		body,
		headers,
		credentials: 'same-origin',
	});
	if (!r.ok) {
		const text = await r.text();
		let detail;
		try { detail = JSON.parse(text).error || JSON.parse(text).detail; } catch { detail = text; }
		if (r.status === 403 && !retriedCsrf && /csrf/i.test(String(detail))) {
			return req(path, opts, true);
		}
		throw new ApiError(r.status, (typeof detail === 'string' && detail) || `${r.status} ${r.statusText}`);
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
	createJob: (b) => req('/jobs', { method: 'POST', body: b }),
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
