from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd

from job_applier.utils import get_project_root

TRACKER_COLUMNS = [
    "timestamp",
    "company",
    "title",
    "job_url",
    "platform",
    "status",
    "submission_type",
    "cv_path",
    "proof_path",
    "notes",
]


def get_tracker_file(custom_path: Path | None = None) -> Path:
    """Returns the path to the tracker CSV file."""
    if custom_path:
        return custom_path
    env_path = os.environ.get("JOB_APPLIER_TRACKER_PATH")
    if env_path:
        p = Path(env_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    project_root = get_project_root()
    tracker_file = project_root / "data" / "applications_tracker.csv"
    tracker_file.parent.mkdir(parents=True, exist_ok=True)
    return tracker_file


def _resolve_db_path(custom_path: Path | None = None) -> Path | None:
    """Helper to resolve the SQLite database path from an optional custom path."""
    if not custom_path:
        return None
    if custom_path.suffix == ".csv":
        return custom_path.with_suffix(".db")
    return custom_path


def load_tracker(custom_path: Path | None = None) -> pd.DataFrame:
    """Loads applications from SQLite applications table into a tracker DataFrame."""
    from job_applier.db import get_connection, init_db

    db_path = _resolve_db_path(custom_path)
    try:
        init_db(db_path)
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                """
                SELECT
                    updated_at as timestamp,
                    company,
                    title,
                    job_url,
                    platform,
                    status,
                    submission_type,
                    cv_filename as cv_path,
                    proof_screenshot as proof_path,
                    notes,
                    folder_name,
                    id
                FROM applications
                WHERE LOWER(status) != 'dismissed'
                ORDER BY updated_at DESC;
                """
            ).fetchall()
            if rows:
                data = [dict(r) for r in rows]
                df = pd.DataFrame(data)
                for col in TRACKER_COLUMNS:
                    if col not in df.columns:
                        df[col] = ""
                return df.fillna("")
        finally:
            conn.close()
    except Exception as e:
        print(f"Notice loading tracker from SQLite: {e}")

    # Fallback to legacy CSV if custom_path explicitly points to an existing CSV file
    if custom_path and custom_path.exists() and custom_path.suffix == ".csv":
        try:
            df = pd.read_csv(custom_path, dtype=str)
            for col in TRACKER_COLUMNS:
                if col not in df.columns:
                    df[col] = ""
            return df.fillna("")
        except Exception:
            pass

    return pd.DataFrame({col: pd.Series(dtype="object") for col in TRACKER_COLUMNS})


def detect_platform_from_url(url: str) -> str:
    """Helper to detect job board or ATS from the job URL."""
    url_lower = url.lower()
    if "linkedin.com" in url_lower:
        return "LinkedIn"
    if "indeed.com" in url_lower:
        return "Indeed"
    if "ejobs.ro" in url_lower:
        return "eJobs"
    if "bestjobs.eu" in url_lower:
        return "BestJobs"
    if "hipo.ro" in url_lower:
        return "Hipo"
    if "undelucram.ro" in url_lower:
        return "Undelucram"
    if "jooble.org" in url_lower:
        return "Jooble"
    if "jobicy.com" in url_lower:
        return "Jobicy"
    if "greenhouse.io" in url_lower or "grnh.se" in url_lower:
        return "Greenhouse"
    if "lever.co" in url_lower:
        return "Lever"
    if "myworkdayjobs.com" in url_lower or "workday" in url_lower:
        return "Workday"
    if "ashbyhq.com" in url_lower:
        return "Ashby"
    if "smartrecruiters.com" in url_lower:
        return "SmartRecruiters"
    if "capgemini.com" in url_lower:
        return "Capgemini"
    if "jobs.sap.com" in url_lower or "sap.com" in url_lower:
        return "SAP"
    if "taleo.net" in url_lower or "oraclecloud.com" in url_lower:
        return "Oracle"
    return "Generic"


