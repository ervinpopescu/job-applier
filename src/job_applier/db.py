from __future__ import annotations

import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pandas as pd  # type: ignore[import-untyped]

from job_applier.utils import get_project_root

_DB_LOCK = threading.Lock()
CURRENT_SCHEMA_VERSION = 5


def get_db_path(custom_path: Path | None = None) -> Path:
    """Returns the path to the SQLite database file."""
    if custom_path:
        return Path(custom_path)
    env_path = os.environ.get("JOB_APPLIER_DB_PATH")
    if env_path:
        p = Path(env_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    project_root = get_project_root()
    db_file = project_root / "data" / "job_applier.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    return db_file


def get_connection(custom_path: Path | None = None) -> sqlite3.Connection:
    """Creates a connection with WAL mode enabled for concurrent reading/writing."""
    path = get_db_path(custom_path)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def backup_db(target_path: Path, custom_path: Path | None = None) -> Path:
    """
    Safely takes an atomic online snapshot of the live SQLite database (including WAL journal pages)
    using SQLite's online Backup API. Safe to run against a live concurrent database without stopping services.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source_conn = get_connection(custom_path)
    dest_conn = sqlite3.connect(str(target_path))
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()
    return target_path


def get_schema_version(custom_path: Path | None = None) -> int:
    """Returns current schema migration version recorded in database."""
    conn = get_connection(custom_path)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
        )
        if not cur.fetchone():
            legacy = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='applications';"
            ).fetchone()
            return 1 if legacy else 0
        row = conn.execute("SELECT MAX(version) FROM schema_migrations;").fetchone()
        return row[0] if row and row[0] is not None else 0
    finally:
        conn.close()


def _migration_1_baseline(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            job_url TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'Generic',
            status TEXT NOT NULL DEFAULT 'pending',
            submission_type TEXT NOT NULL DEFAULT 'manual',
            folder_name TEXT NOT NULL,
            cv_filename TEXT DEFAULT '',
            has_cover_letter INTEGER DEFAULT 0,
            proof_screenshot TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_url ON applications(job_url);")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_jobs (
            job_url TEXT PRIMARY KEY,
            app_id TEXT,
            processed_at TEXT NOT NULL
        );
        """
    )


