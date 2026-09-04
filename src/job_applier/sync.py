from __future__ import annotations

import json
import platform
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.db import get_db_path, migrate_csv_and_disk_to_db
from job_applier.utils import get_project_root


def export_bundle(
    output_path: Path | None = None,
    include_applications: bool = True,
    include_applied: bool = True,
    include_profile: bool = True,
) -> Path:
    """
    Exports all applications, tailored PDFs, cover letters, and database mappings
    into a portable, self-contained .zip backup bundle for transfer to another machine.
    """
    project_root = get_project_root()
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    if output_path is None:
        export_dir = project_root / "output" / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        output_path = export_dir / f"job_applier_backup_{now_str}.zip"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure database is synchronized with latest disk state
    migrate_csv_and_disk_to_db()

    manifest: dict[str, Any] = {
        "version": "1.0.0",
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "machine": platform.node(),
        "os": platform.system(),
        "files_count": 0,
        "applications_count": 0,
        "applied_count": 0,
    }

    files_to_pack: list[tuple[Path, str]] = []

    # 1. Database and Data files
    db_file = get_db_path()
    if db_file.exists():
        files_to_pack.append((db_file, "data/job_applier.db"))

    for fname in [
        "master_resume.json",
        "candidate_profile.json",
        "applications_tracker.csv",
        "processed_jobs.txt",
    ]:
        fpath = project_root / "data" / fname
        if fpath.exists():
            files_to_pack.append((fpath, f"data/{fname}"))

    # 2. Pending Application Packages
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

    # 3. Applied Packages
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

    # Build the zip archive
    with zipfile.ZipFile(str(output_path), "w", zipfile.ZIP_DEFLATED) as zf:
        # Write manifest
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        # Write files
        for src_file, arc_name in files_to_pack:
            try:
                zf.write(str(src_file), arcname=arc_name)
            except Exception as e:
                print(f"Notice during zip write of {src_file.name}: {e}")

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
                if (overwrite_profile or not target.exists()) and str(
                    target.resolve()
                ).startswith(str(project_root.resolve())):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        with zf.open(member) as src, open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    except (OSError, zipfile.BadZipFile) as err:
                        print(
                            f"Notice: Failed to extract profile {member.filename}: {err}"
                        )

            # Extract tracker CSV to merge
            elif member.filename == "data/applications_tracker.csv":
                temp_csv = project_root / "data" / "_imported_tracker.csv"
                if str(temp_csv.resolve()).startswith(str(project_root.resolve())):
                    try:
                        with zf.open(member) as src, open(temp_csv, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    except (OSError, zipfile.BadZipFile) as err:
                        print(f"Notice: Failed to extract tracker CSV: {err}")

    # 3. Synchronize unpacked folders and imported CSV into local SQLite DB
    migrated_count = migrate_csv_and_disk_to_db()
    report["merged_db_records"] = migrated_count

    # Cleanup temp csv
    temp_csv = project_root / "data" / "_imported_tracker.csv"
    if temp_csv.exists():
        temp_csv.unlink(missing_ok=True)

    print(
        f"✅ Successfully imported bundle from {zip_path.name} (Merged {migrated_count} records)!"
    )
    return report
