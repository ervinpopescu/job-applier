from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.db import backup_db, get_db_path
from job_applier.utils import get_project_root


def export_bundle(
    output_path: Path | None = None,
    include_applications: bool = True,
    include_applied: bool = True,
    include_profile: bool = True,
    custom_db_path: Path | None = None,
) -> Path:
    """
    Exports applications, tailored PDFs, cover letters, and database mappings
    into a portable, self-contained .zip backup bundle.
    Guarantees:
    1. Fail-closed: Never falls back to a raw uncheckpointed file copy if backup_db fails.
    2. Collision-free: Uses private per-export temp storage with guaranteed cleanup.
    3. Atomic publication: Writes to an isolated temp archive and atomically replaces output_path.
    4. Non-mutating: Does not mutate the live database, application records, or disk state during export.
    5. Honors include_profile flag: Omits profile and master resume if include_profile=False.
    """
    project_root = get_project_root()
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_suffix = uuid.uuid4().hex[:8]

    if output_path is None:
        export_dir = project_root / "output" / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        output_path = export_dir / f"job_applier_backup_{now_str}_{unique_suffix}.zip"
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "version": "1.0.0",
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "machine": platform.node(),
        "os": platform.system(),
        "files_count": 0,
        "applications_count": 0,
        "applied_count": 0,
        "include_profile": include_profile,
    }

    files_to_pack: list[tuple[Path, str]] = []

    with tempfile.TemporaryDirectory(prefix="job_applier_export_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)

        # 1. Database snapshot via SQLite Online Backup API (captures active WAL without locking)
        db_file = get_db_path(custom_db_path)
        if db_file.exists():
            tmp_backup_file = tmp_dir / "job_applier.db"
            # Fail closed: Do NOT catch and fallback to raw copy!
            backup_db(tmp_backup_file, custom_path=db_file)
            files_to_pack.append((tmp_backup_file, "data/job_applier.db"))

        # 2. Data files (strictly respecting include_profile)
        data_files = ["applications_tracker.csv", "processed_jobs.txt"]
        if include_profile:
            data_files.extend(["master_resume.json", "candidate_profile.json"])

        for fname in data_files:
            fpath = project_root / "data" / fname
            if fpath.exists():
                files_to_pack.append((fpath, f"data/{fname}"))

        # 3. Pending Application Packages
        if include_applications:
            apps_dir = project_root / "output" / "applications"
            if apps_dir.exists():
                app_folders = [d for d in apps_dir.iterdir() if d.is_dir()]
                manifest["applications_count"] = len(app_folders)
                for d in app_folders:
                    for f in d.iterdir():
                        if f.is_file():
                            rel_arc = f"output/applications/{d.name}/{f.name}"
                            files_to_pack.append((f, rel_arc))

        # 4. Applied Packages
        if include_applied:
            applied_dir = project_root / "output" / "applied"
            if applied_dir.exists():
                applied_folders = [d for d in applied_dir.iterdir() if d.is_dir()]
                manifest["applied_count"] = len(applied_folders)
                for d in applied_folders:
                    for f in d.iterdir():
                        if f.is_file():
                            rel_arc = f"output/applied/{d.name}/{f.name}"
                            files_to_pack.append((f, rel_arc))

        manifest["files_count"] = len(files_to_pack)

        # 5. Build archive atomically in private temp file
        tmp_zip_path = output_path.with_name(f".tmp_{output_path.name}_{unique_suffix}")
        try:
            with zipfile.ZipFile(str(tmp_zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(manifest, indent=2))
                for src_file, arc_name in files_to_pack:
                    # Fail closed: Do NOT catch and swallow file write errors!
                    zf.write(str(src_file), arcname=arc_name)

            # Atomic replace to prevent partial or corrupted published archives
            os.replace(tmp_zip_path, output_path)
        except Exception:
            if tmp_zip_path.exists():
                try:
                    tmp_zip_path.unlink()
                except Exception:
                    pass
            raise

    print(
        f"📦 Successfully created portable export bundle ({len(files_to_pack)} files): {output_path}"
    )
    return output_path


def import_bundle(
    zip_path: Path,
    overwrite_profile: bool = False,
) -> dict[str, Any]:
    """
    Imports and merges a backup bundle from another machine into the local environment.
    Unpacks application packages and merges SQLite database records cleanly.
    """
    if not zip_path.exists():
        raise FileNotFoundError(f"Export bundle not found at {zip_path}")

    project_root = get_project_root()
    report: dict[str, Any] = {
        "status": "success",
        "imported_applications": 0,
        "imported_applied": 0,
        "merged_db_records": 0,
        "manifest": {},
    }

    with zipfile.ZipFile(str(zip_path), "r") as zf:
        # 1. Read manifest
        if "manifest.json" in zf.namelist():
            try:
                manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
                report["manifest"] = manifest_data
            except Exception:
                pass

        # 2. Extract application and applied directories safely
        for member in zf.infolist():
            # Prevent path traversal
            if member.filename.startswith(("/", "\\")) or ".." in member.filename:
                continue

            # Extract applications/
            if (
                member.filename.startswith("output/applications/")
                and not member.is_dir()
            ):
                target = project_root / member.filename
                if str(target.resolve()).startswith(str(project_root.resolve())):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with zf.open(member) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        report["imported_applications"] += 1
                    except (OSError, zipfile.BadZipFile) as err:
                        print(
                            f"Notice: Failed to extract application file {member.filename}: {err}"
                        )

            # Extract applied/
            elif member.filename.startswith("output/applied/") and not member.is_dir():
                target = project_root / member.filename
                if str(target.resolve()).startswith(str(project_root.resolve())):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with zf.open(member) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        report["imported_applied"] += 1
                    except (OSError, zipfile.BadZipFile) as err:
                        print(
                            f"Notice: Failed to extract applied file {member.filename}: {err}"
                        )

            # Extract profile/resume if permitted or not present
            elif member.filename in [
                "data/candidate_profile.json",
                "data/master_resume.json",
            ]:
                target = project_root / member.filename
                if not target.exists() or overwrite_profile:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with zf.open(member) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    except (OSError, zipfile.BadZipFile) as err:
                        print(f"Notice: Failed to extract {member.filename}: {err}")

        # 3. Merge processed_jobs.txt if present
        if "data/processed_jobs.txt" in zf.namelist():
            local_pj = project_root / "data" / "processed_jobs.txt"
            try:
                incoming_text = zf.read("data/processed_jobs.txt").decode(
                    "utf-8", errors="replace"
                )
                incoming_urls = {
                    line.strip() for line in incoming_text.splitlines() if line.strip()
                }
                existing_urls = set()
                if local_pj.exists():
                    existing_urls = {
                        line.strip()
                        for line in local_pj.read_text().splitlines()
                        if line.strip()
                    }
                merged_urls = existing_urls | incoming_urls
                local_pj.parent.mkdir(parents=True, exist_ok=True)
                local_pj.write_text("\n".join(sorted(merged_urls)) + "\n")
            except Exception as ex:
                print(f"Notice: Failed to merge processed_jobs.txt: {ex}")

        # 4. Merge SQLite database records if database file present in zip
        if "data/job_applier.db" in zf.namelist():
            temp_db = (
                project_root
                / "output"
                / f"imported_temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            )
            temp_db.parent.mkdir(parents=True, exist_ok=True)
            try:
                with (
                    zf.open("data/job_applier.db") as src,
                    open(temp_db, "wb") as dst,
                ):
                    shutil.copyfileobj(src, dst)

                # Read ALL applications from imported temp DB without arbitrary limits
                import sqlite3
                from job_applier.db import (
                    get_connection,
                    init_db,
                    mark_job_processed,
                    upsert_application,
                )

                t_conn = sqlite3.connect(str(temp_db))
                t_conn.row_factory = sqlite3.Row
                try:
                    app_rows = t_conn.execute("SELECT * FROM applications;").fetchall()

                    # Connect to local live DB to check existing statuses
                    live_db_file = project_root / "data" / "job_applier.db"
                    init_db(custom_path=live_db_file)
                    live_conn = get_connection(custom_path=live_db_file)
                    try:
                        existing_statuses = {
                            r["id"]: r["status"]
                            for r in live_conn.execute(
                                "SELECT id, status FROM applications;"
                            ).fetchall()
                        }
                    finally:
                        live_conn.close()

                    for row in app_rows:
                        app = dict(row)
                        app_id = app["id"]
                        imported_status = app.get("status", "pending")
                        # Conservative status merge: do not allow older bundle to downgrade applied/ambiguous to pending
                        local_status = existing_statuses.get(app_id)
                        if local_status in (
                            "applied",
                            "ambiguous",
                        ) and imported_status not in ("applied",):
                            target_status = local_status
                        else:
                            target_status = imported_status

                        upsert_application(
                            app_id=app_id,
                            company=app["company"],
                            title=app["title"],
                            job_url=app["job_url"],
                            platform=app.get("platform", "Generic"),
                            status=target_status,
                            submission_type=app.get("submission_type", "manual"),
                            folder_name=str(app.get("folder_name") or app_id),
                            cv_filename=app.get("cv_filename", ""),
                            has_cover_letter=bool(app.get("has_cover_letter")),
                            proof_screenshot=app.get("proof_screenshot", ""),
                            notes=app.get("notes", ""),
                            custom_path=live_db_file,
                        )
                        report["merged_db_records"] += 1

                    # Merge processed_jobs table from imported database
                    has_processed_table = t_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='processed_jobs';"
                    ).fetchone()
                    if has_processed_table:
                        pj_rows = t_conn.execute(
                            "SELECT * FROM processed_jobs;"
                        ).fetchall()
                        for pj in pj_rows:
                            col_url = (
                                pj["job_url"] if "job_url" in pj.keys() else pj["url"]
                            )
                            if col_url:
                                col_app = pj["app_id"] if "app_id" in pj.keys() else ""
                                mark_job_processed(
                                    col_url, app_id=col_app, custom_path=live_db_file
                                )
                finally:
                    t_conn.close()
            except Exception as e:
                print(f"Notice: Database merge from backup bundle failed: {e}")
            finally:
                if temp_db.exists():
                    try:
                        temp_db.unlink()
                    except Exception:
                        pass

    print(
        f"✅ Bundle successfully imported: {report['imported_applications']} applications, {report['imported_applied']} applied, {report['merged_db_records']} DB records."
    )
    return report