def _migration_2_automation(conn: sqlite3.Connection) -> None:
    # Check if has_artifacts column exists in applications
    columns = [
        r[1] for r in conn.execute("PRAGMA table_info(applications);").fetchall()
    ]
    if "has_artifacts" not in columns:
        conn.execute(
            "ALTER TABLE applications ADD COLUMN has_artifacts INTEGER DEFAULT 1;"
        )

    # Automation jobs queue table
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_jobs (
            id TEXT PRIMARY KEY,
            app_id TEXT NOT NULL,
            adapter TEXT NOT NULL DEFAULT 'generic',
            adapter_version TEXT NOT NULL DEFAULT '1.0.0',
            state TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            lease_owner TEXT,
            lease_expires_at TEXT,
            fencing_generation INTEGER NOT NULL DEFAULT 0,
            checkpoint TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 3,
            next_retry_at TEXT,
            error_code TEXT,
            error_message TEXT,
            is_cancelled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (app_id) REFERENCES applications(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_state_prio ON automation_jobs(state, priority DESC, created_at ASC);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_app_id ON automation_jobs(app_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_lease ON automation_jobs(lease_owner, lease_expires_at);"
    )

    # Application attempts immutable audit log
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS application_attempts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            profile_snapshot TEXT,
            resume_snapshot TEXT,
            artifact_revisions TEXT,
            submit_intent_at TEXT,
            outcome TEXT,
            confirmation_evidence TEXT,
            error_details TEXT,
            is_ambiguous INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (job_id) REFERENCES automation_jobs(id) ON DELETE CASCADE,
            FOREIGN KEY (app_id) REFERENCES applications(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_job ON application_attempts(job_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_attempts_app ON application_attempts(app_id);"
    )

    # Progress and streaming events
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            app_id TEXT,
            event_type TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'INFO',
            step TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL,
            details_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_job_id ON automation_events(job_id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_created ON automation_events(created_at);"
    )

    # Approved screening question answers
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approved_answers (
            id TEXT PRIMARY KEY,
            question_key TEXT NOT NULL,
            answer_value TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT 'profile',
            sensitivity_class TEXT NOT NULL DEFAULT 'standard',
            scope TEXT NOT NULL DEFAULT 'global',
            approval_status TEXT NOT NULL DEFAULT 'approved',
            revision INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_answers_key_scope ON approved_answers(question_key, scope);"
    )

    # Runtime controls (pause, stop, manual takeover)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            is_paused INTEGER NOT NULL DEFAULT 0,
            is_stopped INTEGER NOT NULL DEFAULT 0,
            manual_takeover_owner TEXT,
            manual_takeover_expires_at TEXT,
            is_waiting_for_code INTEGER NOT NULL DEFAULT 0,
            pending_verification_code TEXT,
            browser_active INTEGER NOT NULL DEFAULT 0,
            browser_url TEXT,
            browser_page_text TEXT,
            browser_is_closed INTEGER NOT NULL DEFAULT 1,
            browser_updated_at TEXT,
            browser_job_id TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    existing_rc_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(runtime_control);").fetchall()
    }
    for col_name, col_def in [
        ("is_waiting_for_code", "INTEGER NOT NULL DEFAULT 0"),
        ("pending_verification_code", "TEXT"),
        ("browser_active", "INTEGER NOT NULL DEFAULT 0"),
        ("browser_url", "TEXT"),
        ("browser_page_text", "TEXT"),
        ("browser_is_closed", "INTEGER NOT NULL DEFAULT 1"),
        ("browser_updated_at", "TEXT"),
        ("browser_job_id", "TEXT"),
    ]:
        if col_name not in existing_rc_cols:
            conn.execute(
                f"ALTER TABLE runtime_control ADD COLUMN {col_name} {col_def};"
            )

    conn.execute(
        """
        INSERT OR IGNORE INTO runtime_control (id, is_paused, is_stopped, updated_at)
        VALUES (1, 0, 0, datetime('now'));
        """
    )

    # Notification outbox
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_outbox (
            id TEXT PRIMARY KEY,
            dedup_key TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            category TEXT NOT NULL,
            urgency TEXT NOT NULL DEFAULT 'normal',
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            url TEXT,
            payload_json TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            next_retry_at TEXT
        );
        """
    )
    existing_outbox_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(notification_outbox);").fetchall()
    }
    if "next_retry_at" not in existing_outbox_cols:
        conn.execute("ALTER TABLE notification_outbox ADD COLUMN next_retry_at TEXT;")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbox_status ON notification_outbox(status, created_at);"
    )


def _migration_3_notifications(conn: sqlite3.Connection) -> None:
    """Migration 3: Durable dashboard notifications, severity, and idempotent acknowledgements."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id TEXT UNIQUE NOT NULL,
            job_id TEXT,
            app_id TEXT,
            event_id INTEGER,
            category TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            url TEXT,
            details_json TEXT,
            acknowledged INTEGER NOT NULL DEFAULT 0,
            acknowledged_at TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_id ON notifications(id);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_ack ON notifications(acknowledged, created_at);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notifications_job ON notifications(job_id);"
    )


def _migration_5_automation_observability(conn: sqlite3.Connection) -> None:
    """Migration 5: durable, fenced automation event metadata and status projection."""
    event_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(automation_events);").fetchall()
    }
    for name, definition in [
        ("attempt_id", "TEXT"),
        ("event_key", "TEXT"),
        ("attempt_number", "INTEGER"),
        ("worker_id", "TEXT"),
        ("lease_generation", "INTEGER"),
        ("browser_job_id", "TEXT"),
        ("from_state", "TEXT"),
        ("to_state", "TEXT"),
        ("outcome_code", "TEXT"),
        ("redaction_version", "INTEGER NOT NULL DEFAULT 1"),
    ]:
        if name not in event_columns:
            conn.execute(
                f"ALTER TABLE automation_events ADD COLUMN {name} {definition};"
            )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_attempt_key "
        "ON automation_events(attempt_id, event_key) WHERE attempt_id IS NOT NULL AND event_key IS NOT NULL;"
    )
    attempt_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(application_attempts);").fetchall()
    }
    for name, definition in [
        ("started_at", "TEXT"),
        ("worker_id", "TEXT"),
        ("lease_generation", "INTEGER"),
        ("browser_job_id", "TEXT"),
        ("last_event_id", "INTEGER"),
        ("redacted_error_code", "TEXT"),
    ]:
        if name not in attempt_columns:
            conn.execute(
                f"ALTER TABLE application_attempts ADD COLUMN {name} {definition};"
            )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS automation_job_status (
            job_id TEXT PRIMARY KEY,
            app_id TEXT NOT NULL,
            attempt_id TEXT,
            attempt_number INTEGER,
            state TEXT NOT NULL DEFAULT 'ready',
            step TEXT NOT NULL DEFAULT '',
            outcome TEXT,
            last_event_id INTEGER,
            worker_id TEXT,
            lease_generation INTEGER,
            browser_job_id TEXT,
            submit_intent_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES automation_jobs(id) ON DELETE CASCADE,
            FOREIGN KEY (app_id) REFERENCES applications(id) ON DELETE CASCADE
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_status_app ON automation_job_status(app_id, updated_at DESC);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_status_state ON automation_job_status(state, updated_at DESC);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_job_status_event ON automation_job_status(last_event_id);"
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO automation_job_status (
            job_id, app_id, state, step, outcome, worker_id, lease_generation,
            updated_at
        )
        SELECT j.id, j.app_id, j.state, COALESCE(j.checkpoint, ''),
               CASE WHEN j.state IN ('applied','failed_permanent','skipped','site_changed','ambiguous_submission')
                    THEN j.state ELSE NULL END,
               j.lease_owner, j.fencing_generation, j.updated_at
        FROM automation_jobs j;
        """
    )
    conn.execute(
        """
        UPDATE automation_job_status
        SET attempt_id = (
                SELECT aa.id FROM application_attempts aa
                WHERE aa.job_id = automation_job_status.job_id
                ORDER BY aa.attempt_number DESC, aa.created_at DESC LIMIT 1
            ),
            attempt_number = (
                SELECT aa.attempt_number FROM application_attempts aa
                WHERE aa.job_id = automation_job_status.job_id
                ORDER BY aa.attempt_number DESC, aa.created_at DESC LIMIT 1
            ),
            submit_intent_at = (
                SELECT aa.submit_intent_at FROM application_attempts aa
                WHERE aa.job_id = automation_job_status.job_id
                ORDER BY aa.attempt_number DESC, aa.created_at DESC LIMIT 1
            ),
            started_at = (
                SELECT COALESCE(aa.started_at, aa.created_at) FROM application_attempts aa
                WHERE aa.job_id = automation_job_status.job_id
                ORDER BY aa.attempt_number DESC, aa.created_at DESC LIMIT 1
            ),
            completed_at = (
                SELECT aa.completed_at FROM application_attempts aa
                WHERE aa.job_id = automation_job_status.job_id
                ORDER BY aa.attempt_number DESC, aa.created_at DESC LIMIT 1
            )
        WHERE EXISTS (SELECT 1 FROM application_attempts aa WHERE aa.job_id = automation_job_status.job_id);
        """
    )


def _migration_4_adapter_safety_controls(conn: sqlite3.Connection) -> None:
    """Migration 4: Adapter safety controls, canary approvals, and submission rate limits."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adapter_controls (
            adapter_name TEXT PRIMARY KEY,
            is_enabled INTEGER NOT NULL DEFAULT 0,
            confirmed_canary_count INTEGER NOT NULL DEFAULT 0,
            min_interval_seconds INTEGER NOT NULL DEFAULT 300,
            max_daily_submissions INTEGER NOT NULL DEFAULT 5,
            last_submission_at TEXT,
            updated_at TEXT NOT NULL
        );
        """
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for name in ["greenhouse", "lever", "ashby", "generic"]:
        conn.execute(
            """
            INSERT OR IGNORE INTO adapter_controls (
                adapter_name, is_enabled, confirmed_canary_count, min_interval_seconds,
                max_daily_submissions, last_submission_at, updated_at
            ) VALUES (?, 0, 0, 300, 5, NULL, ?);
            """,
            (name, now),
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adapter_canaries (
            id TEXT PRIMARY KEY,
            adapter_name TEXT NOT NULL,
            job_id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            confirmation_evidence TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (adapter_name) REFERENCES adapter_controls(adapter_name)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_canaries_adapter ON adapter_canaries(adapter_name, created_at);"
    )


MIGRATIONS = [
    (1, "baseline_applications_processed_jobs", _migration_1_baseline),
    (2, "durable_automation_queue_and_safety", _migration_2_automation),
    (3, "durable_notifications_and_acknowledgements", _migration_3_notifications),
    (4, "adapter_safety_controls_and_canaries", _migration_4_adapter_safety_controls),
    (5, "automation_observability", _migration_5_automation_observability),
]


def run_migrations(custom_path: Path | None = None) -> int:
    """
    Applies all pending migrations up to CURRENT_SCHEMA_VERSION.
    Fails closed if the database schema is newer than the supported codebase version.
    """
    with _DB_LOCK:
        path = get_db_path(custom_path)
        conn = get_connection(custom_path)
        try:
            # Check schema_migrations table
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
            )
            has_table = cur.fetchone() is not None

            if not has_table:
                conn.execute(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    );
                    """
                )
                # Check if legacy applications exists
                legacy = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='applications';"
                ).fetchone()
                if legacy:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (1, 'baseline_applications_processed_jobs', ?);",
                        (now,),
                    )
                conn.commit()

            # Read applied versions
            applied_rows = conn.execute(
                "SELECT version FROM schema_migrations;"
            ).fetchall()
            applied_versions = {r[0] for r in applied_rows}
            max_v = max(applied_versions) if applied_versions else 0

            if max_v > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {max_v} at {path} is newer than supported code version {CURRENT_SCHEMA_VERSION}. "
                    "Refusing startup to prevent database corruption."
                )

            applied_now = 0
            for version, name, migration_fn in MIGRATIONS:
                if version not in applied_versions:
                    with conn:
                        migration_fn(conn)
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        conn.execute(
                            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?);",
                            (version, name, now),
                        )
                    applied_now += 1

            return applied_now
        finally:
            conn.close()


