from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from job_applier.config import (  # type: ignore[import-not-found]
    get_platforms_config,
)
from job_applier.logger import log_event  # type: ignore[import-not-found]
from job_applier.tracker import (  # type: ignore[import-not-found]
    record_application,
)
from job_applier.utils import get_project_root, sanitize_name

TRACKING_QUERY_PARAMS: set[str] = set(
    get_platforms_config().get("tracking_query_params", [])
)


def normalize_job_url(url: str) -> str:
    """
    Normalizes a job URL to its canonical form by stripping analytics/tracking parameters
    (UTM tags, refId, lever-source, feedId) and trailing slashes.
    """
    clean_url = (url or "").strip()
    if not clean_url or not clean_url.startswith("http"):
        return ""

    try:
        parsed = urlparse(clean_url)
        query_dict = parse_qs(parsed.query)

        # Filter out tracking query parameters
        clean_params = {
            k: v
            for k, v in query_dict.items()
            if k.lower() not in TRACKING_QUERY_PARAMS
        }
        clean_query = urlencode(clean_params, doseq=True)

        # Strip trailing slash from path
        clean_path = parsed.path.rstrip("/")

        return urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                clean_path,
                "",
                clean_query,
                "",
            )
        )
    except Exception:
        return clean_url


def get_role_signature(company: str, title: str) -> str:
    """Computes a normalized role identifier: e.g. 'gitlab_staff_backend_engineer'."""
    clean_company = sanitize_name(company).lower().strip("_")
    clean_title = sanitize_name(title).lower().strip("_")
    return f"{clean_company}__{clean_title}"


