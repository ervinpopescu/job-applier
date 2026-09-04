from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from job_applier.utils import get_project_root

_DB_LOCK = threading.Lock()


def get_db_path(custom_path: Path | None = None) -> Path:
    """Returns the path to the SQLite database file."""
    if custom_path:
        return custom_path
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
    return conn


def init_db(custom_path: Path | None = None) -> None:
    """Initializes the database schema and indexes."""
    with _DB_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
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
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_apps_url ON applications(job_url);
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS processed_jobs (
                        job_url TEXT PRIMARY KEY,
                        app_id TEXT,
                        processed_at TEXT NOT NULL
                    );
                    """
                )
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
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def get_applications(
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    custom_path: Path | None = None,
) -> dict[str, Any]:
    """Queries applications with pagination, search, and status filtering using static parameterized queries."""
    init_db(custom_path)
    conn = get_connection(custom_path)

    s_term = f"%{search.strip().lower()}%" if search else ""
    st_clean = status.strip() if status else ""

    with _DB_LOCK:
        try:
            if st_clean and s_term:
                total_row = conn.execute(
                    "SELECT COUNT(*) FROM applications WHERE LOWER(status) = LOWER(?) AND (LOWER(company) LIKE ? OR LOWER(title) LIKE ?);",
                    (st_clean, s_term, s_term),
                ).fetchone()
                rows = conn.execute(
                    "SELECT * FROM applications WHERE LOWER(status) = LOWER(?) AND (LOWER(company) LIKE ? OR LOWER(title) LIKE ?) ORDER BY updated_at DESC LIMIT ? OFFSET ?;",
                    (st_clean, s_term, s_term, limit, offset),
                ).fetchall()
            elif st_clean:
                total_row = conn.execute(
                    "SELECT COUNT(*) FROM applications WHERE LOWER(status) = LOWER(?);",
                    (st_clean,),
                ).fetchone()
                rows = conn.execute(
                    "SELECT * FROM applications WHERE LOWER(status) = LOWER(?) ORDER BY updated_at DESC LIMIT ? OFFSET ?;",
                    (st_clean, limit, offset),
                ).fetchall()
            elif s_term:
                total_row = conn.execute(
                    "SELECT COUNT(*) FROM applications WHERE (LOWER(company) LIKE ? OR LOWER(title) LIKE ?);",
                    (s_term, s_term),
                ).fetchone()
                rows = conn.execute(
                    "SELECT * FROM applications WHERE (LOWER(company) LIKE ? OR LOWER(title) LIKE ?) ORDER BY updated_at DESC LIMIT ? OFFSET ?;",
                    (s_term, s_term, limit, offset),
                ).fetchall()
            else:
                total_row = conn.execute(
                    "SELECT COUNT(*) FROM applications;",
                    (),
                ).fetchone()
                rows = conn.execute(
                    "SELECT * FROM applications ORDER BY updated_at DESC LIMIT ? OFFSET ?;",
                    (limit, offset),
                ).fetchall()

            total = total_row[0] if total_row else 0
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


def delete_application_db(app_id: str, custom_path: Path | None = None) -> bool:
    """Deletes an application by ID."""
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
    """Returns aggregated summary metrics from the SQLite database."""
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
            }
        finally:
            conn.close()


def migrate_csv_and_disk_to_db(custom_path: Path | None = None) -> int:
    """
    Migrates existing records from applications_tracker.csv and folders in output/ into SQLite.
    Safe and idempotent — runs on startup without overwriting newer data.
    """
    init_db(custom_path)
    project_root = get_project_root()
    tracker_file = project_root / "data" / "applications_tracker.csv"
    imported_count = 0

    # 1. Migrate tracker CSV if present
    if tracker_file.exists():
        try:
            df = pd.read_csv(tracker_file, dtype=str).fillna("")
            for _, row in df.iterrows():
                url = str(row.get("job_url", "")).strip()
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

                # Derive clean app_id
                app_id = (
                    f"{company}_{title}".replace(" ", "_")
                    if company
                    else Path(cv_path).parent.name
                )
                if not app_id or app_id == ".":
                    app_id = f"job_{abs(hash(url))}"

                cv_file = Path(cv_path).name if cv_path else ""

                upsert_application(
                    app_id=app_id,
                    company=company,
                    title=title,
                    job_url=url,
                    platform=platform,
                    status=status,
                    submission_type=sub_type,
                    folder_name=app_id,
                    cv_filename=cv_file,
                    proof_screenshot=proof,
                    notes=notes,
                    custom_path=custom_path,
                )
                imported_count += 1
        except Exception as e:
            print(f"Notice during CSV migration: {e}")

    # 2. Migrate existing folders in output/applications/ and output/applied/
    for sub, default_status in [("applications", "pending"), ("applied", "applied")]:
        folder_dir = project_root / "output" / sub
        if folder_dir.exists():
            for d in folder_dir.iterdir():
                if not d.is_dir():
                    continue

                apply_file = d / "APPLY_HERE.txt"
                url = ""
                if apply_file.exists():
                    try:
                        url = apply_file.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass

                pdf = next(d.glob("CV_*.pdf"), None)
                cl = d / "cover_letter.txt"

                # Parse company and title from folder name
                name_parts = d.name.split("_", 1)
                comp = name_parts[0]
                role = name_parts[1] if len(name_parts) > 1 else "Position"

                upsert_application(
                    app_id=d.name,
                    company=comp,
                    title=role,
                    job_url=url,
                    status=default_status,
                    folder_name=d.name,
                    cv_filename=pdf.name if pdf else "",
                    has_cover_letter=cl.exists(),
                    custom_path=custom_path,
                )
                imported_count += 1

    return imported_count


# Initialize database automatically on import
init_db()