def ensure_runtime_control_columns(conn: sqlite3.Connection) -> None:
    """Ensures runtime_control table has all required columns idempotently."""
    has_rc = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='runtime_control';"
        ).fetchone()
        is not None
    )
    if not has_rc:
        return
    existing_rc_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(runtime_control);").fetchall()
    }
    for col_name, col_def in [
        ("is_waiting_for_code", "INTEGER NOT NULL DEFAULT 0"),
        ("pending_verification_code", "TEXT"),
        ("browser_active", "INTEGER NOT NULL DEFAULT 0"),
        ("browser_url", "TEXT"),
        ("browser_page_text", "TEXT"),
        ("browser_is_closed", "INTEGER NOT NULL DEFAULT 1"),
        ("browser_updated_at", "TEXT"),
        ("browser_job_id", "TEXT"),
    ]:
        if col_name not in existing_rc_cols:
            conn.execute(
                f"ALTER TABLE runtime_control ADD COLUMN {col_name} {col_def};"
            )


def init_db(custom_path: Path | None = None) -> None:
    """Initializes the database schema and executes all pending migrations."""
    run_migrations(custom_path)
    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                ensure_runtime_control_columns(conn)
        finally:
            conn.close()


def upsert_application(
    app_id: str,
    company: str,
    title: str,
    job_url: str,
    platform: str = "Generic",
    status: str = "pending",
    submission_type: str = "manual",
    folder_name: str = "",
    cv_filename: str = "",
    has_cover_letter: bool = False,
    proof_screenshot: str = "",
    notes: str = "",
    has_artifacts: bool = True,
    custom_path: Path | None = None,
) -> None:
    """Inserts or updates an application record atomically."""
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    f_name = folder_name or app_id

    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO applications (
                        id, company, title, job_url, platform, status, submission_type,
                        folder_name, cv_filename, has_cover_letter, proof_screenshot, notes,
                        has_artifacts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        company = CASE WHEN excluded.company != '' THEN excluded.company ELSE applications.company END,
                        title = CASE WHEN excluded.title != '' THEN excluded.title ELSE applications.title END,
                        job_url = CASE WHEN excluded.job_url != '' THEN excluded.job_url ELSE applications.job_url END,
                        platform = CASE WHEN excluded.platform != 'Generic' THEN excluded.platform ELSE applications.platform END,
                        status = excluded.status,
                        submission_type = excluded.submission_type,
                        folder_name = excluded.folder_name,
                        cv_filename = CASE WHEN excluded.cv_filename != '' THEN excluded.cv_filename ELSE applications.cv_filename END,
                        has_cover_letter = excluded.has_cover_letter,
                        proof_screenshot = CASE WHEN excluded.proof_screenshot != '' THEN excluded.proof_screenshot ELSE applications.proof_screenshot END,
                        notes = CASE WHEN excluded.notes != '' THEN excluded.notes ELSE applications.notes END,
                        has_artifacts = excluded.has_artifacts,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        app_id,
                        company.strip(),
                        title.strip(),
                        job_url.strip(),
                        platform.strip(),
                        status.strip(),
                        submission_type.strip(),
                        f_name.strip(),
                        cv_filename.strip(),
                        1 if has_cover_letter else 0,
                        proof_screenshot.strip(),
                        notes.strip(),
                        1 if has_artifacts else 0,
                        now,
                        now,
                    ),
                )
                if job_url:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO processed_jobs (job_url, app_id, processed_at)
                        VALUES (?, ?, ?);
                        """,
                        (job_url.strip(), app_id, now),
                    )
        finally:
            conn.close()


def mark_job_processed(
    job_url: str,
    app_id: str = "",
    custom_path: Path | None = None,
) -> None:
    """Records a job URL as processed to prevent duplicate scraping and application."""
    if not job_url:
        return
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO processed_jobs (job_url, app_id, processed_at)
                    VALUES (?, ?, ?);
                    """,
                    (job_url.strip(), app_id, now),
                )
        finally:
            conn.close()


