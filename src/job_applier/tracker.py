from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, cast

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


def load_tracker(custom_path: Path | None = None) -> pd.DataFrame:
    """Loads the application tracker dataframe or initializes an empty one."""
    file_path = get_tracker_file(custom_path)
    if file_path.exists():
        try:
            df = pd.read_csv(file_path, dtype=str)
            # Ensure all standard columns exist
            for col in TRACKER_COLUMNS:
                if col not in df.columns:
                    df[col] = ""
            return df.fillna("")
        except Exception as e:
            print(f"Warning: Could not read tracker file: {e}")

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
    """Records or updates an application entry in the tracker."""
    tracker_file = get_tracker_file(custom_path)
    df = load_tracker(custom_path)

    # Auto-detect platform from URL if it's Generic
    effective_platform = platform.strip()
    if effective_platform == "Generic" and job_url.strip():
        effective_platform = detect_platform_from_url(job_url)

    new_row = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "company": company.strip(),
        "title": title.strip(),
        "job_url": job_url.strip(),
        "platform": effective_platform,
        "status": status.strip(),
        "submission_type": submission_type.strip(),
        "cv_path": str(cv_path).strip(),
        "proof_path": str(proof_path).strip(),
        "notes": notes.strip(),
    }

    # Do not create an entirely empty row
    if not job_url.strip() and not company.strip() and not title.strip():
        return df

    # If already in tracker by job_url, update existing row
    updated = False
    if job_url and "job_url" in df.columns and len(df) > 0:
        mask = df["job_url"] == job_url
        if mask.any():
            for k, v in new_row.items():
                # Only overwrite metadata if new value is non-empty
                if v != "" or k in ["status", "notes", "timestamp", "submission_type"]:
                    if v != "" or k == "notes":
                        df.loc[mask, k] = v
            updated = True

    if not updated:
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)

    try:
        df.to_csv(tracker_file, index=False)
    except Exception as e:
        print(f"Error saving to tracker file: {e}")

    try:
        from job_applier.db import upsert_application

        app_id = (
            f"{company}_{title}".replace(" ", "_")
            if company
            else (Path(cv_path).parent.name if cv_path else f"job_{abs(hash(job_url))}")
        )
        if not app_id or app_id == ".":
            app_id = f"job_{abs(hash(job_url))}"

        upsert_application(
            app_id=app_id,
            company=company,
            title=title,
            job_url=job_url,
            platform=effective_platform,
            status=status,
            submission_type=submission_type,
            cv_filename=Path(cv_path).name if cv_path else "",
            proof_screenshot=proof_path,
            notes=notes,
        )
    except Exception:
        pass

    return df


def remove_from_tracker(job_url: str, custom_path: Path | None = None) -> pd.DataFrame:
    """Removes a job entry from the tracker CSV (e.g. when requeued back to pending)."""
    tracker_file = get_tracker_file(custom_path)
    df = load_tracker(custom_path)
    if not df.empty and job_url and "job_url" in df.columns:
        filtered = df[df["job_url"].str.strip() != job_url.strip()]
        df = cast(pd.DataFrame, filtered)
        try:
            df.to_csv(tracker_file, index=False)
        except Exception as e:
            print(f"Error saving to tracker file: {e}")
    return df


def cleanup_tracker(custom_path: Path | None = None) -> pd.DataFrame:
    """Removes invalid, empty, or requeued rows from the tracker CSV."""
    tracker_file = get_tracker_file(custom_path)
    df = load_tracker(custom_path)
    if not df.empty and "job_url" in df.columns:
        # Filter out empty rows and rows that were marked auto_filled/pending
        valid = (df["job_url"].str.strip() != "") & (df["status"] != "auto_filled")
        filtered = df[valid]
        df = cast(pd.DataFrame, filtered)
        try:
            df.to_csv(tracker_file, index=False)
        except Exception as e:
            print(f"Error saving to tracker file: {e}")
    return df


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
