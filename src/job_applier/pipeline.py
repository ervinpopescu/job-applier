from __future__ import annotations

import os
import shutil
import time

import pandas as pd  # type: ignore[import-untyped]

from job_applier.automation.autofill_script import (  # type: ignore[import-not-found]
    save_autofill_assets,
)
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    load_candidate_profile,
)
from job_applier.automation.queue import enqueue_job  # type: ignore[import-not-found]
from job_applier.config import (  # type: ignore[import-not-found]
    get_platforms_config,
)
from job_applier.dedup import (  # type: ignore[import-not-found]
    get_existing_applications_index,
    get_role_signature,
    is_duplicate_application,
    normalize_job_url,
)
from job_applier.logger import log_event  # type: ignore[import-not-found]
from job_applier.resume.resume import generate_resume
from job_applier.resume.tailor_cv import tailor_resume
from job_applier.scrapers.activity_checker import (  # type: ignore[import-not-found]
    is_job_active,
)
from job_applier.scrapers.job_scraper import fetch_jobs
from job_applier.scrapers.region_config import (  # type: ignore[import-not-found]
    RegionScope,
)
from job_applier.scrapers.url_resolver import resolve_application_url
from job_applier.tracker import record_application  # type: ignore[import-not-found]
from job_applier.db import upsert_application  # type: ignore[import-not-found]
from job_applier.utils import get_project_root, sanitize_name