def get_applications(
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    custom_path: Path | None = None,
    queue_only: bool = False,
) -> dict[str, Any]:
    """Queries applications, optionally restricted to automation-backed queue jobs."""
    init_db(custom_path)
    conn = get_connection(custom_path)

    s_term = f"%{search.strip().lower()}%" if search else ""
    st_clean = status.strip() if status else ""

    with _DB_LOCK:
        try:
            where_clauses: list[str] = []
            params: list[Any] = []
            job_join = ""
            if queue_only:
                job_join = """
                    JOIN automation_jobs queue_job ON queue_job.app_id = a.id
                        AND queue_job.id = (
                            SELECT id FROM automation_jobs
                            WHERE app_id = a.id
                            ORDER BY created_at DESC, id DESC
                            LIMIT 1
                        )
                """
                where_clauses.append("LOWER(a.status) NOT IN ('dismissed', 'skipped')")
                where_clauses.append("LOWER(queue_job.state) != 'skipped'")

            if st_clean:
                where_clauses.append("LOWER(a.status) = LOWER(?)")
                params.append(st_clean)

            if s_term:
                where_clauses.append(
                    "(LOWER(a.company) LIKE ? OR LOWER(a.title) LIKE ?)"
                )
                params.extend([s_term, s_term])

            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

            count_sql = f"SELECT COUNT(*) FROM applications a {job_join} {where_sql};"
            total_row = conn.execute(count_sql, params).fetchone()
            total = total_row[0] if total_row else 0

            query_sql = f"""
                SELECT a.*, j.state as automation_state, j.id as job_id
                FROM applications a
                {job_join}
                LEFT JOIN automation_jobs j ON j.app_id = a.id
                    AND j.id = (SELECT id FROM automation_jobs WHERE app_id = a.id ORDER BY created_at DESC, id DESC LIMIT 1)
                {where_sql}
                ORDER BY a.updated_at DESC
                LIMIT ? OFFSET ?;
            """
            rows = conn.execute(query_sql, params + [limit, offset]).fetchall()

            items = [dict(r) for r in rows]
            return {"items": items, "total": total, "limit": limit, "offset": offset}
        finally:
            conn.close()


