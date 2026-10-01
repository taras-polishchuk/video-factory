// API client. All paths are scoped to the company_id the operator
// selected on the workspace page. Company-scoped queries are required
// because the API enforces isolation; this client surfaces 403/404.
const BASE = '/api/v1';

async function req(path, opts = {}) {
  const headers = opts.headers || {};
  if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(BASE + path, { ...opts, headers });
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
  // companies
  listCompanies: () => req('/companies'),
  createCompany: (b) => req('/companies', { method: 'POST', body: b }),
  getCompany: (id) => req('/companies/' + id),
  updateCompany: (id, b) => req('/companies/' + id, { method: 'PATCH', body: b }),
  // bibles
  listBibles: (companyId) => req('/companies/' + companyId + '/bibles'),
  createBible: (companyId, payload, notes='') => req('/companies/' + companyId + '/bibles', {
    method: 'POST', body: { payload, notes }
  }),
  getBible: (id) => req('/bibles/' + id),
  approveBible: (id) => req('/bibles/' + id + '/approve', { method: 'POST' }),
  // assets
  listAssets: (companyId) => req('/companies/' + companyId + '/assets'),
  uploadAsset: (companyId, kind, file) => {
    const fd = new FormData();
    fd.append('file', file);
    return fetch(BASE + '/companies/' + companyId + '/assets?kind=' + encodeURIComponent(kind),
      { method: 'POST', body: fd }).then(r => {
        if (!r.ok) throw new Error(r.status + ' upload failed');
        return r.json();
      });
  },
  assetDownloadUrl: (id) => BASE + '/assets/' + id + '/download',
  // jobs
  listJobs: (companyId) => req('/jobs?company_id=' + companyId),
  getJob: (id) => req('/jobs/' + id),
  createJob: (b) => req('/jobs', { method: 'POST', body: b }),
  jobStages: (id) => req('/jobs/' + id + '/stages'),
  jobOutputs: (id) => req('/jobs/' + id + '/outputs'),
  retryJob: (id) => req('/jobs/' + id + '/retry', { method: 'POST' }),
  outputDownloadUrl: (jobId, outId) => BASE + '/jobs/' + jobId + '/outputs/' + outId + '/download',
  // publishing
  publish: (jobId, body) => req('/jobs/' + jobId + '/publish', { method: 'POST', body }),
  publishList: (jobId) => req('/jobs/' + jobId + '/publish')
};