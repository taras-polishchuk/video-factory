-- Oldest supported Video Factory schema: V0 (commit c3cb72e, core/repo.py SCHEMA).
-- Used by tests/test_jobs_s3.py to prove a V0 database upgrades through the
-- schema_migrations ledger. Verbatim copy; do not edit.
CREATE TABLE IF NOT EXISTS companies (
    company_id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bible_versions (
    bible_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','approved')),
    payload_json TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(company_id)
);
CREATE INDEX IF NOT EXISTS idx_bible_company ON bible_versions(company_id);

CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER,
    checksum TEXT,
    uploaded_at TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id)
);
CREATE INDEX IF NOT EXISTS idx_assets_company ON assets(company_id);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    bible_id TEXT,
    topic TEXT NOT NULL,
    language TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    platforms_json TEXT NOT NULL,
    aspect_ratio TEXT NOT NULL DEFAULT '9:16',
    count INTEGER NOT NULL DEFAULT 1,
    render_profile TEXT NOT NULL DEFAULT 'mock',
    approval_required INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    status_message TEXT,
    current_stage TEXT,
    FOREIGN KEY (company_id) REFERENCES companies(company_id),
    FOREIGN KEY (bible_id) REFERENCES bible_versions(bible_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);

CREATE TABLE IF NOT EXISTS job_stages (
    stage_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed','skipped')),
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    summary_json TEXT,
    error TEXT,
    ordinal INTEGER NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_stages_job ON job_stages(job_id);

CREATE TABLE IF NOT EXISTS job_outputs (
    output_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER,
    checksum TEXT,
    is_mock INTEGER NOT NULL DEFAULT 0,
    kind TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_outputs_job ON job_outputs(job_id);

CREATE TABLE IF NOT EXISTS publish_intents (
    intent_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('draft','schedule','publish')),
    platforms_json TEXT NOT NULL,
    caption TEXT,
    scheduled_for TEXT,
    created_at TEXT NOT NULL,
    executed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','failed')),
    detail TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (company_id) REFERENCES companies(company_id)
);

CREATE TABLE IF NOT EXISTS integrations (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'mock',
    detail TEXT,
    updated_at TEXT NOT NULL
);