def update_status_by_url(
    job_url: str,
    status: str,
    notes: str = "",
    custom_path: Path | None = None,
) -> bool:
    """Updates status of an application matched by job_url."""
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE applications
                    SET status = ?, notes = CASE WHEN ? != '' THEN ? ELSE notes END, updated_at = ?
                    WHERE job_url = ?;
                    """,
                    (
                        status.strip(),
                        notes.strip(),
                        notes.strip(),
                        now,
                        job_url.strip(),
                    ),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()


def dismiss_application_db(app_id: str, custom_path: Path | None = None) -> bool:
    """Persists a queue dismissal without deleting application or automation history."""
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE applications
                    SET status = 'dismissed',
                        notes = CASE
                            WHEN notes = '' THEN 'Dismissed from Web Dashboard'
                            ELSE notes || ' | Dismissed from Web Dashboard'
                        END,
                        updated_at = ?
                    WHERE id = ?;
                    """,
                    (now, app_id),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()


def delete_application_db(app_id: str, custom_path: Path | None = None) -> bool:
    """Deletes an application by ID for explicit database cleanup operations."""
    init_db(custom_path)
    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM applications WHERE id = ?;", (app_id,)
                )
                return cursor.rowcount > 0
        finally:
            conn.close()


def get_db_stats(custom_path: Path | None = None) -> dict[str, Any]:
    """Returns aggregated summary metrics from the SQLite database and automation queue."""
    init_db(custom_path)
    conn = get_connection(custom_path)

    with _DB_LOCK:
        try:
            total = conn.execute("SELECT COUNT(*) FROM applications;").fetchone()[0]
            status_rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM applications GROUP BY status;"
            ).fetchall()
            platform_rows = conn.execute(
                "SELECT platform, COUNT(*) as cnt FROM applications GROUP BY platform;"
            ).fetchall()

            status_map = {r["status"]: r["cnt"] for r in status_rows}
            platform_map = {r["platform"]: r["cnt"] for r in platform_rows}

            # Automation queue breakdown
            queue_rows = conn.execute(
                "SELECT state, COUNT(*) as cnt FROM automation_jobs GROUP BY state;"
            ).fetchall()
            queue_map = {r["state"]: r["cnt"] for r in queue_rows}

            active_jobs_row = conn.execute(
                "SELECT COUNT(*) FROM automation_jobs WHERE LOWER(state) != 'skipped';"
            ).fetchone()
            active_jobs = active_jobs_row[0] if active_jobs_row else 0

            return {
                "total_records": total,
                "applied": status_map.get("applied", 0),
                "auto_filled": status_map.get("auto_filled", 0),
                "pending": status_map.get("pending", 0)
                + status_map.get("auto_filled", 0),
                "failed": status_map.get("failed", 0),
                "validation_failed": status_map.get("validation_failed", 0),
                "skipped": status_map.get("skipped", 0),
                "platforms": platform_map,
                "queue": {
                    "active_jobs": active_jobs,
                    "ready": queue_map.get("ready", 0),
                    "claimed": queue_map.get("claimed", 0),
                    "in_progress": sum(
                        queue_map.get(s, 0)
                        for s in [
                            "claimed",
                            "navigating",
                            "filling",
                            "validating",
                            "submit_intent",
                            "verifying",
                        ]
                    ),
                    "retry_wait": queue_map.get("retry_wait", 0),
                    "exceptions": sum(
                        queue_map.get(s, 0)
                        for s in [
                            "auth_required",
                            "mfa_required",
                            "captcha_required",
                            "unknown_question",
                            "site_changed",
                            "ambiguous_submission",
                        ]
                    ),
                    "total_jobs": sum(queue_map.values()),
                },
            }
        finally:
            conn.close()


