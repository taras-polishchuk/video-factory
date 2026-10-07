-- migrate_workspace_id_down.sql
-- Wave 2 reversible migration: undo company_id -> workspace_id rename.
--
-- Run with:
--   python -c "import sqlite3; c=sqlite3.connect('artifacts/video_factory.db'); c.executescript(open('migrate_workspace_id_down.sql').read()); c.close()"
--
-- The script is IDEMPOTENT: re-running is a no-op for tables
-- already in the legacy state.  Forward migration (workspace_id
-- -> company_id) is handled in core/repo.py:_apply_migration_005.

PRAGMA foreign_keys=OFF;

-- ── Step 1: ensure the `companies` table exists. ─────────────
CREATE TABLE IF NOT EXISTS companies (
    company_id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

-- ── Step 2: rebuild each legacy table. ──────────────────────
-- We drop the workspace_id column (rename to company_id),
-- back-fill company_id from legacy_company_alias, and
-- recreate the FK against `companies`.

-- 2a. bible_versions
ALTER TABLE bible_versions RENAME TO bible_versions__w2;
CREATE TABLE bible_versions (
    bible_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft','approved')),
    payload_json TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
INSERT INTO bible_versions
    (bible_id, company_id, version, status, payload_json, notes,
     created_at, approved_at)
SELECT
    b.bible_id,
    COALESCE(
        (SELECT legacy_company_id FROM legacy_company_alias
         WHERE workspace_id = b.workspace_id),
        b.workspace_id
    ) AS company_id,
    b.version, b.status, b.payload_json, b.notes,
    b.created_at, b.approved_at
FROM bible_versions__w2 b;
DROP TABLE bible_versions__w2;

-- 2b. assets
ALTER TABLE assets RENAME TO assets__w2;
CREATE TABLE assets (
    asset_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER,
    checksum TEXT,
    uploaded_at TEXT NOT NULL
);
INSERT INTO assets
    (asset_id, company_id, kind, filename, storage_key, mime_type,
     size_bytes, checksum, uploaded_at)
SELECT
    a.asset_id,
    COALESCE(
        (SELECT legacy_company_id FROM legacy_company_alias
         WHERE workspace_id = a.workspace_id),
        a.workspace_id
    ) AS company_id,
    a.kind, a.filename, a.storage_key, a.mime_type,
    a.size_bytes, a.checksum, a.uploaded_at
FROM assets__w2 a;
DROP TABLE assets__w2;

-- 2c. jobs
ALTER TABLE jobs RENAME TO jobs__w2;
CREATE TABLE jobs (
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
    current_stage TEXT
);
INSERT INTO jobs
    (job_id, company_id, bible_id, topic, language, duration_seconds,
     platforms_json, aspect_ratio, count, render_profile,
     approval_required, created_at, updated_at, state, status_message,
     current_stage)
SELECT
    j.job_id,
    COALESCE(
        (SELECT legacy_company_id FROM legacy_company_alias
         WHERE workspace_id = j.workspace_id),
        j.workspace_id
    ) AS company_id,
    j.bible_id, j.topic, j.language, j.duration_seconds,
    j.platforms_json, j.aspect_ratio, j.count, j.render_profile,
    j.approval_required, j.created_at, j.updated_at,
    j.state, j.status_message, j.current_stage
FROM jobs__w2 j;
DROP TABLE jobs__w2;

-- 2d. publish_intents
ALTER TABLE publish_intents RENAME TO publish_intents__w2;
CREATE TABLE publish_intents (
    intent_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('draft','schedule','publish')),
    platforms_json TEXT NOT NULL,
    caption TEXT,
    scheduled_for TEXT,
    created_at TEXT NOT NULL,
    executed_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','done','failed')),
    detail TEXT
);
INSERT INTO publish_intents
    (intent_id, job_id, company_id, action, platforms_json, caption,
     scheduled_for, created_at, executed_at, status, detail)
SELECT
    p.intent_id, p.job_id,
    COALESCE(
        (SELECT legacy_company_id FROM legacy_company_alias
         WHERE workspace_id = p.workspace_id),
        p.workspace_id
    ) AS company_id,
    p.action, p.platforms_json, p.caption, p.scheduled_for,
    p.created_at, p.executed_at, p.status, p.detail
FROM publish_intents__w2 p;
DROP TABLE publish_intents__w2;

-- 2e. credentials
ALTER TABLE credentials RENAME TO credentials__w2;
CREATE TABLE credentials (
    credential_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    encrypted_blob BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    UNIQUE (company_id, provider)
);
INSERT INTO credentials
    (credential_id, company_id, provider, encrypted_blob, fingerprint,
     updated_at, updated_by)
SELECT
    cr.credential_id,
    COALESCE(
        (SELECT legacy_company_id FROM legacy_company_alias
         WHERE workspace_id = cr.workspace_id),
        cr.workspace_id
    ) AS company_id,
    cr.provider, cr.encrypted_blob, cr.fingerprint,
    cr.updated_at, cr.updated_by
FROM credentials__w2 cr;
DROP TABLE credentials__w2;

-- ── Step 3: rebuild indexes. ───────────────────────────────
CREATE INDEX IF NOT EXISTS idx_bible_company ON bible_versions(company_id);
CREATE INDEX IF NOT EXISTS idx_assets_company ON assets(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_credentials_company
    ON credentials(company_id);

PRAGMA foreign_keys=ON;
