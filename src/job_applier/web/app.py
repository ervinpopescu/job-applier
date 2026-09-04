from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from job_applier.automation.autofill_script import (  # type: ignore[import-not-found]
    save_autofill_assets,
)
from job_applier.automation.browser_automator import (  # type: ignore[import-not-found]
    BrowserAutomator,
)
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    load_candidate_profile,
    save_candidate_profile,
)
from job_applier.logger import (  # type: ignore[import-not-found]
    clear_logs,
    get_recent_logs,
    log_event,
)
from job_applier.pipeline import (  # type: ignore[import-not-found]
    run_application_pipeline,
)
from job_applier.sync import (  # type: ignore[import-not-found]
    export_bundle,
    import_bundle,
)
from job_applier.tracker import (  # type: ignore[import-not-found]
    get_tracker_file,
    get_tracker_stats,
    load_tracker,
    record_application,
)
from job_applier.utils import get_project_root, parse_app_folder_info

load_dotenv()

app = FastAPI(
    title="Job Applier Web Dashboard",
    description="Unified Web App for Scraping, AI Tailoring, and Automated Job Application",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

project_root = get_project_root()
output_apps_dir = project_root / "output" / "applications"
output_applied_dir = project_root / "output" / "applied"
output_apps_dir.mkdir(parents=True, exist_ok=True)
output_applied_dir.mkdir(parents=True, exist_ok=True)

# Mount files directories
app.mount(
    "/files/applications",
    StaticFiles(directory=str(output_apps_dir)),
    name="applications_files",
)
app.mount(
    "/files/applied",
    StaticFiles(directory=str(output_applied_dir)),
    name="applied_files",
)

# Global pipeline run state
PIPELINE_STATE = {
    "is_running": False,
    "status": "idle",
    "started_at": "",
    "logs": [],
    "error": "",
}
pipeline_lock = threading.Lock()

# Global browser automation HUD monitoring state
AUTOMATION_STATE: dict[str, Any] = {
    "is_active": False,
    "app_id": "",
    "company": "",
    "title": "",
    "step": "idle",
    "message": "Ready",
    "level": "INFO",
    "started_at": "",
    "updated_at": "",
    "progress_pct": 0,
    "details": {},
}
automation_lock = threading.Lock()


def set_automation_hud(
    is_active: bool = True,
    app_id: str = "",
    company: str = "",
    title: str = "",
    step: str = "init",
    message: str = "",
    level: str = "INFO",
    progress_pct: int = 0,
    details: dict[str, Any] | None = None,
) -> None:
    """Safely updates real-time HUD monitoring state for web UI."""
    with automation_lock:
        AUTOMATION_STATE["is_active"] = is_active
        if app_id:
            AUTOMATION_STATE["app_id"] = app_id
        if company:
            AUTOMATION_STATE["company"] = company
        if title:
            AUTOMATION_STATE["title"] = title
        AUTOMATION_STATE["step"] = step
        AUTOMATION_STATE["message"] = message
        AUTOMATION_STATE["level"] = level
        AUTOMATION_STATE["progress_pct"] = progress_pct
        AUTOMATION_STATE["updated_at"] = datetime.now().strftime("%H:%M:%S")
        if details:
            AUTOMATION_STATE["details"] = details
        if is_active and not AUTOMATION_STATE.get("started_at"):
            AUTOMATION_STATE["started_at"] = datetime.now().strftime("%H:%M:%S")
        elif not is_active:
            AUTOMATION_STATE["started_at"] = ""


# Pydantic models for request bodies
class ApplyRequest(BaseModel):
    mode: str = "assisted"  # 'assisted' or 'autonomous'
    headless: bool | None = None


class BatchApplyRequest(BaseModel):
    app_ids: list[str] | None = None
    count: int = 5
    mode: str = "assisted"
    headless: bool | None = None


class CoverLetterUpdate(BaseModel):
    cover_letter: str


class CleanupRequest(BaseModel):
    older_than_days: int = 1
    archive: bool = True


class PipelineRunRequest(BaseModel):
    terms: list[str] = Field(
        default_factory=lambda: ["Cloud Architect", "DevOps Engineer"]
    )
    locations: list[str] = Field(default_factory=lambda: ["Bucharest"])
    limit: int = 20
    sites: list[str] = Field(
        default_factory=lambda: [
            "linkedin",
            "indeed",
            "ejobs",
            "bestjobs",
            "hipo",
            "undelucram",
            "jooble",
            "google",
            "greenhouse",
            "lever",
            "remote",
            "glassdoor",
        ]
    )
    auto_apply: bool = False
    autonomous: bool = False
    headless: bool = False
    is_remote: bool = False
    region: str = "EMEA"
    countries: list[str] = Field(default_factory=list)
    strict_region: bool = True


class PruneRegionRequest(BaseModel):
    region: str = "EMEA"
    countries: list[str] = Field(default_factory=list)
    strict: bool = True


class StatusUpdateRequest(BaseModel):
    job_url: str
    status: str
    notes: str = ""


class RequeueRequest(BaseModel):
    job_url: str = ""
    folder_name: str = ""


class SubmitCodeRequest(BaseModel):
    code: str


class AuthLoginRequest(BaseModel):
    platform: str
    timeout: int = 180


def get_safe_app_folder(app_id: str) -> Path:
    """Safely resolves an application folder inside output_apps_dir preventing path traversal."""
    safe_name = Path(app_id).name
    folder = (output_apps_dir / safe_name).resolve()
    base_resolved = output_apps_dir.resolve()
    if (
        not str(folder).startswith(str(base_resolved))
        or not folder.exists()
        or not folder.is_dir()
    ):
        raise HTTPException(status_code=404, detail="Application not found")
    return folder


@app.get("/api/health", include_in_schema=False)
def get_health() -> dict[str, str]:
    """Returns a lightweight liveness response for containers and load balancers."""
    return {"status": "ok", "service": "job-applier"}


@app.get("/", response_class=HTMLResponse)
def get_dashboard() -> HTMLResponse:
    """Renders the main single-page web dashboard (prefers compiled Angular SPA if built)."""
    angular_dist = (
        project_root / "frontend" / "dist" / "frontend" / "browser"
    ).resolve()
    angular_index = angular_dist / "index.html"
    if str(angular_index).startswith(str(angular_dist)) and angular_index.is_file():
        try:
            with open(angular_index, encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        except OSError as err:
            print(f"Notice loading Angular index: {err}")

    template_path = Path(__file__).resolve().parent / "templates" / "index.html"
    if not template_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard template not found.")
    try:
        with open(template_path, encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error loading template: {e}"
        ) from e


@app.get("/favicon.ico", include_in_schema=False)
def get_favicon_ico() -> FileResponse:
    ico_path = (project_root / "frontend" / "public" / "favicon.ico").resolve()
    if ico_path.is_file():
        return FileResponse(ico_path, media_type="image/x-icon")
    raise HTTPException(status_code=404, detail="Favicon not found")


@app.get("/favicon.svg", include_in_schema=False)
def get_favicon_svg() -> FileResponse:
    svg_path = (project_root / "frontend" / "public" / "favicon.svg").resolve()
    if svg_path.is_file():
        return FileResponse(svg_path, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Favicon not found")


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    """Returns aggregated pipeline and application metrics."""
    tracker_stats = get_tracker_stats()
    pending_count = 0
    if output_apps_dir.exists():
        pending_count = sum(1 for d in output_apps_dir.iterdir() if d.is_dir())

    applied_count = 0
    if output_applied_dir.exists():
        applied_count = sum(1 for d in output_applied_dir.iterdir() if d.is_dir())

    return {
        "pending": pending_count,
        "applied_folders": applied_count,
        "tracker": tracker_stats,
        "pipeline": {
            "is_running": PIPELINE_STATE["is_running"],
            "status": PIPELINE_STATE["status"],
        },
    }


@app.get("/api/applications")
def list_applications(
    search: str | None = Query(None, description="Search keyword"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Lists pending applications with metadata."""
    if not output_apps_dir.exists():
        return {"items": [], "total": 0, "offset": offset, "limit": limit}

    folders = sorted(
        [d for d in output_apps_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )

    if search:
        s_lower = search.lower()
        folders = [d for d in folders if s_lower in d.name.lower()]

    total = len(folders)
    page_folders = folders[offset : offset + limit]

    items = []
    for app_folder in page_folders:
        info = parse_app_folder_info(app_folder)
        items.append(
            {
                "id": app_folder.name,
                "company": info["company"],
                "title": info["title"],
                "job_url": info["job_url"],
                "cv_filename": info["cv_name"],
                "has_cover_letter": info["has_cover_letter"] == "Yes",
                "cover_letter_preview": (info["cover_letter_text"][:160] + "...")
                if info["cover_letter_text"]
                else "",
                "pdf_url": f"/files/applications/{app_folder.name}/{info['cv_name']}"
                if info["cv_name"] != "None"
                else "",
                "created_at": datetime.fromtimestamp(
                    app_folder.stat().st_mtime
                ).strftime("%Y-%m-%d %H:%M"),
            }
        )

    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/applications/{app_id}")
def get_application(app_id: str) -> dict[str, Any]:
    """Retrieves full details and file contents for a specific pending application."""
    app_folder = get_safe_app_folder(app_id)
    info = parse_app_folder_info(app_folder)

    # Load tailored json if available
    tailored_json_path = app_folder / "tailored_resume.json"
    tailored_data = {}
    if tailored_json_path.exists():
        try:
            with open(tailored_json_path, encoding="utf-8") as f:
                tailored_data = json.load(f)
        except (json.JSONDecodeError, OSError) as load_err:
            print(f"Notice: Failed to load tailored resume JSON: {load_err}")

    # Read bookmarklet string
    bm_path = app_folder / "autofill_bookmarklet.txt"
    bookmarklet_str = ""
    if bm_path.exists():
        try:
            with open(bm_path, encoding="utf-8") as f:
                bookmarklet_str = f.read().strip()
        except OSError as load_err:
            print(f"Notice: Failed to load autofill bookmarklet: {load_err}")

    # If bookmarklet is missing or empty, generate it on the fly!
    if not bookmarklet_str:
        try:
            profile = load_candidate_profile()
            save_autofill_assets(
                app_dir=app_folder,
                profile=profile,
                cover_letter=info["cover_letter_text"],
                cv_path=info["cv_path"],
            )
            if bm_path.exists():
                with open(bm_path, encoding="utf-8") as f:
                    bookmarklet_str = f.read().strip()
        except Exception as gen_err:
            print(f"Notice auto-generating bookmarklet: {gen_err}")

    # Check for proof or error screenshots
    proof_img = app_folder / "submission_proof.png"
    fail_img = app_folder / "submission_failed.png"

    return {
        "id": app_id,
        "company": info["company"],
        "title": info["title"],
        "job_url": info["job_url"],
        "cv_filename": info["cv_name"],
        "cv_pdf_url": f"/files/applications/{app_id}/{info['cv_name']}"
        if info["cv_name"] != "None"
        else "",
        "cover_letter": info["cover_letter_text"],
        "tailored_resume": tailored_data,
        "bookmarklet": bookmarklet_str,
        "proof_screenshot_url": f"/files/applications/{app_id}/submission_proof.png"
        if proof_img.exists()
        else "",
        "fail_screenshot_url": f"/files/applications/{app_id}/submission_failed.png"
        if fail_img.exists()
        else "",
    }


@app.post("/api/applications/{app_id}/update-cover-letter")
def update_cover_letter(app_id: str, body: CoverLetterUpdate) -> dict[str, str]:
    """Updates the cover letter text for an application."""
    app_folder = get_safe_app_folder(app_id)

    cl_file = app_folder / "cover_letter.txt"
    try:
        with open(cl_file, "w", encoding="utf-8") as f:
            f.write(body.cover_letter)

        # Regenerate autofill assets with updated cover letter
        profile = load_candidate_profile()
        save_autofill_assets(app_folder, profile, cover_letter=body.cover_letter)
        return {"status": "success", "message": "Cover letter updated"}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to update cover letter: {e}"
        ) from e


@app.post("/api/applications/{app_id}/regenerate-cv")
def regenerate_application_cv(app_id: str) -> dict[str, Any]:
    """Re-triggers AI resume tailoring and PDF generation for an application package."""
    import requests
    from bs4 import BeautifulSoup

    from job_applier.automation.autofill_script import save_autofill_assets
    from job_applier.resume.resume import generate_resume
    from job_applier.resume.tailor_cv import tailor_resume
    from job_applier.tracker import record_application
    from job_applier.utils import sanitize_name

    app_folder = get_safe_app_folder(app_id)
    info = parse_app_folder_info(app_folder)

    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        load_dotenv(project_root / ".env")
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="GOOGLE_API_KEY is missing. Please set it in your .env file.",
        )

    master_resume = project_root / "data" / "master_resume.json"
    if not master_resume.exists():
        raise HTTPException(
            status_code=404,
            detail="Master resume not found at data/master_resume.json",
        )

    candidate_profile = load_candidate_profile(master_resume_path=master_resume)

    # 1. Obtain Job Description (fetch from URL or use fallback)
    jd_text = ""
    job_url = info.get("job_url", "")
    if job_url.startswith("http"):
        try:
            resp = requests.get(
                job_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for sel in [
                    "#content",
                    ".content",
                    ".job-description",
                    ".description",
                    "main",
                    "body",
                ]:
                    el = soup.select_one(sel)
                    if el and len(el.text.strip()) > 100:
                        jd_text = el.text.strip()
                        break
        except (requests.RequestException, OSError) as fetch_err:
            print(f"Notice fetching job description: {fetch_err}")

    if not jd_text or len(jd_text) < 50:
        jd_text = f"Role: {info['title']} at {info['company']}. Standard industry requirements for this technical position."

    # 2. Call Gemini AI to tailor resume and generate cover letter
    tailored_json, cover_letter = tailor_resume(
        api_key=api_key,
        master_resume_path=master_resume,
        job_description=jd_text,
        output_dir=app_folder,
        custom_instructions=candidate_profile.custom_ai_instructions,
        target_role=candidate_profile.current_title or info["title"],
    )

    if not tailored_json:
        raise HTTPException(
            status_code=500,
            detail="Gemini AI tailoring failed. Please check your API quota or model settings.",
        )

    # 3. Generate Styled PDF
    comp_clean = sanitize_name(info["company"] or "Company")
    title_clean = sanitize_name(info["title"] or "Role")
    pdf_name = f"CV_{comp_clean}_{title_clean}.pdf"
    pdf_path = app_folder / pdf_name
    generate_resume(tailored_json, str(pdf_path))

    # 4. Update Autofill Assets & Bookmarklet
    save_autofill_assets(
        app_dir=app_folder,
        profile=candidate_profile,
        cover_letter=cover_letter or "",
        cv_path=str(pdf_path.resolve()),
    )

    # 5. Update Application Record in Tracker
    record_application(
        company=info["company"],
        title=info["title"],
        job_url=info["job_url"],
        status="auto_filled",
        cv_path=str(pdf_path),
        notes="Regenerated tailored CV and cover letter",
    )

    return {
        "status": "success",
        "cv_filename": pdf_name,
        "cv_pdf_url": f"/files/applications/{app_folder.name}/{pdf_name}",
        "cover_letter": cover_letter or "",
        "message": f"Successfully generated tailored CV for {info['company']}!",
    }


@app.post("/api/applications/{app_id}/apply")
def run_auto_apply_for_job(
    app_id: str,
    body: ApplyRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Spawns browser auto-apply in the background and streams live HUD updates."""
    app_folder = get_safe_app_folder(app_id)
    info = parse_app_folder_info(app_folder)
    if not info["job_url"].startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid job URL")

    with automation_lock:
        if AUTOMATION_STATE.get("is_active"):
            raise HTTPException(
                status_code=409,
                detail=f"Automator is currently applying for {AUTOMATION_STATE.get('company')} — please wait.",
            )
        set_automation_hud(
            is_active=True,
            app_id=app_id,
            company=info["company"],
            title=info["title"],
            step="init",
            message=f"Starting auto-apply for {info['company']}...",
            progress_pct=10,
        )

    def hud_callback(
        step: str, message: str, level: str, details: dict[str, Any]
    ) -> None:
        step_progress = {
            "init": 10,
            "navigating": 25,
            "cloudflare_check": 35,
            "overlay_dismiss": 45,
            "form_detection": 55,
            "autofill_contact": 70,
            "resume_upload": 80,
            "comboboxes": 85,
            "submitting": 90,
            "verifying": 95,
            "submitted": 100,
            "submission_failed": 100,
        }
        set_automation_hud(
            is_active=True,
            app_id=app_id,
            company=info["company"],
            title=info["title"],
            step=step,
            message=message,
            level=level,
            progress_pct=step_progress.get(step, 50),
            details=details,
        )

    def run_worker():
        automator = BrowserAutomator(
            headless=body.headless, status_callback=hud_callback
        )
        try:
            automator.start()
            if body.mode == "autonomous":
                status, msg = automator.run_autonomous_apply(
                    app_dir=app_folder,
                    job_url=info["job_url"],
                    company=info["company"],
                    job_title=info["title"],
                )
            else:
                status, msg = automator.run_assisted_apply(
                    app_dir=app_folder,
                    job_url=info["job_url"],
                    company=info["company"],
                    job_title=info["title"],
                    non_interactive=True,
                )

            if status == "applied":
                target_dir = output_applied_dir / app_folder.name
                try:
                    shutil.move(str(app_folder), str(target_dir))
                except Exception as e:
                    print(f"Notice moving folder: {e}")

            set_automation_hud(
                is_active=False,
                step="idle",
                message=f"Completed {status}: {msg}",
                progress_pct=100,
            )
        except Exception as e:
            set_automation_hud(
                is_active=False,
                step="error",
                message=f"Error: {e}",
                level="ERROR",
                progress_pct=100,
            )
        finally:
            automator.close()

    background_tasks.add_task(run_worker)
    return {
        "status": "started",
        "message": f"Auto-apply started for {info['company']}",
        "app_id": app_id,
    }


@app.post("/api/applications/{app_id}/mark-done")
def mark_application_done(app_id: str) -> dict[str, str]:
    """Moves application folder to applied and records status in tracker."""
    app_folder = get_safe_app_folder(app_id)
    info = parse_app_folder_info(app_folder)
    target_dir = output_applied_dir / app_folder.name

    try:
        shutil.move(str(app_folder), str(target_dir))
        record_application(
            company=info["company"],
            title=info["title"],
            job_url=info["job_url"],
            status="applied",
            submission_type="web_dashboard",
            cv_path=info["cv_path"],
            notes="Marked as done from Web Dashboard",
        )
        return {"status": "success", "message": f"Moved to applied: {target_dir.name}"}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to move application: {e}"
        ) from e


@app.delete("/api/applications/{app_id}")
def delete_application(app_id: str) -> dict[str, str]:
    """Deletes or dismisses a pending application."""
    app_folder = get_safe_app_folder(app_id)

    try:
        shutil.rmtree(str(app_folder))
        return {"status": "success", "message": "Application removed"}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete folder: {e}"
        ) from e


@app.post("/api/applications/cleanup")
def cleanup_applications(body: CleanupRequest) -> dict[str, Any]:
    """Archives and cleans up old pending applications from output/applications/."""
    if not output_apps_dir.exists():
        return {"status": "success", "removed_count": 0, "remaining_count": 0}

    now = time.time()
    cutoff_seconds = body.older_than_days * 86400

    folders_to_delete = []
    for folder in output_apps_dir.iterdir():
        if not folder.is_dir():
            continue
        mtime = folder.stat().st_mtime
        if body.older_than_days <= 0 or (now - mtime) > cutoff_seconds:
            folders_to_delete.append(folder)

    # If archive requested and deleting files, create backup archive
    if body.archive and folders_to_delete:
        try:
            shutil.make_archive(
                base_name=str(project_root / "output" / "archive_applications_backup"),
                format="gztar",
                root_dir=str(output_apps_dir),
            )
        except Exception as e:
            print(f"Notice during archive creation: {e}")

    removed_count = 0
    for folder in folders_to_delete:
        try:
            shutil.rmtree(str(folder))
            removed_count += 1
        except Exception as e:
            print(f"Notice: Could not delete {folder}: {e}")

    remaining = sum(1 for d in output_apps_dir.iterdir() if d.is_dir())
    return {
        "status": "success",
        "message": f"Cleaned up {removed_count} old applications. {remaining} remaining.",
        "removed_count": removed_count,
        "remaining_count": remaining,
    }


@app.post("/api/applications/clear-failed")
def clear_failed_applications() -> dict[str, Any]:
    """Removes all application packages that failed submission from the queue and tracker CSV."""
    if not output_apps_dir.exists():
        return {"status": "success", "removed_count": 0, "remaining_count": 0}

    tracker_f = get_tracker_file()
    df = load_tracker()

    failed_urls = set()
    failed_folder_names = set()

    if not df.empty and "status" in df.columns:
        # Match 'failed' and 'validation_failed' entries
        failed_rows = df[df["status"].isin(["failed", "validation_failed"])]
        for _, row in failed_rows.iterrows():
            url = str(row.get("job_url", "")).strip()
            if url:
                failed_urls.add(url)

            # Extract folder name from cv_path if available
            cv_path = str(row.get("cv_path", "")).strip()
            if cv_path:
                path_obj = Path(cv_path)
                if path_obj.parts:
                    # e.g., the folder parent is the application folder name
                    failed_folder_names.add(path_obj.parent.name)

    removed_count = 0
    # Search and delete folders in applications queue matching failed URLs or folder names
    for folder in output_apps_dir.iterdir():
        if not folder.is_dir():
            continue

        should_delete = False
        if folder.name in failed_folder_names:
            should_delete = True

        if not should_delete:
            apply_file = folder / "APPLY_HERE.txt"
            if apply_file.exists():
                try:
                    with open(apply_file, encoding="utf-8") as f:
                        url = f.read().strip()
                        if url in failed_urls:
                            should_delete = True
                except OSError:
                    pass

        # Also fallback: if the folder contains 'submission_failed.png', delete it
        if not should_delete and (folder / "submission_failed.png").exists():
            should_delete = True

        if should_delete:
            try:
                resolved_folder = folder.resolve()
                # Ensure the resolved folder is strictly inside the applications directory to prevent traversal
                if str(resolved_folder).startswith(str(output_apps_dir.resolve())):
                    shutil.rmtree(str(resolved_folder))
                    removed_count += 1
            except Exception as e:
                print(f"Notice deleting failed folder {folder.name}: {e}")

    # Also clean up failed records from the tracker CSV (always purge the database rows, even if folders are already missing)
    db_removed_count = 0
    if not df.empty and "status" in df.columns:
        initial_rows = len(df)
        filtered_df = df[~df["status"].isin(["failed", "validation_failed"])]
        db_removed_count = initial_rows - len(filtered_df)
        try:
            filtered_df.to_csv(tracker_f, index=False)
        except Exception as e:
            print(f"Notice saving tracker after clear: {e}")

    remaining = sum(1 for d in output_apps_dir.iterdir() if d.is_dir())
    message = (
        f"Successfully cleared failed records! Purged {db_removed_count} database rows"
    )
    if removed_count > 0:
        message += f" and deleted {removed_count} directories."
    else:
        message += " (folders were already removed)."

    return {
        "status": "success",
        "message": message,
        "removed_count": db_removed_count,
        "remaining_count": remaining,
    }


@app.post("/api/applications/prune-inactive")
def prune_inactive_applications() -> dict[str, Any]:
    """Scans all queued applications, verifies live active status, and removes expired/closed jobs."""
    from job_applier.scrapers.activity_checker import is_job_active

    if not output_apps_dir.exists():
        return {"status": "success", "pruned_count": 0, "remaining_count": 0}

    pruned = []
    for folder in list(output_apps_dir.iterdir()):
        if not folder.is_dir():
            continue
        apply_file = folder / "APPLY_HERE.txt"
        if not apply_file.exists():
            continue
        try:
            with open(apply_file, encoding="utf-8") as f:
                job_url = f.read().strip()
            if not job_url or not job_url.startswith("http"):
                continue

            is_active, reason = is_job_active(job_url, timeout=5)
            if not is_active:
                resolved_folder = folder.resolve()
                if str(resolved_folder).startswith(str(output_apps_dir.resolve())):
                    shutil.rmtree(str(resolved_folder))
                pruned.append(
                    {"app_id": folder.name, "reason": reason, "job_url": job_url}
                )
                record_application(
                    company="",
                    title="",
                    job_url=job_url,
                    status="skipped",
                    notes=f"Pruned inactive job: {reason}",
                )
        except Exception as e:
            print(f"Notice pruning check for {folder.name}: {e}")

    remaining = sum(1 for d in output_apps_dir.iterdir() if d.is_dir())
    return {
        "status": "success",
        "pruned_count": len(pruned),
        "remaining_count": remaining,
        "pruned_details": pruned,
        "message": f"Pruned {len(pruned)} inactive/expired applications from the queue.",
    }


@app.post("/api/applications/prune-by-region")
def prune_by_region(body: PruneRegionRequest) -> dict[str, Any]:
    """Prunes queued applications that do not match the specified region or country scope."""
    from job_applier.scrapers.region_config import RegionScope

    if not output_apps_dir.exists():
        return {"status": "success", "pruned_count": 0, "remaining_count": 0}

    scope = RegionScope(
        region=body.region, countries=body.countries, strict=body.strict
    )
    pruned = []

    for folder in list(output_apps_dir.iterdir()):
        if not folder.is_dir():
            continue
        info = parse_app_folder_info(folder)
        job_loc = ""
        desc_text = info.get("cover_letter_preview", "")

        tr_file = folder / "tailored_resume.json"
        if tr_file.exists():
            try:
                with open(tr_file, encoding="utf-8") as f:
                    tr_data = json.load(f)
                    job_loc = tr_data.get("job_location", "")
            except Exception:
                pass

        if not job_loc:
            # Check if location tokens are present in folder name
            parts = folder.name.lower().split("_")
            for p in parts:
                if p in [
                    "remote",
                    "bucharest",
                    "romania",
                    "uk",
                    "emea",
                    "germany",
                    "france",
                    "netherlands",
                    "us",
                    "usa",
                    "amer",
                    "latam",
                    "apac",
                ]:
                    job_loc = p
                    break

        if not job_loc:
            job_loc = "Remote"

        is_compat, reason = scope.is_compatible(job_loc, desc_text)
        if not is_compat:
            resolved_folder = folder.resolve()
            if str(resolved_folder).startswith(str(output_apps_dir.resolve())):
                try:
                    shutil.rmtree(str(resolved_folder))
                except OSError as err:
                    print(f"Notice pruning folder {folder.name}: {err}")
            pruned.append(
                {"app_id": folder.name, "reason": reason, "location": job_loc}
            )
            record_application(
                company="",
                title="",
                job_url=info.get("job_url", ""),
                status="skipped",
                notes=f"Pruned non-matching region ({scope.region}): {reason}",
            )

    remaining = sum(1 for d in output_apps_dir.iterdir() if d.is_dir())
    return {
        "status": "success",
        "pruned_count": len(pruned),
        "remaining_count": remaining,
        "region": scope.region,
        "message": f"Pruned {len(pruned)} applications outside region '{scope.region}'.",
    }


@app.post("/api/applications/prune-duplicates")
def prune_duplicates_endpoint() -> dict[str, Any]:
    """Scans pending applications and removes duplicate packages matching canonical URLs or identical roles."""
    from job_applier.dedup import prune_duplicate_applications

    return prune_duplicate_applications()


@app.get("/api/automation/status")
def get_automation_status() -> dict[str, Any]:
    """Returns real-time browser automation HUD monitoring status."""
    from job_applier.automation.browser_automator import get_active_automator

    with automation_lock:
        state = dict(AUTOMATION_STATE)

    automator = get_active_automator()
    if automator and automator.waiting_for_code:
        state["is_waiting_for_code"] = True
        state["step"] = "verification_code_required"
        state["message"] = (
            "🔑 Security verification code required! Check your email or SMS and submit the code below."
        )
    else:
        state["is_waiting_for_code"] = False

    return state


@app.post("/api/automation/submit-code")
def submit_verification_code(body: SubmitCodeRequest) -> dict[str, Any]:
    """Submits a 2FA or email verification code to the active browser automation session."""
    from job_applier.automation.browser_automator import get_active_automator

    automator = get_active_automator()
    if not automator:
        raise HTTPException(
            status_code=400,
            detail="No active browser session is currently waiting for a verification code.",
        )

    automator.supply_verification_code(body.code)
    return {
        "status": "success",
        "message": f"Verification code '{body.code}' submitted to browser.",
    }


@app.get("/api/auth/status")
def get_auth_status() -> dict[str, Any]:
    """Returns platform authentication status for LinkedIn, BestJobs, eJobs, and Google."""
    from job_applier.automation.auth_manager import AuthManager

    manager = AuthManager()
    return manager.check_auth_status()


@app.post("/api/auth/login")
def launch_auth_login(
    body: AuthLoginRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Launches an interactive headed browser session to log in to a platform and save cookies."""
    from job_applier.automation.auth_manager import AuthManager

    platform = body.platform.strip().lower()
    if platform not in ["linkedin", "bestjobs", "ejobs", "google"]:
        raise HTTPException(
            status_code=400, detail=f"Unsupported platform: {body.platform}"
        )

    manager = AuthManager()

    def run_login_worker():
        manager.launch_interactive_login(
            platform=platform, timeout_seconds=body.timeout
        )

    background_tasks.add_task(run_login_worker)
    return {
        "status": "started",
        "platform": platform,
        "message": f"Launched login window for {platform.upper()}. Please complete sign-in in the Chrome window.",
    }


@app.post("/api/auth/sync-chrome")
def sync_chrome_cookies() -> dict[str, Any]:
    """Syncs existing authenticated sessions from desktop Chrome into the persistent profile."""
    from job_applier.automation.auth_manager import AuthManager

    manager = AuthManager()
    return manager.sync_desktop_cookies()


@app.get("/api/applications/{app_id}/diagnostics")
def get_application_diagnostics(app_id: str) -> dict[str, Any]:
    """Retrieves full diagnostic logs, failure screenshots, and DOM inspection for an application."""
    safe_name = Path(app_id).name
    candidate_app = (output_apps_dir / safe_name).resolve()
    candidate_applied = (output_applied_dir / safe_name).resolve()

    app_folder = None
    if (
        str(candidate_app).startswith(str(output_apps_dir.resolve()))
        and candidate_app.exists()
        and candidate_app.is_dir()
    ):
        app_folder = candidate_app
    elif (
        str(candidate_applied).startswith(str(output_applied_dir.resolve()))
        and candidate_applied.exists()
        and candidate_applied.is_dir()
    ):
        app_folder = candidate_applied

    if not app_folder:
        raise HTTPException(status_code=404, detail="Application folder not found")

    diag_file = app_folder / "diagnostics.json"
    diag_data = {}
    if diag_file.exists():
        try:
            with open(diag_file, encoding="utf-8") as f:
                diag_data = json.load(f)
        except (json.JSONDecodeError, OSError) as err:
            print(f"Notice: Failed loading diagnostics JSON: {err}")

    fail_img = app_folder / "submission_failed.png"
    proof_img = app_folder / "submission_proof.png"
    dom_file = app_folder / "diagnostic_dom.html"

    parent_subpath = "applications" if "applications" in str(app_folder) else "applied"

    return {
        "app_id": app_id,
        "diagnostics": diag_data,
        "has_failed_screenshot": fail_img.exists(),
        "failed_screenshot_url": f"/files/{parent_subpath}/{app_folder.name}/submission_failed.png"
        if fail_img.exists()
        else "",
        "has_proof_screenshot": proof_img.exists(),
        "proof_screenshot_url": f"/files/{parent_subpath}/{app_folder.name}/submission_proof.png"
        if proof_img.exists()
        else "",
        "has_dom_dump": dom_file.exists(),
        "dom_url": f"/files/{parent_subpath}/{app_folder.name}/diagnostic_dom.html"
        if dom_file.exists()
        else "",
    }


@app.post("/api/batch-apply")
def batch_apply(
    body: BatchApplyRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Launches sequential batch auto-apply."""
    folders = []
    if body.app_ids:
        folders = [
            output_apps_dir / aid
            for aid in body.app_ids
            if (output_apps_dir / aid).exists()
        ]
    else:
        folders = sorted([d for d in output_apps_dir.iterdir() if d.is_dir()])[
            : body.count
        ]

    if not folders:
        raise HTTPException(
            status_code=400, detail="No valid applications found for batch"
        )

    def run_batch():
        try:
            automator = BrowserAutomator(headless=body.headless)
            try:
                automator.start()
                for f in folders:
                    info = parse_app_folder_info(f)
                    if not info["job_url"].startswith("http"):
                        continue
                    if body.mode == "autonomous":
                        status, _ = automator.run_autonomous_apply(
                            f, info["job_url"], info["company"], info["title"]
                        )
                    else:
                        status, _ = automator.run_assisted_apply(
                            f,
                            info["job_url"],
                            info["company"],
                            info["title"],
                            non_interactive=True,
                        )

                    if status == "applied":
                        target = output_applied_dir / f.name
                        try:
                            shutil.move(str(f), str(target))
                        except Exception:
                            pass
            finally:
                automator.close()
        except Exception as e:
            print(f"Notice: Batch auto-apply ended with message: {e}")

    background_tasks.add_task(run_batch)
    return {
        "status": "started",
        "message": f"Started batch application for {len(folders)} jobs in background.",
        "count": len(folders),
    }


@app.get("/api/tracker")
def get_tracker_data(status: str | None = Query(None)) -> dict[str, Any]:
    """Returns applications from the tracker CSV with optional status filtering and folders in applied/."""
    df = load_tracker()

    records = []
    if not df.empty:
        if status:
            df = df[df["status"].str.lower() == status.lower()]
        records = [
            dict(zip(df.columns, [str(v) if v is not None else "" for v in row]))
            for row in df.values
        ]

    # Map existing applied folders to records
    applied_urls = {r.get("job_url", "").strip() for r in records if r.get("job_url")}
    if output_applied_dir.exists():
        for folder in output_applied_dir.iterdir():
            if not folder.is_dir():
                continue
            info = parse_app_folder_info(folder)
            job_url = info["job_url"].strip()
            # If not tracked yet in CSV, add synthetic record
            if job_url and job_url not in applied_urls:
                records.append(
                    {
                        "timestamp": datetime.fromtimestamp(
                            folder.stat().st_mtime
                        ).strftime("%Y-%m-%d %H:%M:%S"),
                        "company": info["company"],
                        "title": info["title"],
                        "job_url": job_url,
                        "platform": "Generic",
                        "status": "applied",
                        "submission_type": "folder_applied",
                        "cv_path": info["cv_path"],
                        "proof_path": "",
                        "notes": "Application package in applied folder",
                        "folder_name": folder.name,
                    }
                )
                applied_urls.add(job_url)

    # Attach folder_name to records where missing
    for r in records:
        if not r.get("folder_name"):
            url = r.get("job_url", "").strip()
            for f in output_applied_dir.iterdir():
                if not f.is_dir():
                    continue
                app_file = f / "APPLY_HERE.txt"
                if app_file.exists():
                    try:
                        with open(app_file, encoding="utf-8") as af:
                            if af.read().strip() == url:
                                r["folder_name"] = f.name
                                break
                    except OSError:
                        pass

    return {
        "records": records,
        "total": len(records),
        "stats": get_tracker_stats(),
    }


@app.post("/api/tracker/update-status")
def update_tracker_job_status(body: StatusUpdateRequest) -> dict[str, str]:
    """Updates status for a tracked job (e.g. interviewing, offered, rejected)."""
    record_application(
        company="",
        title="",
        job_url=body.job_url,
        status=body.status,
        notes=body.notes,
    )
    return {"status": "success", "message": f"Updated {body.job_url} to {body.status}"}


@app.get("/api/export")
def export_applications_bundle() -> FileResponse:
    """Creates and downloads a portable .zip backup containing all applications, database mappings, and profile."""
    try:
        zip_path = export_bundle()
        return FileResponse(
            path=str(zip_path),
            filename=zip_path.name,
            media_type="application/zip",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {e}") from e


@app.post("/api/import")
def import_applications_bundle(file: UploadFile = File(...)) -> dict[str, Any]:
    """Uploads and merges a portable backup .zip bundle from another machine."""
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(
            status_code=400, detail="Only .zip backup bundles are supported."
        )

    export_temp_dir = project_root / "output" / "exports"
    export_temp_dir.mkdir(parents=True, exist_ok=True)
    temp_zip = (
        export_temp_dir / f"_temp_import_{datetime.now().strftime('%Y%m%d%H%M%S')}.zip"
    )
    try:
        with open(temp_zip, "wb") as f:
            shutil.copyfileobj(file.file, f)

        res = import_bundle(temp_zip)
        return {
            "status": "success",
            "message": f"Successfully imported {res['imported_applications']} applications and merged {res['merged_db_records']} records.",
            "report": res,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {e}") from e
    finally:
        if temp_zip.exists():
            temp_zip.unlink(missing_ok=True)


@app.post("/api/tracker/requeue")
def requeue_application(body: RequeueRequest) -> dict[str, str]:
    """Moves an application from output/applied back to output/applications and updates tracker."""
    found_folder: Path | None = None

    # 1. Search by folder_name if provided
    if body.folder_name:
        safe_name = Path(body.folder_name).name
        candidate = (output_applied_dir / safe_name).resolve()
        if (
            str(candidate).startswith(str(output_applied_dir.resolve()))
            and candidate.exists()
            and candidate.is_dir()
        ):
            found_folder = candidate

    # 2. Search by matching job_url in applied folders
    if found_folder is None and body.job_url:
        for folder in output_applied_dir.iterdir():
            if not folder.is_dir():
                continue
            apply_file = folder / "APPLY_HERE.txt"
            if apply_file.exists():
                try:
                    with open(apply_file, encoding="utf-8") as f:
                        if f.read().strip() == body.job_url.strip():
                            found_folder = folder
                            break
                except OSError:
                    continue

    if found_folder is None:
        if body.job_url:
            record_application(
                company="",
                title="",
                job_url=body.job_url,
                status="auto_filled",
                notes="Requeued to pending",
            )
            return {"status": "success", "message": "Updated tracker status to pending"}
        raise HTTPException(
            status_code=404, detail="Applied application folder not found"
        )

    target_dir = output_apps_dir / found_folder.name
    try:
        shutil.move(str(found_folder), str(target_dir))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to move folder back to queue: {e}"
        ) from e

    # Update tracker record
    record_application(
        company="",
        title="",
        job_url=body.job_url,
        status="auto_filled",
        notes="Sent back to queue from tracker",
    )

    return {
        "status": "success",
        "message": f"Successfully moved {found_folder.name} back to queue!",
    }


@app.get("/api/profile")
def get_profile() -> dict[str, Any]:
    """Returns candidate profile data."""
    profile = load_candidate_profile()
    return profile.to_dict()


@app.post("/api/profile")
def update_profile(body: dict[str, Any]) -> dict[str, Any]:
    """Updates candidate profile in data/candidate_profile.json preserving unmentioned fields."""
    profile = load_candidate_profile()
    for k, v in body.items():
        if hasattr(profile, k) and v is not None:
            setattr(profile, k, v)
    save_candidate_profile(profile)
    return {"status": "success", "profile": profile.to_dict()}


@app.post("/api/pipeline/run")
def trigger_pipeline(
    body: PipelineRunRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Triggers scraping + tailoring pipeline in background."""
    with pipeline_lock:
        if PIPELINE_STATE["is_running"]:
            raise HTTPException(
                status_code=409, detail="A pipeline run is already in progress."
            )
        PIPELINE_STATE["is_running"] = True
        PIPELINE_STATE["status"] = "running"
        PIPELINE_STATE["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        PIPELINE_STATE["logs"] = []
        PIPELINE_STATE["error"] = ""
        clear_logs()

    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        load_dotenv(project_root / ".env")
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="GOOGLE_API_KEY is missing. Please set it in your .env file or environment variables.",
        )

    def run_pipeline_worker():
        try:
            log_event(
                f"Starting pipeline for search terms: {', '.join(body.terms)} in {', '.join(body.locations)}",
                category="Pipeline",
            )
            log_event(
                f"Target platforms: {', '.join(body.sites)}",
                category="Scraper",
            )
            run_application_pipeline(
                api_key=api_key,
                search_terms=body.terms,
                locations=body.locations,
                results_wanted=body.limit,
                sites=body.sites,
                is_remote=body.is_remote,
                region=body.region,
                countries=body.countries,
                emea_only=body.strict_region,
                auto_apply=body.auto_apply,
                autonomous=body.autonomous,
                headless=body.headless,
            )
            PIPELINE_STATE["status"] = "completed"
            log_event(
                "Pipeline run completed successfully!",
                level="SUCCESS",
                category="Pipeline",
            )
        except Exception as e:
            PIPELINE_STATE["status"] = "failed"
            PIPELINE_STATE["error"] = str(e)
            log_event(f"Pipeline error: {e}", level="ERROR", category="Pipeline")
        finally:
            with pipeline_lock:
                PIPELINE_STATE["is_running"] = False

    background_tasks.add_task(run_pipeline_worker)
    return {
        "status": "started",
        "message": f"Pipeline started for {len(body.terms)} search terms.",
    }


@app.get("/api/pipeline/status")
def get_pipeline_status() -> dict[str, Any]:
    """Returns live status and log output of the scraping/tailoring pipeline."""
    state = dict(PIPELINE_STATE)
    state["logs"] = get_recent_logs(150)
    return state


@app.post("/api/logs/clear")
def clear_system_logs() -> dict[str, str]:
    """Clears in-memory log buffer."""
    clear_logs()
    return {"status": "success", "message": "Console logs cleared successfully"}


# Mount compiled Angular single-page application if built
angular_browser_dist = project_root / "frontend" / "dist" / "frontend" / "browser"
if angular_browser_dist.exists() and (angular_browser_dist / "index.html").exists():
    app.mount(
        "/",
        StaticFiles(directory=str(angular_browser_dist), html=True),
        name="angular_app",
    )