def normalize_job_url(url: str) -> str:
    """
    Conservatively normalizes job URLs by removing marketing/tracking parameters
    (utm_*, gclid, fbclid, etc.) while preserving platform-specific job IDs.
    """
    if not url or not url.strip():
        return ""
    clean = url.strip()
    try:
        parsed = urlparse(clean)
        # Filter query params
        qs = parse_qs(parsed.query, keep_blank_values=True)
        filtered_qs = {
            k: v
            for k, v in qs.items()
            if not k.lower().startswith("utm_")
            and k.lower() not in {"gclid", "fbclid", "refid", "trackingid", "trk"}
        }
        clean_query = urlencode(filtered_qs, doseq=True)
        # Strip trailing slash from path if length > 1
        path = parsed.path.rstrip("/") if len(parsed.path) > 1 else parsed.path
        normalized = urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                path,
                parsed.params,
                clean_query,
                "",  # Strip fragment
            )
        )
        return normalized
    except Exception:
        return clean.rstrip("/")


def reconcile_storage(custom_path: Path | None = None) -> dict[str, Any]:
    """
    Reconciles applications from CSV tracker, output folders, and SQLite DB.
    Guarantees:
    1. Preserves completed 'applied' statuses (never overwrites 'applied' with 'pending').
    2. Identifies missing artifacts without deleting any files.
    3. Normalizes job URLs without stripping critical job identifiers.
    4. Safe, idempotent, and non-destructive to existing personal data.
    """
    init_db(custom_path)
    project_root = get_project_root()
    tracker_file = project_root / "data" / "applications_tracker.csv"

    scanned_count = 0
    imported_count = 0
    applied_preserved = 0
    missing_artifacts_count = 0

    # 1. First pass: Collect all confirmed applied URLs/companies from tracker CSV
    applied_urls: set[str] = set()
    csv_records: dict[str, dict[str, Any]] = {}
    disk_folders: dict[str, str] = {}

    if tracker_file.exists():
        try:
            df = pd.read_csv(tracker_file, dtype=str).fillna("")
            for _, row in df.iterrows():
                url = normalize_job_url(str(row.get("job_url", "")).strip())
                company = str(row.get("company", "")).strip()
                title = str(row.get("title", "")).strip()
                status = str(row.get("status", "pending")).strip()
                platform = str(row.get("platform", "Generic")).strip()
                sub_type = str(row.get("submission_type", "manual")).strip()
                notes = str(row.get("notes", "")).strip()
                cv_path = str(row.get("cv_path", "")).strip()
                proof = str(row.get("proof_path", "")).strip()

                if not url and not company:
                    continue

                if status.lower() == "applied":
                    if url:
                        applied_urls.add(url)

                app_id = (
                    f"{company}_{title}".replace(" ", "_")
                    if company
                    else Path(cv_path).parent.name
                )
                if not app_id or app_id == ".":
                    app_id = f"job_{abs(hash(url))}"

                csv_records[app_id] = {
                    "app_id": app_id,
                    "company": company,
                    "title": title,
                    "job_url": url,
                    "platform": platform,
                    "status": status,
                    "submission_type": sub_type,
                    "cv_filename": Path(cv_path).name if cv_path else "",
                    "proof_screenshot": proof,
                    "notes": notes,
                }
        except Exception as e:
            print(f"Notice during CSV reconciliation: {e}")

    # 2. Reconcile folders in output/applied/ (always 'applied')
    applied_dir = project_root / "output" / "applied"
    if applied_dir.exists():
        for d in applied_dir.iterdir():
            if not d.is_dir():
                continue
            scanned_count += 1
            apply_file = d / "APPLY_HERE.txt"
            url = ""
            if apply_file.exists():
                try:
                    url = normalize_job_url(
                        apply_file.read_text(encoding="utf-8").strip()
                    )
                except Exception:
                    pass
            if url:
                applied_urls.add(url)

            pdf = next(d.glob("CV_*.pdf"), None)
            cl = d / "cover_letter.txt"
            proof_file = next(d.glob("submission_proof.*"), None)

            name_parts = d.name.split("_", 1)
            comp = name_parts[0]
            role = name_parts[1] if len(name_parts) > 1 else "Position"

            base_name = re.sub(r"_\d+$", "", d.name)
            disk_folders[d.name] = d.name
            if base_name != d.name:
                disk_folders[base_name] = d.name
            if url:
                disk_folders[url] = d.name

            upsert_application(
                app_id=d.name,
                company=comp,
                title=role,
                job_url=url,
                status="applied",
                folder_name=d.name,
                cv_filename=pdf.name if pdf else "",
                has_cover_letter=cl.exists(),
                proof_screenshot=str(proof_file) if proof_file else "",
                has_artifacts=pdf is not None,
                custom_path=custom_path,
            )
            if base_name != d.name:
                upsert_application(
                    app_id=base_name,
                    company=comp,
                    title=role,
                    job_url=url,
                    status="applied",
                    folder_name=d.name,
                    cv_filename=pdf.name if pdf else "",
                    has_cover_letter=cl.exists(),
                    proof_screenshot=str(proof_file) if proof_file else "",
                    has_artifacts=pdf is not None,
                    custom_path=custom_path,
                )
            imported_count += 1
            applied_preserved += 1

    # 3. Reconcile folders in output/applications/ (pending, unless in applied_urls)
    pending_dir = project_root / "output" / "applications"
    if pending_dir.exists():
        for d in pending_dir.iterdir():
            if not d.is_dir():
                continue
            scanned_count += 1
            apply_file = d / "APPLY_HERE.txt"
            url = ""
            if apply_file.exists():
                try:
                    url = normalize_job_url(
                        apply_file.read_text(encoding="utf-8").strip()
                    )
                except Exception:
                    pass

            pdf = next(d.glob("CV_*.pdf"), None)
            cl = d / "cover_letter.txt"

            name_parts = d.name.split("_", 1)
            comp = name_parts[0]
            role = name_parts[1] if len(name_parts) > 1 else "Position"

            status = "applied" if (url and url in applied_urls) else "pending"
            if status == "applied":
                applied_preserved += 1

            has_art = pdf is not None
            if not has_art:
                missing_artifacts_count += 1

            base_name = re.sub(r"_\d+$", "", d.name)
            disk_folders[d.name] = d.name
            if base_name != d.name:
                disk_folders[base_name] = d.name
            if url:
                disk_folders[url] = d.name

            upsert_application(
                app_id=d.name,
                company=comp,
                title=role,
                job_url=url,
                status=status,
                folder_name=d.name,
                cv_filename=pdf.name if pdf else "",
                has_cover_letter=cl.exists(),
                has_artifacts=has_art,
                custom_path=custom_path,
            )
            if base_name != d.name:
                upsert_application(
                    app_id=base_name,
                    company=comp,
                    title=role,
                    job_url=url,
                    status=status,
                    folder_name=d.name,
                    cv_filename=pdf.name if pdf else "",
                    has_cover_letter=cl.exists(),
                    has_artifacts=has_art,
                    custom_path=custom_path,
                )
            imported_count += 1

    # 4. Merge any remaining records from CSV tracker not yet covered by folders
    for app_id, rec in csv_records.items():
        scanned_count += 1
        # If record is in applied_urls, ensure status is applied
        if rec["job_url"] and rec["job_url"] in applied_urls:
            rec["status"] = "applied"
            applied_preserved += 1

        target_folder = (
            disk_folders.get(rec["app_id"])
            or disk_folders.get(rec["job_url"])
            or rec.get("folder_name")
            or rec["app_id"]
        )

        upsert_application(
            app_id=rec["app_id"],
            company=rec["company"],
            title=rec["title"],
            job_url=rec["job_url"],
            platform=rec["platform"],
            status=rec["status"],
            submission_type=rec["submission_type"],
            folder_name=target_folder,
            cv_filename=rec["cv_filename"],
            proof_screenshot=rec["proof_screenshot"],
            notes=rec["notes"],
            custom_path=custom_path,
        )
        imported_count += 1

    return {
        "scanned": scanned_count,
        "imported": imported_count,
        "applied_preserved": applied_preserved,
        "missing_artifacts": missing_artifacts_count,
    }