def get_existing_applications_index(
    project_root: Path | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Scans both output/applications (pending) and output/applied (submitted).
    Returns (url_to_folder, role_to_folder) index maps.
    """
    root = project_root or get_project_root()
    apps_base = root / "output" / "applications"
    applied_base = root / "output" / "applied"

    url_to_folder: dict[str, str] = {}
    role_to_folder: dict[str, str] = {}

    for base_dir in [applied_base, apps_base]:
        if not base_dir.exists():
            continue

        for folder in sorted(base_dir.iterdir(), key=lambda d: d.name):
            if not folder.is_dir():
                continue

            # 1. Read URL from APPLY_HERE.txt
            apply_file = folder / "APPLY_HERE.txt"
            if apply_file.exists():
                try:
                    with open(apply_file, encoding="utf-8") as f:
                        raw_url = f.read().strip()
                        norm_url = normalize_job_url(raw_url)
                        if norm_url and norm_url not in url_to_folder:
                            url_to_folder[norm_url] = folder.name
                except OSError:
                    pass

            # 2. Extract Company + Title signature
            parts = folder.name.rsplit("_", 1)
            folder_prefix = parts[0] if len(parts) > 1 else folder.name
            name_parts = folder_prefix.split("_", 1)
            comp = name_parts[0]
            title = name_parts[1] if len(name_parts) > 1 else "role"
            sig = get_role_signature(comp, title)
            if sig and sig not in role_to_folder:
                role_to_folder[sig] = folder.name

    return url_to_folder, role_to_folder


def is_duplicate_application(
    job_url: str,
    company: str,
    title: str,
    existing_urls: dict[str, str],
    existing_roles: dict[str, str],
) -> tuple[bool, str]:
    """
    Checks if a job posting has already been processed or prepared.
    Validates by canonical URL and (company, title) signature.
    """
    # 1. Check normalized canonical URL
    norm_url = normalize_job_url(job_url)
    if norm_url and norm_url in existing_urls:
        existing_folder = existing_urls[norm_url]
        return True, f"Canonical URL already processed in package: '{existing_folder}'"

    # 2. Check Role + Company signature
    role_sig = get_role_signature(company, title)
    if role_sig and role_sig in existing_roles:
        existing_folder = existing_roles[role_sig]
        return (
            True,
            f"Identical role ({company} - {title}) already prepared in: '{existing_folder}'",
        )

    return False, ""


def prune_duplicate_applications(
    project_root: Path | None = None,
) -> dict[str, Any]:
    """
    Scans the pending applications directory (output/applications/) and purges duplicate packages,
    retaining the primary/most complete package for each job.
    """
    root = project_root or get_project_root()
    apps_base = root / "output" / "applications"
    applied_base = root / "output" / "applied"

    if not apps_base.exists():
        return {
            "status": "success",
            "pruned_count": 0,
            "remaining_count": 0,
            "pruned_details": [],
        }

    # First index all already applied folders (never duplicate an applied job)
    applied_urls: set[str] = set()
    applied_roles: set[str] = set()

    if applied_base.exists():
        for folder in applied_base.iterdir():
            if not folder.is_dir():
                continue
            apply_file = folder / "APPLY_HERE.txt"
            if apply_file.exists():
                try:
                    with open(apply_file, encoding="utf-8") as f:
                        u = normalize_job_url(f.read().strip())
                        if u:
                            applied_urls.add(u)
                except OSError:
                    pass

            parts = folder.name.rsplit("_", 1)
            folder_prefix = parts[0] if len(parts) > 1 else folder.name
            name_parts = folder_prefix.split("_", 1)
            comp = name_parts[0]
            title = name_parts[1] if len(name_parts) > 1 else "role"
            applied_roles.add(get_role_signature(comp, title))

    seen_urls: dict[str, Path] = {}
    seen_roles: dict[str, Path] = {}
    pruned: list[dict[str, str]] = []

    # Sort folders so that older index (original) is kept
    folders = sorted(
        [d for d in apps_base.iterdir() if d.is_dir()],
        key=lambda d: d.name,
    )

    for folder in folders:
        apply_file = folder / "APPLY_HERE.txt"
        raw_url = ""
        if apply_file.exists():
            try:
                with open(apply_file, encoding="utf-8") as f:
                    raw_url = f.read().strip()
            except OSError:
                pass

        norm_url = normalize_job_url(raw_url)

        parts = folder.name.rsplit("_", 1)
        folder_prefix = parts[0] if len(parts) > 1 else folder.name
        name_parts = folder_prefix.split("_", 1)
        comp = name_parts[0]
        title = name_parts[1] if len(name_parts) > 1 else "role"
        role_sig = get_role_signature(comp, title)

        is_dup = False
        reason = ""

        # Check against applied jobs
        if norm_url and norm_url in applied_urls:
            is_dup = True
            reason = "Job was already submitted (found in applied/)"
        elif role_sig in applied_roles:
            is_dup = True
            reason = f"Role '{comp} - {title}' was already submitted in applied/"
        # Check against already seen pending applications
        elif norm_url and norm_url in seen_urls:
            is_dup = True
            primary = seen_urls[norm_url].name
            reason = f"Duplicate canonical URL of '{primary}'"
        elif role_sig in seen_roles:
            is_dup = True
            primary = seen_roles[role_sig].name
            reason = f"Duplicate role ({comp} - {title}) of '{primary}'"

        if is_dup:
            resolved_folder = folder.resolve()
            if str(resolved_folder).startswith(str(apps_base.resolve())):
                try:
                    shutil.rmtree(str(resolved_folder))
                except OSError as err:
                    print(f"Notice deleting duplicate folder {folder.name}: {err}")

            pruned.append(
                {
                    "folder": folder.name,
                    "reason": reason,
                    "url": raw_url,
                }
            )
            # Record as skipped duplicate in database/tracker
            record_application(
                company=comp.replace("-", " "),
                title=title.replace("_", " "),
                job_url=raw_url,
                status="skipped",
                notes=f"Pruned duplicate package: {reason}",
                custom_path=root / "data" / "applications_tracker.csv",
            )
        else:
            if norm_url:
                seen_urls[norm_url] = folder
            if role_sig:
                seen_roles[role_sig] = folder

    remaining = sum(1 for d in apps_base.iterdir() if d.is_dir())
    log_event(
        f"Pruned {len(pruned)} duplicate applications. Remaining in queue: {remaining}",
        level="SUCCESS",
        category="Deduplication",
    )
    return {
        "status": "success",
        "pruned_count": len(pruned),
        "remaining_count": remaining,
        "pruned_details": pruned,
        "message": f"Pruned {len(pruned)} duplicate applications from queue.",
    }