def run_application_pipeline(
    api_key: str,
    search_terms: list[str] | None = None,
    locations: list[str] | None = None,
    results_wanted: int = 50,
    sites: list[str] | None = None,
    is_remote: bool = False,
    region: str = "EMEA",
    countries: list[str] | None = None,
    emea_only: bool = True,
    auto_apply: bool = False,
    autonomous: bool = False,
    headless: bool = False,
    browser: str | None = None,
) -> None:
    """
    Core pipeline service that scrapes job listings, predicts active status,
    tailors JSON resumes and cover letters via Google Gemini (gemini-3.8-flash),
    generates PDF packages, and optionally triggers autonomous/assisted browser submission.
    """
    project_root = get_project_root()
    master_resume = project_root / "data" / "master_resume.json"
    output_base = project_root / "output" / "applications"
    applied_base = project_root / "output" / "applied"
    processed_jobs_file = project_root / "data" / "processed_jobs.txt"

    output_base.mkdir(parents=True, exist_ok=True)
    applied_base.mkdir(parents=True, exist_ok=True)

    scope = RegionScope(region=region, countries=countries, strict=emea_only)
    candidate_profile = load_candidate_profile(master_resume_path=master_resume)

    active_terms = list(
        search_terms
        or candidate_profile.target_roles
        or get_platforms_config().get("default_search_roles")
        or [
            "Software Engineer",
            "Solutions Architect",
            "DevOps Engineer",
            "Cloud Architect",
            "Python Dev",
        ]
    )
    if locations is None:
        locations = candidate_profile.target_locations or (
            [candidate_profile.city] if candidate_profile.city else ["Bucharest"]
        )

    # Load processed jobs
    processed_urls: set[str] = set()
    if processed_jobs_file.exists():
        try:
            with open(processed_jobs_file, encoding="utf-8") as f:
                processed_urls = set(line.strip() for line in f if line.strip())
        except Exception as e:
            log_event(
                f"Notice reading processed jobs: {e}", level="WARN", category="Pipeline"
            )

    existing_urls, existing_roles = get_existing_applications_index(project_root)

    try:
        for term in active_terms:
            log_event(f"--- Processing jobs for: {term} ---", category="Pipeline")
            csv_path = fetch_jobs(
                search_term=term,
                locations=locations,
                results_wanted=results_wanted,
                sites=sites,
                is_remote=is_remote,
                region=region,
                countries=countries,
                emea_only=emea_only,
            )

            if not csv_path or not os.path.exists(csv_path):
                continue

            try:
                df = pd.read_csv(csv_path)
            except Exception as e:
                log_event(
                    f"Error reading scraped CSV: {e}",
                    level="ERROR",
                    category="Pipeline",
                )
                continue

            for index, row in df.iterrows():
                company = sanitize_name(str(row.get("company", "Unknown_Company")))
                job_title = sanitize_name(str(row.get("title", "Position")))
                jd_text = str(row.get("description", ""))
                job_url_raw = str(row.get("job_url", "")).strip()
                job_url_direct = str(row.get("job_url_direct", "")).strip()

                if not job_url_raw or job_url_raw == "URL not found":
                    continue

                # Skip if already processed
                if job_url_raw in processed_urls:
                    continue

                # Resolve the actual application form website (bypasses aggregator landing pages)
                job_url, detected_platform = resolve_application_url(
                    job_url=job_url_raw,
                    job_url_direct=job_url_direct if job_url_direct else None,
                )

                # Deduplication check by canonical URL and (company, title) signature
                is_dup, dup_reason = is_duplicate_application(
                    job_url=job_url,
                    company=company,
                    title=job_title,
                    existing_urls=existing_urls,
                    existing_roles=existing_roles,
                )
                if is_dup:
                    log_event(
                        f"Skipping duplicate job: {company} - {job_title} ({dup_reason})",
                        level="INFO",
                        category="Pipeline",
                    )
                    processed_urls.add(job_url_raw)
                    continue

                # Validate regional compatibility
                is_comp, comp_reason = scope.is_compatible(
                    str(row.get("location", "")), jd_text
                )
                if not is_comp:
                    log_event(
                        f"Skipping non-matching job for {scope.region}: {company} - {job_title} ({comp_reason})",
                        level="INFO",
                        category="Pipeline",
                    )
                    processed_urls.add(job_url_raw)
                    continue

                # Predict and verify if the job posting is active and accepting applications
                is_active, active_reason = is_job_active(job_url)
                if not is_active:
                    log_event(
                        f"Skipping inactive/expired job: {company} - {job_title} ({active_reason})",
                        level="WARN",
                        category="Pipeline",
                    )
                    processed_urls.add(job_url_raw)
                    try:
                        with open(processed_jobs_file, "a", encoding="utf-8") as f:
                            f.write(f"{job_url_raw}\n")
                    except Exception:
                        pass
                    continue

                if not jd_text or len(jd_text) < 50:
                    jd_text = f"Role: {job_title} at {company}. (Full description unavailable, please assume standard industry requirements for this role.)"

                app_id = f"{company}_{job_title}_{index}"
                app_dir = output_base / app_id
                app_dir.mkdir(parents=True, exist_ok=True)

                # Save the application link for easy access
                try:
                    with open(app_dir / "APPLY_HERE.txt", "w", encoding="utf-8") as f:
                        f.write(job_url)
                except Exception as e:
                    log_event(
                        f"Could not write APPLY_HERE.txt: {e}",
                        level="WARN",
                        category="Pipeline",
                    )

                log_event(
                    f"Tailoring application for: {company} - {job_title}...",
                    category="Pipeline",
                )

                # 1. Tailor CV and get Cover Letter
                tailored_json, cover_letter = tailor_resume(
                    api_key=api_key,
                    master_resume_path=master_resume,
                    job_description=jd_text,
                    output_dir=app_dir,
                    custom_instructions=candidate_profile.custom_ai_instructions,
                    target_role=candidate_profile.current_title or job_title,
                )

                if not tailored_json:
                    log_event(
                        f"Resume tailoring failed or was interrupted for {company} - {job_title}. Cleaning up empty folder.",
                        level="WARN",
                        category="Pipeline",
                    )
                    shutil.rmtree(str(app_dir), ignore_errors=True)
                    continue

                # 2. Generate PDF
                pdf_output = app_dir / f"CV_{company}_{job_title}.pdf"
                generate_resume(tailored_json, str(pdf_output))

                # Validate artifacts: check that CV PDF exists and is non-empty
                # Missing artifacts block enqueueing without deleting unmatched files
                if not pdf_output.exists() or pdf_output.stat().st_size == 0:
                    log_event(
                        f"CV PDF artifact missing or empty for {company} - {job_title}. "
                        "Blocking enqueueing without deleting application folder.",
                        level="WARN",
                        category="Pipeline",
                    )
                    record_application(
                        company=company,
                        title=job_title,
                        job_url=job_url,
                        platform=detected_platform,
                        status="missing_artifacts",
                        submission_type="pipeline_generated",
                        cv_path="",
                        notes="CV PDF generation failed or empty; enqueueing blocked",
                    )
                    continue

                # 3. Generate 1-Click Autofill Assets (Bookmarklet and JS)
                save_autofill_assets(
                    app_dir=app_dir,
                    profile=candidate_profile,
                    cover_letter=cover_letter or "",
                    cv_path=str(pdf_output.resolve()),
                )

                # Mark as processed in tracker
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    platform=detected_platform,
                    status="auto_filled",
                    submission_type="pipeline_generated",
                    cv_path=str(pdf_output),
                )

                # Persist to relational DB and enqueue into automation queue
                norm_adapter = detected_platform.lower()
                if norm_adapter not in {"greenhouse", "lever", "ashby"}:
                    norm_adapter = "generic"

                try:
                    upsert_application(
                        app_id=app_id,
                        company=company,
                        title=job_title,
                        job_url=job_url,
                        platform=detected_platform,
                        status="pending",
                        submission_type="pipeline_generated",
                        folder_name=app_id,
                        cv_filename=pdf_output.name,
                    )
                    enqueue_job(
                        app_id=app_id,
                        adapter=norm_adapter,
                        priority=5,
                        verify_artifacts=True,
                    )
                except Exception as eq_err:
                    log_event(
                        f"Notice: Could not enqueue {app_id} into queue: {eq_err}",
                        level="WARN",
                        category="Pipeline",
                    )

                # Index for runtime deduplication within the same batch
                norm_u = normalize_job_url(job_url)
                if norm_u:
                    existing_urls[norm_u] = app_id
                sig = get_role_signature(company, job_title)
                if sig:
                    existing_roles[sig] = app_id

                # Mark as processed in text tracking file
                try:
                    with open(processed_jobs_file, "a", encoding="utf-8") as f:
                        f.write(f"{job_url}\n")
                    processed_urls.add(job_url)
                except Exception as e:
                    log_event(
                        f"Could not record to processed_jobs.txt: {e}",
                        level="WARN",
                        category="Pipeline",
                    )

                # Avoid hitting rate limits
                time.sleep(2)
    finally:
        pass

    log_event("Pipeline run completed!", level="SUCCESS", category="Pipeline")