def migrate_csv_and_disk_to_db(custom_path: Path | None = None) -> int:
    """Wrapper for backward compatibility."""
    result = reconcile_storage(custom_path)
    return result["imported"]


def get_adapter_control(
    adapter_name: str, custom_path: Path | None = None
) -> dict[str, Any] | None:
    """Retrieves adapter control row by adapter name."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            "SELECT * FROM adapter_controls WHERE adapter_name = ?;",
            (adapter_name.lower(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_all_adapter_controls(
    custom_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Retrieves all adapter control rows."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        rows = conn.execute(
            "SELECT * FROM adapter_controls ORDER BY adapter_name ASC;"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_canary_approval(
    adapter_name: str,
    job_id: str,
    app_id: str,
    approved_by: str,
    confirmation_evidence: str,
    custom_path: Path | None = None,
) -> dict[str, Any]:
    """
    Records an operator-approved canary for an adapter and increments confirmed canary count.
    Generic adapter can never have canaries recorded or be enabled.
    """
    if adapter_name.lower() == "generic":
        raise ValueError(
            "Generic adapter cannot have canaries approved; it is fill-only."
        )

    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    canary_id = f"canary_{adapter_name}_{uuid.uuid4().hex[:10]}"

    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO adapter_canaries (
                        id, adapter_name, job_id, app_id, approved_by, confirmation_evidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        canary_id,
                        adapter_name.lower(),
                        job_id,
                        app_id,
                        approved_by,
                        confirmation_evidence,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE adapter_controls
                    SET confirmed_canary_count = confirmed_canary_count + 1,
                        updated_at = ?
                    WHERE adapter_name = ?;
                    """,
                    (now, adapter_name.lower()),
                )
                row = conn.execute(
                    "SELECT * FROM adapter_controls WHERE adapter_name = ?;",
                    (adapter_name.lower(),),
                ).fetchone()
                return dict(row) if row else {}
        finally:
            conn.close()


