from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import Receive, Scope, Send

from job_applier.web.edge_auth import (
    EdgeAuthConfig,
    EdgeAuthError,
    EdgeAuthMiddleware,
    handle_viewer_websocket,
    validate_edge_auth_startup,
    verify_cf_access_jwt,
)

from job_applier.db import (
    dismiss_application_db,
    get_applications,
    get_connection,
    get_db_stats,
    init_db,
)

from job_applier.automation.autofill_script import (  # type: ignore[import-not-found]
    save_autofill_assets,
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

edge_config = EdgeAuthConfig.from_env()


class SafeStaticFiles(StaticFiles):
    """StaticFiles handler that gracefully rejects WebSocket scopes without crashing."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1000})
            return
        await super().__call__(scope, receive, send)


class CacheAwareStaticFiles(SafeStaticFiles):
    """Serve SPA HTML with revalidation and fingerprinted assets immutably."""

    _fingerprinted_asset = re.compile(r"^.+-[A-Za-z0-9]{8,}\.(?:js|css)$")

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        filename = Path(full_path).name
        if filename == "index.html" or response.media_type == "text/html":
            cache_control = "no-store, no-cache, must-revalidate, max-age=0"
        elif self._fingerprinted_asset.fullmatch(filename):
            cache_control = "public, max-age=31536000, immutable"
        else:
            cache_control = "no-cache"
        response.headers["Cache-Control"] = cache_control
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_edge_auth_startup(edge_config)
    yield


app = FastAPI(
    title="Job Applier Web Dashboard",
    description="Unified Web App for Scraping, AI Tailoring, and Automated Job Application",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(edge_config.allowed_origins),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(EdgeAuthMiddleware, config=edge_config)

project_root = get_project_root()
output_apps_dir = project_root / "output" / "applications"
output_applied_dir = project_root / "output" / "applied"
output_apps_dir.mkdir(parents=True, exist_ok=True)
output_applied_dir.mkdir(parents=True, exist_ok=True)

# Main resume viewer/generation state. The lock prevents concurrent requests from
# launching duplicate PDF generations; generation errors are intentionally generic.
main_resume_pdf = (project_root / "output" / "main_resume.pdf").resolve()
resume_generation_lock = threading.Lock()
resume_generation_state: dict[str, Any] = {
    "status": "idle",
    "generation_id": None,
    "error": None,
    "updated_at": None,
}

# Mount files directories
app.mount(
    "/files/applications",
    SafeStaticFiles(directory=str(output_apps_dir)),
    name="applications_files",
)
app.mount(
    "/files/applied",
    SafeStaticFiles(directory=str(output_applied_dir)),
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
    browser: str | None = None


class BatchApplyRequest(BaseModel):
    app_ids: list[str] | None = None
    count: int = 5
    mode: str = "assisted"
    headless: bool | None = None
    browser: str | None = None


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


class BatchRequeueRequest(BaseModel):
    items: list[RequeueRequest] = Field(default_factory=list)
    requeue_all: bool = False
    status_filter: str | None = None


class SubmitCodeRequest(BaseModel):
    code: str


class AuthLoginRequest(BaseModel):
    platform: str
    timeout: int = 180
    browser: str | None = None


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
def get_health() -> JSONResponse:
    """Returns a lightweight, non-sensitive liveness and health response for containers."""
    from job_applier.db import get_connection
    from job_applier.ops.disk_guard import check_disk_pressure

    try:
        conn = get_connection()
        try:
            conn.execute("SELECT 1;").fetchone()
        finally:
            conn.close()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "service": "job-applier",
                "reason": "database_error",
            },
        )

    status = check_disk_pressure()
    if status.is_under_pressure:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "service": "job-applier",
                "reason": "disk_pressure",
            },
        )

    return JSONResponse(
        status_code=200,
        content={"status": "ok", "service": "job-applier"},
    )


DASHBOARD_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _load_dashboard_html() -> str:
    """Load the compiled SPA, falling back to the dependency-free error page."""
    angular_dist = (
        project_root / "frontend" / "dist" / "frontend" / "browser"
    ).resolve()
    angular_index = angular_dist / "index.html"
    if str(angular_index).startswith(str(angular_dist)) and angular_index.is_file():
        try:
            return angular_index.read_text(encoding="utf-8")
        except OSError as err:
            print(f"Notice loading Angular index: {err}")

    template_path = Path(__file__).resolve().parent / "templates" / "index.html"
    if not template_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard template not found.")
    try:
        return template_path.read_text(encoding="utf-8")
    except OSError as err:
        raise HTTPException(
            status_code=500, detail=f"Error loading template: {err}"
        ) from err


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
def get_dashboard() -> HTMLResponse:
    """Render the dashboard with mandatory HTML revalidation headers."""
    return HTMLResponse(content=_load_dashboard_html(), headers=DASHBOARD_CACHE_HEADERS)


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
    """Returns aggregated pipeline and application metrics from the SQLite database."""
    db_stats = get_db_stats()
    tracker_stats = get_tracker_stats()

    pending_count = db_stats.get("pending", 0)
    applied_count = db_stats.get("applied", 0)

    # Fallback to counting disk folders if DB has 0 records
    if pending_count == 0 and output_apps_dir.exists():
        pending_count = sum(1 for d in output_apps_dir.iterdir() if d.is_dir())
    if applied_count == 0 and output_applied_dir.exists():
        applied_count = sum(1 for d in output_applied_dir.iterdir() if d.is_dir())

    return {
        "pending": pending_count,
        "applied_folders": applied_count,
        "applied": applied_count,
        "tracker": tracker_stats,
        "db": db_stats,
        "queue": db_stats.get("queue", {}),
        "pipeline": {
            "is_running": PIPELINE_STATE["is_running"],
            "status": PIPELINE_STATE["status"],
        },
    }


@app.get("/api/applications")
def list_applications(
    search: str | None = Query(None, description="Search keyword"),
    status: str | None = Query(
        None, description="Filter by status (e.g. pending, applied)"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    queue_only: bool = Query(
        True,
        description="Return only applications backed by an automation queue job",
    ),
) -> dict[str, Any]:
    """Lists queue-backed applications by default; set queue_only=false for legacy inventory."""
    db_res = get_applications(
        status=status,
        search=search,
        limit=limit,
        offset=offset,
        queue_only=queue_only,
    )
    total = db_res["total"]
    items = []

    for r in db_res["items"]:
        folder_name = r.get("folder_name") or r["id"]
        cv_name = r.get("cv_filename") or ""

        # Check if local folder has cover letter text
        cl_preview = ""
        app_folder = output_apps_dir / folder_name
        if not app_folder.exists():
            app_folder = output_applied_dir / folder_name

        if app_folder.exists():
            cl_file = app_folder / "cover_letter.txt"
            if cl_file.exists():
                try:
                    cl_preview = cl_file.read_text(encoding="utf-8")[:160] + "..."
                except Exception:
                    pass

        status_sub = "applications" if r.get("status") != "applied" else "applied"
        pdf_url = (
            f"/files/{status_sub}/{folder_name}/{cv_name}"
            if cv_name and cv_name != "None"
            else ""
        )

        items.append(
            {
                "id": r["id"],
                "company": r["company"],
                "title": r["title"],
                "job_url": r["job_url"],
                "platform": r.get("platform", "Generic"),
                "status": r.get("status", "pending"),
                "submission_type": r.get("submission_type", "manual"),
                "cv_filename": cv_name,
                "has_cover_letter": bool(r.get("has_cover_letter")),
                "cover_letter_preview": cl_preview,
                "pdf_url": pdf_url,
                "created_at": r.get("created_at", ""),
                "automation_state": r.get("automation_state"),
                "job_id": r.get("job_id"),
                "has_artifacts": bool(r.get("has_artifacts", True)),
            }
        )

    # Legacy inventory fallback is never mixed into the automation queue response.
    if not queue_only and total == 0 and output_apps_dir.exists():
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
        "scope": "queue" if queue_only else "legacy_inventory",
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


@app.post("/api/applications/{app_id}/apply", status_code=202)
def run_auto_apply_for_job(
    app_id: str,
    body: ApplyRequest,
    response: Response,
) -> dict[str, Any]:
    """Enqueues an application for the durable automation worker (returns 202 Accepted)."""
    from job_applier.automation.queue import enqueue_job
    from job_applier.tracker import detect_platform_from_url

    conn = get_connection()
    try:
        app_row = conn.execute(
            "SELECT * FROM applications WHERE id = ?;", (app_id,)
        ).fetchone()
    finally:
        conn.close()

    job_url = ""
    company = ""
    title = ""
    if app_row:
        job_url = app_row["job_url"]
        company = app_row["company"]
        title = app_row["title"]
    else:
        app_folder = get_safe_app_folder(app_id)
        info = parse_app_folder_info(app_folder)
        job_url = info["job_url"]
        company = info["company"]
        title = info["title"]

    if not job_url or not job_url.startswith("http"):
        raise HTTPException(status_code=400, detail="Invalid job URL")

    platform = detect_platform_from_url(job_url)
    job = enqueue_job(app_id=app_id, adapter=platform.lower(), priority=10)

    set_automation_hud(
        is_active=True,
        app_id=app_id,
        company=company,
        title=title,
        step="enqueued",
        message=f"Application for {company} queued for processing (Job ID: {job.id}).",
        progress_pct=10,
    )

    response.status_code = 202
    return {
        "status": "queued",
        "job_id": job.id,
        "app_id": app_id,
        "adapter": job.adapter,
        "state": job.state,
        "message": f"Successfully queued application for {company}.",
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
    """Persists an explicit queue dismissal and removes its generated folder when present."""
    if not dismiss_application_db(app_id):
        raise HTTPException(status_code=404, detail="Application not found")

    safe_name = Path(app_id).name
    app_folder = (output_apps_dir / safe_name).resolve()
    base_resolved = output_apps_dir.resolve()
    if str(app_folder).startswith(str(base_resolved)) and app_folder.is_dir():
        try:
            shutil.rmtree(str(app_folder))
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Application dismissed but folder cleanup failed: {e}",
            ) from e

    return {
        "status": "success",
        "message": "Application dismissed and removed from queue",
    }


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
    """Returns real-time browser automation HUD monitoring status, queue metrics, and runtime controls."""
    from job_applier.automation.browser_automator import get_active_automator
    from job_applier.automation.queue import (
        get_active_job,
        get_browser_owner_job,
        get_latest_job,
        get_queue_stats,
        get_runtime_control,
    )

    with automation_lock:
        state = dict(AUTOMATION_STATE)

    ctrl = get_runtime_control()
    state["is_paused"] = bool(ctrl.get("is_paused"))
    state["is_stopped"] = bool(ctrl.get("is_stopped"))
    state["browser_active"] = bool(ctrl.get("browser_active"))
    state["browser_is_closed"] = bool(ctrl.get("browser_is_closed", True))
    state["queue"] = get_queue_stats()
    automator = get_active_automator()
    if (automator and automator.waiting_for_code) or bool(
        ctrl.get("is_waiting_for_code")
    ):
        state["is_waiting_for_code"] = True
        state["step"] = "verification_code_required"
        state["message"] = (
            "🔑 Security verification code required! Check your email or SMS and submit the code below."
        )
    else:
        state["is_waiting_for_code"] = False

    browser_owner_job = get_browser_owner_job()
    active_job = browser_owner_job or get_active_job()
    state["active_job"] = active_job
    state["browser_job_id"] = (
        browser_owner_job.get("id") if browser_owner_job else ctrl.get("browser_job_id")
    )

    target_job = active_job or get_latest_job()
    if target_job:
        job_state = target_job.get("state", "claimed")
        comp = target_job.get("company", "") or target_job.get("app_id", "")
        title = target_job.get("title", "")
        err_msg = target_job.get("error_message", "")

        state["app_id"] = target_job.get("app_id", "")
        state["company"] = comp
        state["title"] = title

        state_progress_map = {
            "ready": (
                10,
                "enqueued",
                f"Application for {comp} queued for processing (Job ID: {target_job.get('id')}).",
            ),
            "claimed": (20, "claimed", f"Worker claimed application for {comp}."),
            "navigating": (
                40,
                "navigating",
                f"Navigating to {comp} application page...",
            ),
            "filling": (60, "filling", f"Filling {comp} application form fields..."),
            "validating": (
                80,
                "validating",
                f"Validating {comp} fields and documents...",
            ),
            "submit_intent": (
                90,
                "submitting",
                f"Submitting application for {comp}...",
            ),
            "verifying": (
                95,
                "verifying",
                f"Verifying {comp} submission confirmation...",
            ),
            "applied": (
                100,
                "applied",
                f"Application for {comp} successfully verified and submitted!",
            ),
            "auth_required": (
                50,
                "auth_required",
                "Authentication required. Open Browser View and log in; automation remains paused.",
            ),
            "mfa_required": (
                50,
                "mfa_required",
                "MFA code required - operator takeover needed.",
            ),
            "captcha_required": (
                50,
                "captcha_required",
                "CAPTCHA detected - operator takeover needed.",
            ),
            "unknown_question": (
                70,
                "unknown_question",
                "Novel screening question requires review.",
            ),
            "ambiguous_submission": (
                90,
                "ambiguous_submission",
                f"Submission outcome uncertain for {comp}. Automatic retry paused.",
            ),
            "site_changed": (
                0,
                "failed",
                f"Failed: {err_msg or 'Application site or artifacts changed.'}",
            ),
            "failed_permanent": (
                0,
                "failed",
                f"Failed: {err_msg or 'Application submission rejected.'}",
            ),
            "retry_wait": (
                20,
                "retry_wait",
                f"Waiting to retry application for {comp}.",
            ),
            "cancelled": (
                0,
                "cancelled",
                f"Job cancelled: {err_msg or 'User requested.'}",
            ),
        }

        if active_job:
            state["is_active"] = True
        elif job_state in ("applied", "failed_permanent", "site_changed", "cancelled"):
            state["is_active"] = False

        if job_state in state_progress_map:
            pct, s_step, s_msg = state_progress_map[job_state]
            state["progress_pct"] = pct
            state["step"] = s_step
            state["message"] = s_msg
    elif not state.get("is_active"):
        state["progress_pct"] = 0

    return state


class ResolveJobRequest(BaseModel):
    resolution_type: str = "continue"  # 'answer' or 'continue'
    answer_value: str = ""
    question_key: str = ""
    approved_scope: str = "global"
    force: bool = False


class AckAllNotificationsRequest(BaseModel):
    up_to_id: int | None = None


class TakeoverClaimRequest(BaseModel):
    owner: str = "operator"
    lease_seconds: int = 300


class TakeoverReleaseRequest(BaseModel):
    owner: str = "operator"
    force: bool = False


@app.get("/api/notifications")
def get_notifications_endpoint(
    after: str | None = Query(
        None, description="Notification ID or integer ID to fetch after"
    ),
    unread_only: bool = Query(
        False, description="Filter for unacknowledged notifications only"
    ),
    limit: int = Query(50, ge=1, le=200, description="Max notifications to return"),
) -> dict[str, Any]:
    """
    Retrieves durable notifications with monotonic ordering, unread filtering,
    and aggregate stats.
    """
    from job_applier.automation.queue import get_notification_stats, get_notifications

    notifications = get_notifications(
        after_id=after, unread_only=unread_only, limit=limit
    )
    stats = get_notification_stats()
    return {
        "status": "success",
        "notifications": notifications,
        "unread_count": stats["unread_count"],
        "total": stats["total"],
        "latest_id": stats["latest_id"],
    }


@app.post("/api/notifications/{notification_id}/ack")
def ack_notification_endpoint(notification_id: str) -> dict[str, Any]:
    """
    Idempotently acknowledges a notification.
    Distinct from job resolution/resumption: acknowledging never resumes automation.
    """
    from job_applier.automation.queue import ack_notification

    result = ack_notification(notification_id)
    if result.get("status") == "not_found":
        raise HTTPException(
            status_code=404,
            detail=f"Notification {notification_id} not found.",
        )
    return result


@app.post("/api/notifications/ack-all")
def ack_all_notifications_endpoint(
    body: AckAllNotificationsRequest | None = None,
) -> dict[str, Any]:
    """Idempotently acknowledges all unacknowledged notifications."""
    from job_applier.automation.queue import ack_all_notifications

    up_to_id = body.up_to_id if body else None
    count = ack_all_notifications(up_to_id=up_to_id)
    return {
        "status": "success",
        "acknowledged_count": count,
        "message": f"Acknowledged {count} notifications.",
    }


@app.delete("/api/notifications/{notification_id}")
def delete_notification_endpoint(notification_id: str) -> dict[str, Any]:
    """Deletes a single notification."""
    from job_applier.automation.queue import delete_notification

    deleted = delete_notification(notification_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Notification {notification_id} not found.",
        )
    return {
        "status": "success",
        "notification_id": notification_id,
        "message": f"Notification {notification_id} deleted.",
    }


@app.delete("/api/notifications")
@app.post("/api/notifications/clear")
def clear_all_notifications_endpoint() -> dict[str, Any]:
    """Clears all notifications."""
    from job_applier.automation.queue import clear_all_notifications

    count = clear_all_notifications()
    return {
        "status": "success",
        "cleared_count": count,
        "message": f"Cleared {count} notifications.",
    }


@app.post("/api/notifications/dispatch")
def dispatch_notifications_endpoint() -> dict[str, Any]:
    """Triggers delivery of pending notifications from outbox to mobile/ntfy push channel."""
    from job_applier.automation.ntfy import process_outbox

    counts = process_outbox()
    return {
        "status": "success",
        "counts": counts,
    }


@app.get("/api/automation/takeover/status")
def takeover_status_endpoint() -> dict[str, Any]:
    """Returns manual takeover status, active lease owner, expiration, and read-only enforcement status."""
    from job_applier.automation.queue import get_runtime_control, is_takeover_active

    is_active, owner, expires_at = is_takeover_active()
    ctrl = get_runtime_control()
    return {
        "status": "success",
        "is_takeover_active": is_active,
        "owner": owner if is_active else None,
        "expires_at": expires_at if is_active else None,
        "is_paused": ctrl.get("is_paused", False),
        "is_stopped": ctrl.get("is_stopped", False),
        "read_only": not is_active,  # Server-enforced read-only default
    }


@app.post("/api/automation/takeover/claim")
def claim_takeover_endpoint(body: TakeoverClaimRequest) -> dict[str, Any]:
    """
    Claims exclusive operator takeover lease.
    Atomically pauses the worker and enables write/input transport.
    """
    from job_applier.automation.queue import claim_manual_takeover

    result = claim_manual_takeover(owner=body.owner, lease_seconds=body.lease_seconds)
    if result.get("status") == "conflict":
        raise HTTPException(
            status_code=409,
            detail=result.get(
                "message", "Takeover is currently active by another user."
            ),
        )
    return result


@app.post("/api/automation/takeover/release")
def release_takeover_endpoint(body: TakeoverReleaseRequest) -> dict[str, Any]:
    """
    Releases operator takeover lease.
    Worker remains paused awaiting safe resume revalidation.
    """
    from job_applier.automation.queue import release_manual_takeover

    result = release_manual_takeover(owner=body.owner, force=body.force)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@app.post("/api/automation/takeover/reopen-auth")
def reopen_auth_session_endpoint(job_id: str | None = None) -> dict[str, Any]:
    """Reopens an auth-paused job in a fresh worker-controlled browser session."""
    from job_applier.automation.queue import requeue_auth_required_job

    result = requeue_auth_required_job(job_id=job_id)
    if result.get("status") == "rejected":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@app.post("/api/automation/takeover/resume")
def resume_from_takeover_endpoint(job_id: str | None = None) -> dict[str, Any]:
    """
    Performs safe resume revalidation (domain check, completion check, challenge check)
    before clearing takeover and unpausing automation.
    """
    from job_applier.automation.browser_automator import get_active_automator
    from job_applier.automation.safe_resume import safe_resume_revalidate

    automator = get_active_automator()
    result = safe_resume_revalidate(job_id=job_id, automator=automator)
    if result.get("status") == "rejected":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@app.post("/api/automation/pause")
def pause_automation_endpoint() -> dict[str, Any]:
    """Sets global automation pause."""
    from job_applier.automation.queue import set_runtime_pause

    set_runtime_pause(True)
    return {"status": "success", "is_paused": True, "message": "Automation paused."}


@app.post("/api/automation/resume")
def resume_automation_endpoint() -> dict[str, Any]:
    """Resumes global automation after safe resume revalidation."""
    from job_applier.automation.browser_automator import get_active_automator
    from job_applier.automation.safe_resume import safe_resume_revalidate
    from job_applier.ops.emergency_stop import clear_emergency_stop

    automator = get_active_automator()
    result = safe_resume_revalidate(automator=automator)
    if result.get("status") == "rejected":
        raise HTTPException(status_code=400, detail=result.get("message"))

    clear_emergency_stop(reason="Operator resumed automation via dashboard")
    return {
        "status": "success",
        "is_paused": False,
        "is_stopped": False,
        "message": result.get("message", "Automation resumed."),
        "action": result.get("action", "resumed"),
    }


@app.post("/api/automation/stop")
def stop_automation_endpoint() -> dict[str, Any]:
    """Engages emergency stop for automation workers, revoking leases and emitting alert."""
    from job_applier.ops.emergency_stop import emergency_stop

    res = emergency_stop(reason="Emergency stop triggered from web dashboard")
    return {
        "status": "success",
        "is_stopped": True,
        "revoked_leases_count": res.get("revoked_leases_count", 0),
        "message": res.get("message", "Automation emergency stop engaged."),
    }


@app.post("/api/automation/clear-state")
def clear_automation_state_endpoint() -> dict[str, Any]:
    """Fully clears automation state, resets stuck leases/jobs back to ready, and closes orphaned browser sessions."""
    from job_applier.automation.queue import clear_automation_state

    res = clear_automation_state()
    return res


@app.post("/api/automation/jobs/{job_id}/cancel")
def cancel_job_endpoint(job_id: str) -> dict[str, Any]:
    """Cancels a specific automation job."""
    from job_applier.automation.queue import cancel_job

    success = cancel_job(job_id, reason="Cancelled from web dashboard")
    if not success:
        raise HTTPException(
            status_code=404, detail=f"Job {job_id} not found or already terminal"
        )
    return {
        "status": "success",
        "job_id": job_id,
        "message": f"Job {job_id} cancelled.",
    }


@app.post("/api/automation/jobs/{job_id}/skip")
def skip_job_endpoint(job_id: str) -> dict[str, Any]:
    """Skips a specific automation job."""
    from job_applier.automation.queue import skip_job

    success = skip_job(job_id, reason="Skipped from web dashboard")
    if not success:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return {
        "status": "success",
        "job_id": job_id,
        "message": f"Job {job_id} marked as skipped.",
    }


@app.post("/api/automation/jobs/{job_id}/resolve")
def resolve_job_endpoint(job_id: str, body: ResolveJobRequest) -> dict[str, Any]:
    """Resolves an exception (e.g. provides approved screening answer or confirms manual login) and resumes job."""
    from job_applier.automation.queue import resolve_job

    try:
        success = resolve_job(
            job_id=job_id,
            resolution_type=body.resolution_type,
            answer_value=body.answer_value,
            question_key=body.question_key,
            approved_scope=body.approved_scope,
            force=body.force,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not success:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return {
        "status": "success",
        "job_id": job_id,
        "message": f"Job {job_id} resolved and requeued.",
    }


@app.get("/api/automation/applications/{app_id}/status")
def application_automation_status_endpoint(
    app_id: str,
    job_id: str | None = Query(None),
) -> dict[str, Any]:
    """Returns durable, sanitized automation status for an application."""
    from job_applier.automation.queue import get_application_automation_status

    return {
        "app_id": app_id,
        "jobs": get_application_automation_status(app_id, job_id=job_id),
    }


@app.get("/api/automation/applications/{app_id}/events")
def application_automation_events_endpoint(
    app_id: str,
    job_id: str | None = Query(None),
    after: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> dict[str, Any]:
    """Returns bounded, ordered, sanitized event history for an application."""
    from job_applier.automation.queue import get_application_automation_events

    events = get_application_automation_events(
        app_id, job_id=job_id, after_id=after, limit=limit
    )
    return {
        "app_id": app_id,
        "events": events,
        "next_after": events[-1]["id"] if events else after,
        "has_more": len(events) == limit,
    }


@app.get("/api/automation/funnel")
def automation_funnel_endpoint() -> dict[str, Any]:
    """Returns accurate durable automation funnel metrics, separate from artifacts."""
    from job_applier.automation.queue import get_automation_funnel

    return get_automation_funnel()


@app.get("/api/automation/events")
async def stream_automation_events(
    request: Request,
    after: int | None = Query(None, description="Event ID to resume streaming from"),
    replay: bool = Query(False, description="Replay historical events from beginning"),
) -> StreamingResponse:
    """Streams server-sent events (SSE) for real-time automation progress and audit replay."""
    import asyncio
    from job_applier.automation.queue import (
        get_events,
        get_latest_event_id,
        get_queue_stats,
    )

    header_last_id = request.headers.get("Last-Event-ID")
    if after is not None:
        cursor = after
    elif header_last_id and header_last_id.isdigit():
        cursor = int(header_last_id)
    elif replay:
        cursor = 0
    else:
        cursor = None

    user = getattr(request.state, "user", None)
    if user and isinstance(user, dict) and "exp" in user:
        token_exp = float(user["exp"])
    elif edge_config.cf_access_enabled:
        token_exp = time.time() + 3600.0  # Fallback for authenticated edge session
    else:
        token_exp = None  # Local / tokenless SSE sessions do not expire after 300s

    async def event_generator():
        nonlocal cursor
        latest_id = get_latest_event_id()
        if cursor is None:
            cursor = latest_id
        from job_applier.automation.queue import get_notification_stats

        notif_stats = get_notification_stats()
        snapshot_data = {
            "queue": get_queue_stats(),
            "latest_event_id": latest_id,
            "unread_notifications": notif_stats["unread_count"],
            "notifications_latest_id": notif_stats["latest_id"],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        yield f"id: {latest_id}\nevent: snapshot\ndata: {json.dumps(snapshot_data)}\n\n"

        while True:
            if await request.is_disconnected():
                break

            if token_exp is not None and time.time() >= token_exp:
                yield f"id: {cursor}\nevent: expired\ndata: {json.dumps({'message': 'Session expired, reauthentication required'})}\n\n"
                break

            new_events = get_events(after_id=cursor, limit=50)
            if new_events:
                for ev in new_events:
                    ev_id = ev["id"]
                    ev_type = ev.get("event_type", "message")
                    data_str = json.dumps(ev)
                    yield f"id: {ev_id}\nevent: {ev_type}\ndata: {data_str}\n\n"
                    cursor = max(cursor, ev_id)
            else:
                yield ": keep-alive\n\n"

            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.api_route("/api/auth/viewer-gate", methods=["GET", "HEAD"])
def viewer_gate_endpoint(request: Request) -> Response:
    """
    Gateway authorization endpoint for noVNC HTTP and WebSocket upgrade requests.
    Validates Cloudflare Access assertion, exact identity allowlist, and Origin header.
    Returns HTTP 200 with X-Viewer-Expires-In header if authorized, or 401/403 if rejected.
    """
    token_exp: float | None = None
    if edge_config.cf_access_enabled:
        token = request.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            token = request.cookies.get("CF_Authorization")
        if not token:
            return JSONResponse(
                {"detail": "Missing Cloudflare Access assertion"},
                status_code=401,
            )
        try:
            payload = verify_cf_access_jwt(token, edge_config)
            token_exp = payload["exp"]
        except EdgeAuthError as e:
            return JSONResponse({"detail": e.message}, status_code=e.status_code)
        except Exception as e:
            return JSONResponse({"detail": f"Auth error: {e}"}, status_code=401)
    else:
        token_exp = time.time() + float(edge_config.viewer_max_duration_seconds)

    # Origin verification for WebSocket upgrade or sensitive viewer access
    is_ws_upgrade = request.headers.get("Upgrade", "").lower() == "websocket"
    origin = request.headers.get("origin")
    allowed_origins = edge_config.allowed_origins
    if is_ws_upgrade and not origin:
        return JSONResponse(
            {"detail": "Missing Origin header for WebSocket upgrade"},
            status_code=403,
        )
    if origin:
        norm_origin = origin.rstrip("/").lower()
        if norm_origin not in allowed_origins:
            return JSONResponse(
                {"detail": "Cross-origin viewer access forbidden"},
                status_code=403,
            )

    now = time.time()
    time_to_exp = (
        max(0, int(token_exp - now))
        if token_exp
        else edge_config.viewer_max_duration_seconds
    )
    viewer_lifetime = min(edge_config.viewer_max_duration_seconds, time_to_exp)

    return JSONResponse(
        {
            "status": "authorized",
            "is_ws_upgrade": is_ws_upgrade,
            "expires_in": viewer_lifetime,
        },
        status_code=200,
        headers={
            "X-Viewer-Expires-In": str(viewer_lifetime),
            "Cache-Control": "no-store, no-cache",
        },
    )


@app.websocket("/api/auth/viewer-gate")
async def viewer_gate_ws_route(websocket: WebSocket) -> None:
    """Gracefully closes any direct WebSocket handshake to the HTTP viewer-gate endpoint."""
    await websocket.close(code=1000)


@app.websocket("/browser/websockify")
@app.websocket("/websockify")
@app.websocket("/api/browser/ws")
async def viewer_websocket_route(websocket: WebSocket) -> None:
    """
    Proxies viewer WebSocket to runtime websockify (VNC) while strictly enforcing
    server-side 5-minute / token-expiry lifetime.
    """
    await handle_viewer_websocket(websocket, edge_config)


@app.post("/api/automation/submit-code")
def submit_verification_code(body: SubmitCodeRequest) -> dict[str, Any]:
    """Submits a 2FA or email verification code to the active browser automation session."""
    from job_applier.automation.browser_automator import get_active_automator
    from job_applier.automation.queue import (
        get_runtime_control,
        set_pending_verification_code,
    )

    automator = get_active_automator()
    ctrl = get_runtime_control()
    is_waiting = (
        automator is not None and getattr(automator, "waiting_for_code", False)
    ) or bool(ctrl.get("is_waiting_for_code"))
    if not is_waiting and not automator:
        raise HTTPException(
            status_code=400,
            detail="No active browser session is currently waiting for a verification code.",
        )

    if automator and hasattr(automator, "supply_verification_code"):
        automator.supply_verification_code(body.code)
    set_pending_verification_code(body.code)
    return {
        "status": "success",
        "message": f"Verification code '{body.code}' submitted to browser.",
    }


@app.get("/api/auth/status")
def get_auth_status(browser: str | None = None) -> dict[str, Any]:
    """Returns platform authentication status for LinkedIn, BestJobs, eJobs, and Google."""
    from job_applier.automation.auth_manager import AuthManager

    manager = AuthManager(browser=browser)
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

    manager = AuthManager(browser=body.browser)
    engine_name = manager.engine.capitalize()

    def run_login_worker():
        manager.launch_interactive_login(
            platform=platform,
            timeout_seconds=body.timeout,
            browser=body.browser,
        )

    background_tasks.add_task(run_login_worker)
    return {
        "status": "started",
        "platform": platform,
        "browser": manager.engine,
        "message": f"Launched login window for {platform.upper()} using {engine_name}. Please complete sign-in in the browser window.",
    }


@app.post("/api/auth/sync-chrome")
def sync_chrome_cookies() -> dict[str, Any]:
    """Syncs existing authenticated sessions from desktop Chrome into the persistent profile."""
    from job_applier.automation.auth_manager import AuthManager

    manager = AuthManager(browser="chrome")
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


@app.post("/api/batch-apply", status_code=202)
def batch_apply(body: BatchApplyRequest, response: Response) -> dict[str, Any]:
    """Enqueues multiple applications into the durable automation queue (returns 202 Accepted)."""
    from job_applier.automation.queue import enqueue_job
    from job_applier.tracker import detect_platform_from_url

    conn = get_connection()
    try:
        if body.app_ids:
            placeholders = ",".join("?" for _ in body.app_ids)
            rows = conn.execute(
                f"SELECT * FROM applications WHERE id IN ({placeholders});",
                body.app_ids,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM applications WHERE status = 'pending' ORDER BY updated_at DESC LIMIT ?;",
                (body.count,),
            ).fetchall()
    finally:
        conn.close()

    enqueued_jobs = []
    for r in rows:
        if r["job_url"] and r["job_url"].startswith("http"):
            platform = detect_platform_from_url(r["job_url"])
            j = enqueue_job(app_id=r["id"], adapter=platform.lower(), priority=5)
            enqueued_jobs.append(j)

    if not enqueued_jobs and not body.app_ids:
        # Fallback to output_apps_dir if DB had 0 records
        if output_apps_dir.exists():
            folders = sorted([d for d in output_apps_dir.iterdir() if d.is_dir()])[
                : body.count
            ]
            for f in folders:
                info = parse_app_folder_info(f)
                if info["job_url"].startswith("http"):
                    j = enqueue_job(
                        app_id=f.name,
                        adapter=detect_platform_from_url(info["job_url"]).lower(),
                        priority=5,
                    )
                    enqueued_jobs.append(j)

    if not enqueued_jobs:
        raise HTTPException(
            status_code=400, detail="No valid applications found for batch apply"
        )

    response.status_code = 202
    return {
        "status": "queued",
        "queued_count": len(enqueued_jobs),
        "job_ids": [j.id for j in enqueued_jobs],
        "message": f"Successfully queued {len(enqueued_jobs)} applications for processing.",
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


def _requeue_single_item(
    job_url: str,
    folder_name: str,
    output_apps: Path,
    output_applied: Path,
) -> dict[str, Any]:
    """Moves an application to applications/, sets SQLite status to pending, and enqueues to automation_jobs."""
    from job_applier.automation.queue import enqueue_job
    from job_applier.tracker import detect_platform_from_url, record_application

    init_db()
    conn = get_connection()
    row = None
    try:
        if job_url:
            row = conn.execute(
                "SELECT * FROM applications WHERE job_url = ?;", (job_url.strip(),)
            ).fetchone()
        if not row and folder_name:
            clean_name = Path(folder_name).name
            row = conn.execute(
                "SELECT * FROM applications WHERE folder_name = ? OR id = ?;",
                (clean_name, clean_name),
            ).fetchone()
    finally:
        conn.close()

    # Determine candidate folder in output/applied/
    found_applied_folder: Path | None = None
    target_folder_name = folder_name or (row["folder_name"] if row else "")

    if target_folder_name:
        safe_name = Path(target_folder_name).name
        cand = (output_applied / safe_name).resolve()
        if (
            str(cand).startswith(str(output_applied.resolve()))
            and cand.exists()
            and cand.is_dir()
        ):
            found_applied_folder = cand

    if found_applied_folder is None and job_url:
        for folder in output_applied.iterdir():
            if not folder.is_dir():
                continue
            apply_file = folder / "APPLY_HERE.txt"
            if apply_file.exists():
                try:
                    with open(apply_file, encoding="utf-8") as f:
                        if f.read().strip() == job_url.strip():
                            found_applied_folder = folder
                            break
                except OSError:
                    continue

    moved_folder_name = ""
    if found_applied_folder:
        target_dir = output_apps / found_applied_folder.name
        try:
            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            shutil.move(str(found_applied_folder), str(target_dir))
            moved_folder_name = found_applied_folder.name
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to move folder back to queue: {e}"
            ) from e

    # Determine metadata
    app_id = ""
    company = ""
    title = ""
    final_job_url = job_url.strip()
    platform = "Generic"

    if row:
        app_id = row["id"]
        company = row["company"] or ""
        title = row["title"] or ""
        final_job_url = row["job_url"] or final_job_url
        platform = row["platform"] or platform
        target_folder_name = row["folder_name"] or app_id
    else:
        app_id = moved_folder_name or (Path(folder_name).name if folder_name else "")
        if not app_id and final_job_url:
            app_id = f"job_{abs(hash(final_job_url))}"

    if final_job_url and (not platform or platform == "Generic"):
        platform = detect_platform_from_url(final_job_url)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Update SQLite applications record (status='pending')
    conn = get_connection()
    try:
        with conn:
            if row:
                conn.execute(
                    "UPDATE applications SET status = 'pending', updated_at = ? WHERE id = ?;",
                    (now, app_id),
                )
            elif app_id:
                conn.execute(
                    """
                    INSERT INTO applications (
                        id, company, title, job_url, platform, status,
                        submission_type, folder_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 'manual', ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status = 'pending',
                        updated_at = excluded.updated_at;
                    """,
                    (
                        app_id,
                        company,
                        title,
                        final_job_url,
                        platform,
                        target_folder_name or app_id,
                        now,
                        now,
                    ),
                )
    finally:
        conn.close()

    # Update applications_tracker.csv
    try:
        record_application(
            company=company,
            title=title,
            job_url=final_job_url,
            status="pending",
            platform=platform,
            notes="Sent back to queue from tracker",
        )
    except Exception as e:
        print(f"Warning: Failed to update tracker CSV on requeue: {e}")

    # Enroll in automation_jobs
    job_id = None
    if app_id:
        try:
            job = enqueue_job(
                app_id=app_id,
                adapter=platform.lower() if platform else "generic",
                priority=5,
            )
            job_id = job.id

            # Ensure job is set to 'ready' if it was in an exception or inactive state
            if job.state not in (
                "ready",
                "claimed",
                "navigating",
                "filling",
                "validating",
                "submit_intent",
                "verifying",
            ):
                conn = get_connection()
                try:
                    with conn:
                        conn.execute(
                            """
                            UPDATE automation_jobs
                            SET state = 'ready', attempt_count = 0, is_cancelled = 0,
                                error_code = NULL, error_message = NULL, updated_at = ?
                            WHERE id = ?;
                            """,
                            (now, job.id),
                        )
                finally:
                    conn.close()
        except Exception as e:
            print(f"Warning: Could not enqueue job for {app_id}: {e}")

    return {
        "app_id": app_id,
        "job_id": job_id,
        "job_url": final_job_url,
        "folder_name": moved_folder_name or target_folder_name,
    }


@app.post("/api/tracker/requeue")
def requeue_application(body: RequeueRequest) -> dict[str, Any]:
    """Moves an application from output/applied back to output/applications, syncs SQLite & CSV, and enqueues to automation_jobs."""
    if not body.job_url and not body.folder_name:
        raise HTTPException(
            status_code=400, detail="Must provide job_url or folder_name"
        )
    res = _requeue_single_item(
        job_url=body.job_url,
        folder_name=body.folder_name,
        output_apps=output_apps_dir,
        output_applied=output_applied_dir,
    )
    return {
        "status": "success",
        "message": f"Successfully queued {res['app_id'] or res['job_url']} for processing!",
        "app_id": res["app_id"],
        "job_id": res["job_id"],
    }


@app.post("/api/tracker/batch-requeue")
def batch_requeue_applications(body: BatchRequeueRequest) -> dict[str, Any]:
    """Batch requeues multiple applications, moves applied folders, updates tracker & SQLite, and enqueues to automation_jobs."""
    items_to_process: list[tuple[str, str]] = []

    if body.requeue_all:
        init_db()
        conn = get_connection()
        seen_urls: set[str] = set()
        try:
            if body.status_filter and body.status_filter.lower() != "all":
                rows = conn.execute(
                    "SELECT job_url, folder_name FROM applications WHERE LOWER(status) = LOWER(?);",
                    (body.status_filter.strip(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT job_url, folder_name FROM applications;"
                ).fetchall()
            for r in rows:
                url = r["job_url"] or ""
                fname = r["folder_name"] or ""
                if url:
                    seen_urls.add(url)
                items_to_process.append((url, fname))
        finally:
            conn.close()

        # Check tracker df for any additional records
        try:
            from job_applier.tracker import load_tracker

            df = load_tracker()
            if not df.empty:
                if body.status_filter and body.status_filter.lower() != "all":
                    df = df[
                        df["status"].astype(str).str.lower()
                        == body.status_filter.strip().lower()
                    ]
                for _, row_item in df.iterrows():
                    u = str(row_item.get("job_url", "")).strip()
                    if u and u not in seen_urls:
                        items_to_process.append((u, ""))
                        seen_urls.add(u)
        except Exception as e:
            print(f"Warning: Could not read tracker for requeue: {e}")
    else:
        for item in body.items:
            if item.job_url or item.folder_name:
                items_to_process.append((item.job_url, item.folder_name))

    enqueued: list[dict[str, Any]] = []
    for j_url, f_name in items_to_process:
        try:
            item_res = _requeue_single_item(
                job_url=j_url,
                folder_name=f_name,
                output_apps=output_apps_dir,
                output_applied=output_applied_dir,
            )
            enqueued.append(item_res)
        except Exception as exc:
            print(f"Failed to requeue item {j_url or f_name}: {exc}")

    return {
        "success": True,
        "count": len(enqueued),
        "enqueued": enqueued,
    }


MAIN_RESUME_CONTACT_FIELDS = (
    "name",
    "phone",
    "email",
    "linkedin",
    "github",
    "location",
    "languages",
)


def _load_main_resume_data() -> tuple[Path, dict[str, Any]]:
    """Loads the private main resume and exposes only its supported resume schema."""
    resume_path = (project_root / "data" / "master_resume.json").resolve()
    data_root = (project_root / "data").resolve()
    if (
        not str(resume_path).startswith(f"{data_root}{os.sep}")
        or not resume_path.is_file()
    ):
        raise HTTPException(status_code=404, detail="Main resume is not available")
    try:
        with resume_path.open(encoding="utf-8") as resume_file:
            source = json.load(resume_file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Notice: failed to load main resume ({type(exc).__name__})")
        raise HTTPException(
            status_code=500, detail="Main resume could not be loaded"
        ) from exc
    if not isinstance(source, dict):
        raise HTTPException(status_code=500, detail="Main resume has an invalid format")

    def text(value: Any) -> str:
        return value if isinstance(value, str) else ""

    contact_value = source.get("contact")
    contact_source: dict[str, Any] = (
        contact_value if isinstance(contact_value, dict) else {}
    )
    contact = {
        key: text(contact_source.get(key))
        for key in MAIN_RESUME_CONTACT_FIELDS
        if text(contact_source.get(key))
    }
    experience = []
    for item in source.get("experience", []):
        if not isinstance(item, dict):
            continue
        experience.append(
            {
                "role": text(item.get("role")),
                "company": text(item.get("company")),
                "dates": text(item.get("dates")),
                "details": [
                    text(detail)
                    for detail in item.get("details", [])
                    if isinstance(detail, str)
                ],
            }
        )
    education_value = source.get("education")
    education_source: dict[str, Any] = (
        education_value if isinstance(education_value, dict) else {}
    )
    education = {
        key: text(education_source.get(key))
        for key in ("institution", "degree", "details")
        if text(education_source.get(key))
    }
    projects = []
    for item in source.get("projects", []):
        if not isinstance(item, dict):
            continue
        projects.append(
            {
                "name": text(item.get("name")),
                "description": text(item.get("description")),
                "url": text(item.get("url")),
            }
        )
    safe_data: dict[str, Any] = {
        "contact": contact,
        "summary": text(source.get("summary")),
        "experience": experience,
        "skills": [
            text(skill) for skill in source.get("skills", []) if isinstance(skill, str)
        ],
        "education": education,
        "projects": projects,
    }
    return resume_path, safe_data


def _resume_pdf_is_current(resume_path: Path) -> bool:
    try:
        return (
            main_resume_pdf.is_file()
            and main_resume_pdf.stat().st_mtime_ns >= resume_path.stat().st_mtime_ns
        )
    except OSError:
        return False


def _generate_main_resume_pdf(generation_id: str) -> None:
    """Generates the main resume PDF atomically without logging resume contents."""
    temp_path = main_resume_pdf.with_name(f".main_resume.{generation_id}.tmp.pdf")
    try:
        resume_path, resume_data = _load_main_resume_data()
        main_resume_pdf.parent.mkdir(parents=True, exist_ok=True)
        from job_applier.resume.resume import generate_resume

        generate_resume(resume_data, str(temp_path))
        if not temp_path.is_file() or temp_path.stat().st_size == 0:
            raise RuntimeError("generated PDF is empty")
        os.replace(temp_path, main_resume_pdf)
        with resume_generation_lock:
            resume_generation_state.update(
                {
                    "status": "ready",
                    "generation_id": generation_id,
                    "error": None,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
    except Exception as exc:
        print(f"Notice: main resume generation failed ({type(exc).__name__})")
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        with resume_generation_lock:
            resume_generation_state.update(
                {
                    "status": "error",
                    "generation_id": generation_id,
                    "error": "Resume generation failed. Retry.",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
            )


def _schedule_main_resume_generation(
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    with resume_generation_lock:
        if resume_generation_state.get("status") != "generating":
            resume_generation_state.update(
                {
                    "status": "generating",
                    "generation_id": uuid.uuid4().hex,
                    "error": None,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            background_tasks.add_task(
                _generate_main_resume_pdf,
                resume_generation_state["generation_id"],
            )
        return dict(resume_generation_state)


@app.get("/api/resume/main")
def get_main_resume(background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Returns a sanitized main resume and starts locked PDF generation when needed."""
    resume_path, resume_data = _load_main_resume_data()
    current = _resume_pdf_is_current(resume_path)
    has_artifact = main_resume_pdf.is_file()
    with resume_generation_lock:
        state = dict(resume_generation_state)
    if not current:
        state = _schedule_main_resume_generation(background_tasks)
    status = (
        "ready"
        if current
        else ("stale" if has_artifact else state.get("status", "generating"))
    )
    return {
        "status": status,
        "resume": resume_data,
        "artifact_url": "/api/resume/main.pdf" if current else None,
        "download_url": "/api/resume/main.pdf?download=true" if current else None,
        "generation_id": state.get("generation_id"),
        "updated_at": state.get("updated_at"),
        "error": state.get("error"),
        "retry_after_seconds": 2 if status in {"generating", "stale"} else None,
    }


@app.get("/api/resume/main.pdf")
def get_main_resume_pdf(download: bool = False) -> FileResponse:
    """Serves the generated main resume PDF only after a current artifact exists."""
    resume_path, _ = _load_main_resume_data()
    if not _resume_pdf_is_current(resume_path):
        raise HTTPException(status_code=404, detail="Resume PDF is not ready")
    disposition = "attachment" if download else "inline"
    return FileResponse(
        main_resume_pdf,
        media_type="application/pdf",
        filename="main-resume.pdf",
        content_disposition_type=disposition,
        headers=DASHBOARD_CACHE_HEADERS,
    )


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
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        load_dotenv(project_root / ".env")
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()

    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="GOOGLE_API_KEY is missing. Please set it in your .env file or environment variables.",
        )

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
        CacheAwareStaticFiles(directory=str(angular_browser_dist), html=True),
        name="angular_app",
    )