def record_application(
    company: str,
    title: str,
    job_url: str,
    status: str = "applied",
    platform: str = "Generic",
    submission_type: str = "manual",
    cv_path: str = "",
    proof_path: str = "",
    notes: str = "",
    custom_path: Path | None = None,
) -> pd.DataFrame:
    """Records or updates an application entry in SQLite applications table."""
    from job_applier.db import get_connection, init_db, upsert_application

    db_path = _resolve_db_path(custom_path)

    # Auto-detect platform from URL if it's Generic
    effective_platform = platform.strip()
    if effective_platform == "Generic" and job_url.strip():
        effective_platform = detect_platform_from_url(job_url)

    app_id = (
        f"{company}_{title}".replace(" ", "_")
        if company
        else (Path(cv_path).parent.name if cv_path else f"job_{abs(hash(job_url))}")
    )
    if not app_id or app_id == ".":
        app_id = f"job_{abs(hash(job_url))}"

    # If already in SQLite by job_url, reuse existing app_id
    if job_url.strip():
        try:
            init_db(db_path)
            conn = get_connection(db_path)
            try:
                row = conn.execute(
                    "SELECT id FROM applications WHERE job_url = ? LIMIT 1;",
                    (job_url.strip(),),
                ).fetchone()
                if row:
                    app_id = row[0]
            finally:
                conn.close()
        except Exception:
            pass

    cv_filename = Path(cv_path).name if cv_path else ""
    folder_name = (
        Path(cv_path).parent.name
        if cv_path and Path(cv_path).parent.name != "."
        else app_id
    )

    try:
        upsert_application(
            app_id=app_id,
            company=company.strip(),
            title=title.strip(),
            job_url=job_url.strip(),
            platform=effective_platform,
            status=status.strip(),
            submission_type=submission_type.strip(),
            cv_filename=cv_filename,
            proof_screenshot=str(proof_path).strip(),
            notes=notes.strip(),
            folder_name=folder_name,
            custom_path=db_path,
        )
    except Exception as e:
        print(f"Error saving application to SQLite: {e}")

    return load_tracker(custom_path)


def update_status(
    job_url: str,
    status: str,
    notes: str = "",
    custom_path: Path | None = None,
) -> bool:
    """Updates status for an application in SQLite applications table."""
    from job_applier.db import update_status_by_url

    db_path = _resolve_db_path(custom_path)
    return update_status_by_url(
        job_url=job_url,
        status=status,
        notes=notes,
        custom_path=db_path,
    )


def remove_from_tracker(job_url: str, custom_path: Path | None = None) -> pd.DataFrame:
    """Resets application status in SQLite applications table."""
    from job_applier.db import get_connection, init_db

    db_path = _resolve_db_path(custom_path)
    try:
        init_db(db_path)
        conn = get_connection(db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE applications SET status = 'pending', updated_at = datetime('now') WHERE job_url = ?;",
                    (job_url.strip(),),
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"Notice updating status on remove_from_tracker: {e}")

    return load_tracker(custom_path)


def cleanup_tracker(custom_path: Path | None = None) -> pd.DataFrame:
    """Removes invalid or empty applications from SQLite applications table."""
    from job_applier.db import get_connection, init_db

    db_path = _resolve_db_path(custom_path)
    try:
        init_db(db_path)
        conn = get_connection(db_path)
        try:
            with conn:
                conn.execute(
                    "DELETE FROM applications WHERE job_url = '' OR job_url IS NULL;"
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"Notice cleaning tracker in SQLite: {e}")

    return load_tracker(custom_path)


def export_tracker_csv(
    custom_path: Path | None = None,
    output_path: Path | None = None,
) -> str:
    """Dynamically generates a CSV export from SQLite applications rows."""
    df = load_tracker(custom_path)
    csv_string = df.to_csv(index=False)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(csv_string, encoding="utf-8")
    return csv_string


def get_tracker_stats(custom_path: Path | None = None) -> dict[str, Any]:
    """Returns aggregated summary metrics for applied jobs."""
    df = load_tracker(custom_path)
    if df.empty:
        return {
            "total_records": 0,
            "applied": 0,
            "auto_filled": 0,
            "skipped": 0,
            "failed": 0,
            "platforms": {},
        }

    status_counts = df["status"].value_counts().to_dict()
    platform_counts = df["platform"].value_counts().to_dict()

    def _safe_int(val: Any) -> int:
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0

    return {
        "total_records": len(df),
        "applied": _safe_int(status_counts.get("applied", 0)),
        "auto_filled": _safe_int(status_counts.get("auto_filled", 0)),
        "skipped": _safe_int(status_counts.get("skipped", 0)),
        "failed": _safe_int(status_counts.get("failed", 0)),
        "platforms": platform_counts,
    }


def print_tracker_summary(custom_path: Path | None = None) -> None:
    """Prints a friendly summary of tracked applications."""
    stats = get_tracker_stats(custom_path)
    print("\n Application Pipeline Tracker Summary")
    print("=========================================")
    print(f" Total Logged Applications: {stats['total_records']}")
    print(f"   Successfully Applied:    {stats['applied']}")
    print(f"   Auto-Filled / Pending:   {stats['auto_filled']}")
    print(f"   Skipped / Deferred:      {stats['skipped']}")
    print(f"   Failed / Action Needed:  {stats['failed']}")

    if stats["platforms"]:
        print("\n Applications by Platform:")
        for plat, count in stats["platforms"].items():
            print(f"  - {plat}: {count}")
    print("=========================================\n")