def set_adapter_enabled(
    adapter_name: str,
    enabled: bool,
    custom_path: Path | None = None,
) -> bool:
    """
    Enables or disables an adapter.
    Enabling an adapter requires at least 3 confirmed canaries!
    Generic adapter can NEVER be enabled for automated submission.
    """
    name = adapter_name.lower()
    if name == "generic" and enabled:
        raise ValueError(
            "Generic adapter is fill-only and cannot be enabled for submission."
        )

    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                ctrl = conn.execute(
                    "SELECT * FROM adapter_controls WHERE adapter_name = ?;",
                    (name,),
                ).fetchone()
                if not ctrl:
                    raise ValueError(f"Adapter '{name}' not found in adapter_controls.")

                if enabled and ctrl["confirmed_canary_count"] < 3:
                    raise ValueError(
                        f"Adapter '{name}' cannot be enabled: requires 3 approved confirmed canaries "
                        f"(currently {ctrl['confirmed_canary_count']})."
                    )

                conn.execute(
                    """
                    UPDATE adapter_controls
                    SET is_enabled = ?, updated_at = ?
                    WHERE adapter_name = ?;
                    """,
                    (1 if enabled else 0, now, name),
                )
                return True
        finally:
            conn.close()


def record_submission_timestamp(
    adapter_name: str,
    timestamp: str | None = None,
    custom_path: Path | None = None,
) -> None:
    """Records the timestamp of a successful submission for rate limiting and pacing."""
    init_db(custom_path)
    ts = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE adapter_controls
                    SET last_submission_at = ?, updated_at = ?
                    WHERE adapter_name = ?;
                    """,
                    (ts, ts, adapter_name.lower()),
                )
        finally:
            conn.close()


def get_latest_submission_timestamp(custom_path: Path | None = None) -> str | None:
    """Returns the latest submission timestamp across all adapters."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            "SELECT MAX(last_submission_at) FROM adapter_controls;"
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()


def get_submissions_in_window(
    window_hours: int = 24,
    custom_path: Path | None = None,
) -> int:
    """Counts completed submissions within the sliding window from application_attempts."""
    init_db(custom_path)
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(hours=window_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM application_attempts
            WHERE outcome = 'applied' AND completed_at >= ?;
            """,
            (cutoff,),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# Initialize database automatically on import
init_db()
