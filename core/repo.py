"""SQLite-backed persistence for multi-company Video Factory.

Tables:
- companies: company profile
- bible_versions: Company Video Bible with draft/approved states
- assets: brand assets (logo, fonts, examples)
- jobs: video jobs
- job_stages: per-stage execution record
- job_outputs: per-asset output metadata
- publish_intents: mock draft/schedule/publish records
- integrations: provider connection state (mock-only here)

All FKs scoped to company_id. Cross-company access raises Forbidden.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional


SCHEMA = """
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

-- ── Per-company encrypted credentials (F-credential-vault) ─────────
-- One row per (company_id, provider_name). The plaintext is never
-- stored; only an encrypted blob. The repo exposes only masked
-- surfaces to the API.
CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    encrypted_blob BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    FOREIGN KEY (company_id) REFERENCES companies(company_id),
    UNIQUE (company_id, provider)
);
CREATE INDEX IF NOT EXISTS idx_credentials_company ON credentials(company_id);
"""


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Repo:
    """Thread-safe SQLite wrapper. Per-thread connections guarded by a lock."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Open one connection per thread (sqlite3 is connection-thread-local).
        import threading as _t
        self._local = _t.local()
        self._bootstrap_schema()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def _bootstrap_schema(self):
        c = self._conn()
        c.executescript(SCHEMA)
        # Wave 1: apply auth migrations idempotently.
        from core.migrations import apply_migrations
        apply_migrations(c)
        # Extend audit_log with user_id + workspace_id if absent.
        self._extend_audit_log_columns()
        for name in ("heygen", "comfy_cloud", "comfy_gpu_worker", "n8n", "publer"):
            c.execute(
                "INSERT OR IGNORE INTO integrations(name, enabled, mode, detail, updated_at) VALUES (?,?,?,?,?)",
                (name, 0, "mock", "default mock mode", now_iso()),
            )
        # F3: append runner_audit table for idempotency-mirror events.
        from control_plane.repo_helpers import RUNNER_AUDIT_SCHEMA, bootstrap_runner_audit
        c.executescript(RUNNER_AUDIT_SCHEMA)
        bootstrap_runner_audit(self)

    def _extend_audit_log_columns(self) -> None:
        """Ensure audit_log has user_id + workspace_id columns (migration 004).

        SQLite has no IF NOT EXISTS for ADD COLUMN prior to 3.35, so we
        check via PRAGMA table_info and add missing columns manually.
        Idempotent.
        """
        c = self._conn()
        cols = {row[1] for row in c.execute("PRAGMA table_info(audit_log)").fetchall()}
        if "user_id" not in cols:
            c.execute("ALTER TABLE audit_log ADD COLUMN user_id TEXT")
        if "workspace_id" not in cols:
            c.execute("ALTER TABLE audit_log ADD COLUMN workspace_id TEXT")
        c.commit()

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            c = self._conn()
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise

    # ── Companies ─────────────────────────────────────────────

    def create_company(self, *, slug: str, name: str, description: str = "") -> dict:
        company_id = new_id("cmp")
        t = now_iso()
        with self.tx() as c:
            c.execute(
                "INSERT INTO companies(company_id,slug,name,description,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (company_id, slug, name, description, t, t),
            )
        return self.get_company(company_id)

    def get_company(self, company_id: str) -> dict:
        row = self._conn().execute(
            "SELECT company_id, slug, name, description, created_at, updated_at FROM companies WHERE company_id=?",
            (company_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"company {company_id} not found")
        return self._row_company(row)

    def list_companies(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT company_id, slug, name, description, created_at, updated_at FROM companies ORDER BY created_at"
        ).fetchall()
        return [self._row_company(r) for r in rows]

    def update_company(self, company_id: str, *, name: Optional[str] = None,
                        description: Optional[str] = None) -> dict:
        with self.tx() as c:
            existing = self.get_company(company_id)
            new_name = name if name is not None else existing["name"]
            new_desc = description if description is not None else existing["description"]
            c.execute(
                "UPDATE companies SET name=?, description=?, updated_at=? WHERE company_id=?",
                (new_name, new_desc, now_iso(), company_id),
            )
        return self.get_company(company_id)

    def _row_company(self, row) -> dict:
        return {
            "company_id": row[0],
            "slug": row[1],
            "name": row[2],
            "description": row[3] or "",
            "created_at": row[4],
            "updated_at": row[5],
        }

    # ── Bible ─────────────────────────────────────────────────

    def create_bible(self, company_id: str, *, payload: dict, notes: str = "") -> dict:
        self.get_company(company_id)  # 404 if missing
        with self.tx() as c:
            version = (c.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM bible_versions WHERE company_id=?",
                (company_id,),
            ).fetchone()[0])
            bible_id = new_id("bbl")
            c.execute(
                "INSERT INTO bible_versions(bible_id,company_id,version,status,payload_json,notes,created_at,approved_at) VALUES (?,?,?,?,?,?,?,?)",
                (bible_id, company_id, version, "draft", json.dumps(payload), notes, now_iso(), None),
            )
        return self.get_bible(bible_id)

    def approve_bible(self, bible_id: str) -> dict:
        with self.tx() as c:
            row = c.execute("SELECT company_id, status, version FROM bible_versions WHERE bible_id=?",
                            (bible_id,)).fetchone()
            if not row:
                raise NotFound(f"bible {bible_id} not found")
            if row[1] != "draft":
                raise ValueError("only draft bibles can be approved")
            c.execute("UPDATE bible_versions SET status='approved', approved_at=? WHERE bible_id=?",
                      (now_iso(), bible_id))
        return self.get_bible(bible_id)

    def get_bible(self, bible_id: str) -> dict:
        row = self._conn().execute(
            "SELECT bible_id, company_id, version, status, payload_json, notes, created_at, approved_at FROM bible_versions WHERE bible_id=?",
            (bible_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"bible {bible_id} not found")
        return self._row_bible(row)

    def list_bibles(self, company_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT bible_id, company_id, version, status, payload_json, notes, created_at, approved_at FROM bible_versions WHERE company_id=? ORDER BY version DESC",
            (company_id,),
        ).fetchall()
        return [self._row_bible(r) for r in rows]

    def _row_bible(self, row) -> dict:
        return {
            "bible_id": row[0],
            "company_id": row[1],
            "version": row[2],
            "status": row[3],
            "payload": json.loads(row[4]) if row[4] else {},
            "notes": row[5] or "",
            "created_at": row[6],
            "approved_at": row[7],
        }

    # ── Assets ────────────────────────────────────────────────

    def create_asset(self, company_id: str, *, kind: str, filename: str,
                     storage_key: str, mime_type: str, size_bytes: int,
                     checksum: str) -> dict:
        self.get_company(company_id)
        asset_id = new_id("ast")
        with self.tx() as c:
            c.execute(
                "INSERT INTO assets(asset_id,company_id,kind,filename,storage_key,mime_type,size_bytes,checksum,uploaded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (asset_id, company_id, kind, filename, storage_key, mime_type, size_bytes, checksum, now_iso()),
            )
        return self.get_asset(asset_id)

    def get_asset(self, asset_id: str) -> dict:
        row = self._conn().execute(
            "SELECT asset_id, company_id, kind, filename, storage_key, mime_type, size_bytes, checksum, uploaded_at FROM assets WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"asset {asset_id} not found")
        return self._row_asset(row)

    def list_assets(self, company_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT asset_id, company_id, kind, filename, storage_key, mime_type, size_bytes, checksum, uploaded_at FROM assets WHERE company_id=? ORDER BY uploaded_at DESC",
            (company_id,),
        ).fetchall()
        return [self._row_asset(r) for r in rows]

    def _row_asset(self, row) -> dict:
        return {
            "asset_id": row[0],
            "company_id": row[1],
            "kind": row[2],
            "filename": row[3],
            "storage_key": row[4],
            "mime_type": row[5] or "",
            "size_bytes": row[6] or 0,
            "checksum": row[7] or "",
            "uploaded_at": row[8],
        }

    # ── Jobs ──────────────────────────────────────────────────

    def create_job(self, company_id: str, *, topic: str, language: str,
                   duration_seconds: int, platforms: list[str],
                   aspect_ratio: str = "9:16", count: int = 1,
                   render_profile: str = "mock", bible_id: Optional[str] = None,
                   approval_required: bool = False) -> dict:
        self.get_company(company_id)
        if bible_id:
            b = self.get_bible(bible_id)
            if b["company_id"] != company_id:
                raise Forbidden("bible does not belong to company")
            if b["status"] != "approved":
                raise ValueError("bible must be approved before job creation")
        job_id = new_id("job")
        t = now_iso()
        with self.tx() as c:
            c.execute(
                """INSERT INTO jobs(job_id, company_id, bible_id, topic, language, duration_seconds, platforms_json, aspect_ratio, count, render_profile, approval_required, created_at, updated_at, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, company_id, bible_id, topic, language, duration_seconds,
                 json.dumps(platforms), aspect_ratio, count, render_profile,
                 1 if approval_required else 0, t, t, "queued"),
            )
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields) -> dict:
        with self.tx() as c:
            row = c.execute("SELECT company_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise NotFound(f"job {job_id} not found")
            keys = list(fields.keys())
            sets = ",".join(f"{k}=?" for k in keys)
            vals = [fields[k] for k in keys]
            c.execute(f"UPDATE jobs SET {sets}, updated_at=? WHERE job_id=?",
                      (*vals, now_iso(), job_id))
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict:
        row = self._conn().execute(
            """SELECT job_id, company_id, bible_id, topic, language, duration_seconds, platforms_json, aspect_ratio, count, render_profile, approval_required, created_at, updated_at, state, status_message, current_stage FROM jobs WHERE job_id=?""",
            (job_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"job {job_id} not found")
        return self._row_job(row)

    def list_jobs(self, company_id: Optional[str] = None,
                  state: Optional[str] = None) -> list[dict]:
        q = """SELECT job_id, company_id, bible_id, topic, language, duration_seconds, platforms_json, aspect_ratio, count, render_profile, approval_required, created_at, updated_at, state, status_message, current_stage FROM jobs WHERE 1=1"""
        params: list = []
        if company_id:
            q += " AND company_id=?"
            params.append(company_id)
        if state:
            q += " AND state=?"
            params.append(state)
        q += " ORDER BY created_at DESC"
        rows = self._conn().execute(q, params).fetchall()
        return [self._row_job(r) for r in rows]

    def _row_job(self, row) -> dict:
        return {
            "job_id": row[0],
            "company_id": row[1],
            "bible_id": row[2],
            "topic": row[3],
            "language": row[4],
            "duration_seconds": row[5],
            "platforms": json.loads(row[6]) if row[6] else [],
            "aspect_ratio": row[7],
            "count": row[8],
            "render_profile": row[9],
            "approval_required": bool(row[10]),
            "created_at": row[11],
            "updated_at": row[12],
            "state": row[13],
            "status_message": row[14] or "",
            "current_stage": row[15] or "",
        }

    # ── Stages ────────────────────────────────────────────────

    def upsert_stage(self, *, job_id: str, stage_key: str, ordinal: int,
                     status: str, started_at: Optional[str] = None,
                     finished_at: Optional[str] = None, duration_ms: Optional[int] = None,
                     summary: Optional[dict] = None, error: Optional[str] = None) -> dict:
        stage_id = new_id("stg")
        with self.tx() as c:
            existing = c.execute(
                "SELECT stage_id FROM job_stages WHERE job_id=? AND stage_key=?",
                (job_id, stage_key),
            ).fetchone()
            if existing:
                stage_id = existing[0]
                c.execute(
                    "UPDATE job_stages SET status=?, started_at=COALESCE(?,started_at), finished_at=COALESCE(?,finished_at), duration_ms=COALESCE(?,duration_ms), summary_json=COALESCE(?,summary_json), error=? WHERE stage_id=?",
                    (status, started_at, finished_at, duration_ms,
                     json.dumps(summary) if summary is not None else None, error, stage_id),
                )
            else:
                c.execute(
                    "INSERT INTO job_stages(stage_id,job_id,stage_key,ordinal,status,started_at,finished_at,duration_ms,summary_json,error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (stage_id, job_id, stage_key, ordinal, status, started_at, finished_at, duration_ms,
                     json.dumps(summary) if summary is not None else None, error),
                )
        return self.get_stage(stage_id)

    def get_stage(self, stage_id: str) -> dict:
        row = self._conn().execute(
            "SELECT stage_id, job_id, stage_key, ordinal, status, started_at, finished_at, duration_ms, summary_json, error FROM job_stages WHERE stage_id=?",
            (stage_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"stage {stage_id} not found")
        return self._row_stage(row)

    def list_stages(self, job_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT stage_id, job_id, stage_key, ordinal, status, started_at, finished_at, duration_ms, summary_json, error FROM job_stages WHERE job_id=? ORDER BY ordinal",
            (job_id,),
        ).fetchall()
        return [self._row_stage(r) for r in rows]

    def _row_stage(self, row) -> dict:
        return {
            "stage_id": row[0],
            "job_id": row[1],
            "stage_key": row[2],
            "ordinal": row[3],
            "status": row[4],
            "started_at": row[5],
            "finished_at": row[6],
            "duration_ms": row[7],
            "summary": json.loads(row[8]) if row[8] else {},
            "error": row[9],
        }

    # ── Outputs ───────────────────────────────────────────────

    def create_output(self, job_id: str, *, storage_key: str, filename: str,
                      mime_type: str, size_bytes: int, checksum: str,
                      is_mock: bool = True, kind: str = "video") -> dict:
        output_id = new_id("out")
        with self.tx() as c:
            c.execute(
                "INSERT INTO job_outputs(output_id,job_id,storage_key,filename,mime_type,size_bytes,checksum,is_mock,kind,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (output_id, job_id, storage_key, filename, mime_type, size_bytes, checksum,
                 1 if is_mock else 0, kind, now_iso()),
            )
        return self.get_output(output_id)

    def get_output(self, output_id: str) -> dict:
        row = self._conn().execute(
            "SELECT output_id, job_id, storage_key, filename, mime_type, size_bytes, checksum, is_mock, kind, created_at FROM job_outputs WHERE output_id=?",
            (output_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"output {output_id} not found")
        return self._row_output(row)

    def list_outputs(self, job_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT output_id, job_id, storage_key, filename, mime_type, size_bytes, checksum, is_mock, kind, created_at FROM job_outputs WHERE job_id=? ORDER BY created_at",
            (job_id,),
        ).fetchall()
        return [self._row_output(r) for r in rows]

    def _row_output(self, row) -> dict:
        return {
            "output_id": row[0],
            "job_id": row[1],
            "storage_key": row[2],
            "filename": row[3],
            "mime_type": row[4] or "",
            "size_bytes": row[5] or 0,
            "checksum": row[6] or "",
            "is_mock": bool(row[7]),
            "kind": row[8],
            "created_at": row[9],
        }

    # ── Publishing ────────────────────────────────────────────

    def create_publish_intent(self, *, job_id: str, company_id: str,
                              action: str, platforms: list[str],
                              caption: str = "", scheduled_for: Optional[str] = None) -> dict:
        # F7: cross-company guard. A publish intent must reference a job
        # that actually belongs to the calling company. Without this
        # check, any caller could file a publish intent for another
        # tenant's job — they would see the intent detail and could
        # potentially pivot to live publishing once enabled.
        job = self.get_job(job_id)
        if job["company_id"] != company_id:
            raise Forbidden(
                f"job {job_id} belongs to company {job['company_id']!r}; "
                f"cannot publish on behalf of {company_id!r}"
            )
        if action not in ("draft", "schedule", "publish"):
            raise ValueError(f"invalid action {action}")
        # Validate that platforms are recognized by the intake validator.
        try:
            from control_plane.campaign import PlatformSpec
            for p in platforms:
                PlatformSpec.from_name(p)
        except ValueError as e:
            raise ValueError(str(e)) from e
        intent_id = new_id("pub")
        with self.tx() as c:
            c.execute(
                "INSERT INTO publish_intents(intent_id,job_id,company_id,action,platforms_json,caption,scheduled_for,created_at,status) VALUES (?,?,?,?,?,?,?,?,?)",
                (intent_id, job_id, company_id, action, json.dumps(platforms), caption,
                 scheduled_for, now_iso(), "pending"),
            )
        return self.get_publish_intent(intent_id)

    def update_publish_intent(self, intent_id: str, *, status: str,
                              detail: str = "") -> dict:
        with self.tx() as c:
            c.execute(
                "UPDATE publish_intents SET status=?, executed_at=?, detail=? WHERE intent_id=?",
                (status, now_iso(), detail, intent_id),
            )
        return self.get_publish_intent(intent_id)

    def get_publish_intent(self, intent_id: str) -> dict:
        row = self._conn().execute(
            "SELECT intent_id, job_id, company_id, action, platforms_json, caption, scheduled_for, created_at, executed_at, status, detail FROM publish_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"intent {intent_id} not found")
        return self._row_intent(row)

    def list_publish_intents(self, job_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT intent_id, job_id, company_id, action, platforms_json, caption, scheduled_for, created_at, executed_at, status, detail FROM publish_intents WHERE job_id=? ORDER BY created_at DESC",
            (job_id,),
        ).fetchall()
        return [self._row_intent(r) for r in rows]

    def _row_intent(self, row) -> dict:
        return {
            "intent_id": row[0],
            "job_id": row[1],
            "company_id": row[2],
            "action": row[3],
            "platforms": json.loads(row[4]) if row[4] else [],
            "caption": row[5] or "",
            "scheduled_for": row[6],
            "created_at": row[7],
            "executed_at": row[8],
            "status": row[9],
            "detail": row[10] or "",
        }

    # ── Integrations ──────────────────────────────────────────

    def list_integrations(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT name, enabled, mode, detail, updated_at FROM integrations ORDER BY name"
        ).fetchall()
        return [
            {"name": r[0], "enabled": bool(r[1]), "mode": r[2],
             "detail": r[3] or "", "updated_at": r[4]}
            for r in rows
        ]

    # ── Credentials (F-credential-vault) ─────────────────────────

    # Providers we will accept secrets for. Anything outside this set is
    # rejected at the repo layer so a typo cannot silently store a blob.
    CREDENTIAL_PROVIDERS = {
        "heygen_direct", "comfy_cloud", "comfy_gpu_worker",
        "comfy_heygen_partner", "publer",
    }

    def upsert_credential(self, *, company_id: str, provider: str,
                            fingerprint_str: str, encrypted_blob: bytes,
                            updated_by: str) -> dict:
        """Store or replace the encrypted credential for (company, provider).

        The plaintext never enters the repo: callers encrypt via
        control_plane.secret_vault and pass the bytes. Returns the
        masked surface (NEVER the plaintext).
        """
        if provider not in self.CREDENTIAL_PROVIDERS:
            raise ValueError(f"unknown provider: {provider!r}")
        self.get_company(company_id)  # 404 if missing
        with self.tx() as c:
            existing = c.execute(
                "SELECT credential_id FROM credentials WHERE company_id=? AND provider=?",
                (company_id, provider),
            ).fetchone()
            if existing:
                c.execute(
                    """UPDATE credentials
                       SET encrypted_blob=?, fingerprint=?, updated_at=?, credential_id=?
                       WHERE company_id=? AND provider=?""",
                    (encrypted_blob, fingerprint_str, now_iso(), existing[0],
                     company_id, provider),
                )
                cid = existing[0]
            else:
                cid = new_id("cred")
                c.execute(
                    """INSERT INTO credentials(credential_id, company_id, provider,
                                              encrypted_blob, fingerprint,
                                              updated_at, updated_by)
                       VALUES (?,?,?,?,?,?,?)""",
                    (cid, company_id, provider, encrypted_blob, fingerprint_str,
                     now_iso(), updated_by),
                )
        return self.get_credential(company_id, provider)

    def get_credential(self, company_id: str, provider: str) -> dict:
        row = self._conn().execute(
            "SELECT credential_id, company_id, provider, fingerprint, updated_at, updated_by "
            "FROM credentials WHERE company_id=? AND provider=?",
            (company_id, provider),
        ).fetchone()
        if not row:
            raise NotFound(f"credential {provider!r} for company {company_id!r} not found")
        return {
            "credential_id": row[0],
            "company_id": row[1],
            "provider": row[2],
            "has_value": True,
            "fingerprint": row[3],
            "updated_at": row[4],
            "updated_by": row[5],
        }

    def list_credentials(self, company_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT credential_id, provider, fingerprint, updated_at, updated_by "
            "FROM credentials WHERE company_id=? ORDER BY provider",
            (company_id,),
        ).fetchall()
        return [
            {
                "credential_id": r[0],
                "company_id": company_id,
                "provider": r[1],
                "has_value": True,
                "fingerprint": r[2],
                "updated_at": r[3],
                "updated_by": r[4],
            }
            for r in rows
        ]

    def delete_credential(self, *, company_id: str, provider: str) -> None:
        with self.tx() as c:
            existing = c.execute(
                "SELECT credential_id FROM credentials WHERE company_id=? AND provider=?",
                (company_id, provider),
            ).fetchone()
            if not existing:
                raise NotFound(f"credential {provider!r} for company {company_id!r} not found")
            c.execute(
                "DELETE FROM credentials WHERE company_id=? AND provider=?",
                (company_id, provider),
            )

    def decrypt_credential(self, *, company_id: str, provider: str) -> str:
        """Return the plaintext for (company, provider).

        INTERNAL USE ONLY — never exposed through any API surface.
        The render plane calls this at submit() time. Any caller that
        surfaces the return through JSON is the leak.
        """
        from control_plane.secret_vault import decrypt as _decrypt, SecretReadError
        row = self._conn().execute(
            "SELECT encrypted_blob FROM credentials WHERE company_id=? AND provider=?",
            (company_id, provider),
        ).fetchone()
        if not row:
            raise NotFound(f"no credential for {company_id}@{provider}")
        try:
            return _decrypt(provider, row[0])
        except SecretReadError as e:
            raise SecretReadError(
                f"credential for {company_id}@{provider} unreadable: {e}. "
                f"Likely cause: VIDEO_FACTORY_SECRET_KEY changed since the secret was written. "
                f"Operator must re-enter the secret."
            ) from e

    # ── Auth: users ───────────────────────────────────────────
    # All methods here are added by Wave 1 (auth foundations).
    # They live on Repo so the DB lock and per-thread connection
    # handling stay centralized.

    def create_user(self, *, email: str, password_hash: str) -> dict:
        user_id = new_id("usr")
        t = now_iso()
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO users(user_id, email, password_hash, status, "
                    "email_verified, created_at) VALUES (?,?,?,?,?,?)",
                    (user_id, email.lower(), password_hash, "active", 0, t),
                )
        except Exception as e:
            if "UNIQUE constraint failed" in str(e):
                raise ValueError(f"email {email!r} already exists") from e
            raise
        return self.get_user(user_id)

    def get_user_by_email(self, email: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT user_id, email, password_hash, status, email_verified, "
            "created_at, last_login_at FROM users WHERE email=?",
            (email.lower(),),
        ).fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "email": row[1],
            "password_hash": row[2],
            "status": row[3],
            "email_verified": bool(row[4]),
            "created_at": row[5],
            "last_login_at": row[6],
        }

    def get_user(self, user_id: str) -> dict:
        row = self._conn().execute(
            "SELECT user_id, email, password_hash, status, email_verified, "
            "created_at, last_login_at FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"user {user_id} not found")
        return {
            "user_id": row[0],
            "email": row[1],
            "password_hash": row[2],
            "status": row[3],
            "email_verified": bool(row[4]),
            "created_at": row[5],
            "last_login_at": row[6],
        }

    def update_user_last_login(self, user_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET last_login_at=? WHERE user_id=?",
                (now_iso(), user_id),
            )

    def update_user_status(self, user_id: str, *, status: str) -> None:
        if status not in ("active", "locked", "pending"):
            raise ValueError(f"invalid user status: {status!r}")
        with self.tx() as c:
            c.execute(
                "UPDATE users SET status=? WHERE user_id=?",
                (status, user_id),
            )

    def mark_user_email_verified(self, user_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE users SET email_verified=1 WHERE user_id=?",
                (user_id,),
            )

    # ── Auth: workspaces ──────────────────────────────────────

    def create_workspace(self, *, slug: str, name: str, owner_user_id: str,
                           plan: str = "free") -> dict:
        workspace_id = new_id("wks")
        t = now_iso()
        if plan not in ("free", "pro", "enterprise"):
            raise ValueError(f"invalid plan: {plan!r}")
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO workspaces(workspace_id, slug, name, "
                    "owner_user_id, plan, status, live_enabled, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (workspace_id, slug.lower(), name, owner_user_id, plan,
                     "active", 0, t),
                )
        except Exception as e:
            if "UNIQUE constraint failed" in str(e) and "slug" in str(e):
                raise ValueError(f"workspace slug {slug!r} already exists") from e
            raise
        return self.get_workspace(workspace_id)

    def get_workspace(self, workspace_id: str) -> dict:
        row = self._conn().execute(
            "SELECT workspace_id, slug, name, owner_user_id, plan, status, "
            "live_enabled, stripe_customer_id, stripe_subscription_id, "
            "created_at FROM workspaces WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"workspace {workspace_id} not found")
        return self._row_workspace(row)

    def get_workspace_by_slug(self, slug: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT workspace_id, slug, name, owner_user_id, plan, status, "
            "live_enabled, stripe_customer_id, stripe_subscription_id, "
            "created_at FROM workspaces WHERE slug=?",
            (slug.lower(),),
        ).fetchone()
        if not row:
            return None
        return self._row_workspace(row)

    def _row_workspace(self, row) -> dict:
        return {
            "workspace_id": row[0],
            "slug": row[1],
            "name": row[2],
            "owner_user_id": row[3],
            "plan": row[4],
            "status": row[5],
            "live_enabled": bool(row[6]),
            "stripe_customer_id": row[7],
            "stripe_subscription_id": row[8],
            "created_at": row[9],
        }

    # ── Auth: memberships ─────────────────────────────────────

    def add_membership(self, *, user_id: str, workspace_id: str,
                         role: str) -> dict:
        if role not in ("owner", "admin", "editor", "viewer"):
            raise ValueError(f"invalid role: {role!r}")
        # Verify both ends exist; cheaper than the FK error.
        self.get_user(user_id)
        self.get_workspace(workspace_id)
        t = now_iso()
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO memberships(user_id, workspace_id, "
                "role, created_at) VALUES (?,?,?,?)",
                (user_id, workspace_id, role, t),
            )
        return {"user_id": user_id, "workspace_id": workspace_id,
                "role": role, "created_at": t}

    def list_memberships(self, user_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT user_id, workspace_id, role, created_at FROM memberships "
            "WHERE user_id=?",
            (user_id,),
        ).fetchall()
        return [
            {"user_id": r[0], "workspace_id": r[1], "role": r[2],
             "created_at": r[3]}
            for r in rows
        ]

    # ── Auth: sessions ────────────────────────────────────────

    def create_session(self, *, user_id: str, csrf_token: str,
                        ip: Optional[str], user_agent: Optional[str],
                        expires_at: str, session_id: Optional[str] = None) -> dict:
        """Create a session row.

        `session_id` is generated by the caller (using a
        URL-safe token) and is what goes into the cookie. The
        default `new_id("ses")` would be too short to be
        unpredictable; callers MUST pass their own id. We allow
        the default for tests only.
        """
        sid = session_id or new_id("ses")
        t = now_iso()
        with self.tx() as c:
            c.execute(
                "INSERT INTO sessions(session_id, user_id, csrf_token, ip, "
                "user_agent, expires_at, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, user_id, csrf_token, ip, user_agent,
                 expires_at, t),
            )
        return {
            "session_id": sid,
            "user_id": user_id,
            "csrf_token": csrf_token,
            "ip": ip,
            "user_agent": user_agent,
            "expires_at": expires_at,
            "created_at": t,
        }

    def get_session(self, session_id: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT session_id, user_id, csrf_token, ip, user_agent, "
            "expires_at, created_at, last_seen_at FROM sessions "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "session_id": row[0],
            "user_id": row[1],
            "csrf_token": row[2],
            "ip": row[3],
            "user_agent": row[4],
            "expires_at": row[5],
            "created_at": row[6],
            "last_seen_at": row[7],
        }

    def delete_session(self, session_id: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE session_id=?",
                      (session_id,))

    def delete_sessions_for_user(self, user_id: str) -> int:
        """Force-logout: remove every session for a user. Returns count."""
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM sessions WHERE user_id=?", (user_id,),
            )
            return cur.rowcount

    def gc_expired_sessions(self) -> int:
        with self.tx() as c:
            cur = c.execute(
                "DELETE FROM sessions WHERE expires_at < ?", (now_iso(),),
            )
            return cur.rowcount

    # ── Auth: email verifications / password resets ───────────

    def create_email_verification(self, *, user_id: str, token: str,
                                    expires_at: str) -> dict:
        with self.tx() as c:
            c.execute(
                "INSERT INTO email_verifications(token, user_id, expires_at, "
                "created_at) VALUES (?,?,?,?)",
                (token, user_id, expires_at, now_iso()),
            )
        return {"token": token, "user_id": user_id, "expires_at": expires_at,
                "consumed_at": None, "created_at": now_iso()}

    def get_email_verification(self, token: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT token, user_id, expires_at, consumed_at, created_at "
            "FROM email_verifications WHERE token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        return {"token": row[0], "user_id": row[1], "expires_at": row[2],
                "consumed_at": row[3], "created_at": row[4]}

    def consume_email_verification(self, token: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE email_verifications SET consumed_at=? WHERE token=?",
                (now_iso(), token),
            )

    def create_password_reset(self, *, user_id: str, token: str,
                                expires_at: str) -> dict:
        with self.tx() as c:
            c.execute(
                "INSERT INTO password_resets(token, user_id, expires_at, "
                "created_at) VALUES (?,?,?,?)",
                (token, user_id, expires_at, now_iso()),
            )
        return {"token": token, "user_id": user_id, "expires_at": expires_at,
                "consumed_at": None, "created_at": now_iso()}

    def get_password_reset(self, token: str) -> Optional[dict]:
        row = self._conn().execute(
            "SELECT token, user_id, expires_at, consumed_at, created_at "
            "FROM password_resets WHERE token=?",
            (token,),
        ).fetchone()
        if not row:
            return None
        return {"token": row[0], "user_id": row[1], "expires_at": row[2],
                "consumed_at": row[3], "created_at": row[4]}

    def consume_password_reset(self, token: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE password_resets SET consumed_at=? WHERE token=?",
                (now_iso(), token),
            )

    # ── Auth: aliases for the multi-tenant transition ─────────

    def workspace_id_for_company(self, company_id: str) -> Optional[str]:
        """Return the workspace_id for a company_id, if one exists.

        Wave 2 will replace companies with workspaces outright; for
        now this is a no-op lookup that returns None for legacy
        companies that do not have a corresponding workspace. When
        the migration in Wave 2 lands, we back-fill workspaces and
        this method will then resolve.
        """
        row = self._conn().execute(
            "SELECT workspace_id FROM workspaces WHERE owner_user_id IN "
            "(SELECT user_id FROM users WHERE email=?)",
            (f"legacy::{company_id}",),
        ).fetchone()
        return row[0] if row else None