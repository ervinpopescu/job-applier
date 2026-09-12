"""
Comprehensive Backup, Restore, and Retention Operations Module.

Provides:
1. Online SQLite snapshot via SQLite Backup API (captures active WAL cleanly).
2. Quiesced artifact consistency (pauses queue during snapshot).
3. Stopped-profile consistency (refuses backup while browser is running, sanitizes ephemeral locks).
4. Age encryption with recipient public key (off-server private identity architecture).
5. Retention policy enforcement: keeps 7 daily and 4 weekly backup versions.
6. Safe restore drill and schema downgrade refusal protection.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.automation.browser_runtime import get_default_profile_dir
from job_applier.automation.profile_lock import (
    ProfileOwnershipError,
    ProfileOwnershipLock,
)
from job_applier.automation.queue import (
    get_runtime_control,
    record_event,
    set_runtime_pause,
)
from job_applier.db import (
    CURRENT_SCHEMA_VERSION,
    backup_db,
    get_db_path,
    get_schema_version,
    init_db,
)
from job_applier.utils import get_project_root, get_secret


class BackupOperationError(Exception):
    """Base exception for backup and restore operational failures."""

    pass


class ProfileConsistencyError(BackupOperationError):
    """Raised when browser profile is in an inconsistent state or actively locked."""

    pass


class DowngradeRefusalError(BackupOperationError):
    """Raised when attempting to restore or run a database with a newer schema version."""

    pass


class AgeEncryptionError(BackupOperationError):
    """Raised when age encryption or decryption fails."""

    pass


@dataclass(frozen=True)
class BackupManifest:
    version: str
    exported_at: str
    machine: str
    os_name: str
    schema_version: int
    files_count: int
    applications_count: int
    applied_count: int
    include_profile: bool
    db_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "exported_at": self.exported_at,
            "machine": self.machine,
            "os": self.os_name,
            "schema_version": self.schema_version,
            "files_count": self.files_count,
            "applications_count": self.applications_count,
            "applied_count": self.applied_count,
            "include_profile": self.include_profile,
            "db_sha256": self.db_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BackupManifest:
        return cls(
            version=data.get("version", "1.0.0"),
            exported_at=data.get("exported_at", ""),
            machine=data.get("machine", ""),
            os_name=data.get("os", ""),
            schema_version=int(data.get("schema_version", 1)),
            files_count=int(data.get("files_count", 0)),
            applications_count=int(data.get("applications_count", 0)),
            applied_count=int(data.get("applied_count", 0)),
            include_profile=bool(data.get("include_profile", False)),
            db_sha256=data.get("db_sha256", ""),
        )


def _compute_sha256(path: Path) -> str:
    """Computes SHA-256 checksum of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


# Ephemeral locks, sockets, and cache artifacts to exclude when backing up browser profiles
PROFILE_EXCLUDE_FILENAMES = {
    ".profile_ownership.lock",
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "lockfile",
}


def sanitize_profile_copy(src_dir: Path, dst_dir: Path) -> int:
    """
    Copies browser profile directory to destination while stripping ephemeral
    locks, Unix domain sockets, and running process locks to guarantee consistency upon restore.
    Returns number of files copied.
    """
    if not src_dir.exists():
        return 0

    copied = 0
    dst_dir.mkdir(parents=True, exist_ok=True)

    for root, dirs, files in os.walk(src_dir):
        rel_root = Path(root).relative_to(src_dir)
        target_root = dst_dir / rel_root
        target_root.mkdir(parents=True, exist_ok=True)

        for f in files:
            if f in PROFILE_EXCLUDE_FILENAMES:
                continue
            src_file = Path(root) / f
            dst_file = target_root / f
            # Exclude sockets or special files
            if src_file.is_socket():
                continue
            try:
                shutil.copy2(src_file, dst_file)
                copied += 1
            except OSError as err:
                # Tolerate transient cache read issues
                print(f"Notice: skipped profile file {src_file.name}: {err}")

    return copied


def encrypt_with_age(
    plaintext_path: Path,
    output_path: Path,
    recipient: str,
) -> Path:
    """
    Encrypts a file with age using an X25519 or SSH recipient public key.
    Enforces atomic publication and fail-closed cleanup on error.
    Server stores ONLY the public recipient key; private identity remains off-server.
    """
    recipient_clean = recipient.strip()
    if not recipient_clean:
        raise AgeEncryptionError("Recipient public key cannot be empty.")

    tmp_out = output_path.with_name(f".tmp_enc_{output_path.name}_{int(time.time())}")

    try:
        # Try pyrage first
        try:
            import pyrage
            from pyrage import ssh, x25519

            parsed_recipient: Any = None
            if recipient_clean.startswith("age1"):
                parsed_recipient = x25519.Recipient.from_str(recipient_clean)
            elif recipient_clean.startswith("ssh-"):
                parsed_recipient = ssh.Recipient.from_str(recipient_clean)
            else:
                # Try X25519
                parsed_recipient = x25519.Recipient.from_str(recipient_clean)

            with open(plaintext_path, "rb") as f_in:
                data = f_in.read()

            encrypted_bytes = pyrage.encrypt(data, [parsed_recipient])

            with open(tmp_out, "wb") as f_out:
                f_out.write(encrypted_bytes)

        except (ImportError, Exception) as pyrage_err:
            # Fallback to age CLI binary if pyrage fails or recipient format needs CLI
            age_bin = shutil.which("age") or shutil.which("rage")
            if not age_bin:
                raise AgeEncryptionError(
                    f"Age encryption failed via pyrage ({pyrage_err}) and no 'age' or 'rage' binary was found."
                ) from pyrage_err

            import subprocess

            cmd = [
                age_bin,
                "-r",
                recipient_clean,
                "-o",
                str(tmp_out),
                str(plaintext_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise AgeEncryptionError(
                    f"age CLI encryption failed: {proc.stderr.strip()}"
                )

        os.replace(tmp_out, output_path)
        return output_path

    except Exception as e:
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass
        raise AgeEncryptionError(f"Age encryption failed: {e}") from e


def decrypt_with_age(
    ciphertext_path: Path,
    output_path: Path,
    identity: str | None = None,
    identity_file: Path | None = None,
) -> Path:
    """
    Decrypts an age-encrypted archive using the operator's private identity key.
    Supports in-memory identity string or identity key file.
    """
    ident_str = (identity or "").strip()
    if not ident_str and identity_file and Path(identity_file).exists():
        ident_str = Path(identity_file).read_text(encoding="utf-8").strip()

    if not ident_str:
        # Check environment variable or mounted secret
        ident_str = (
            os.environ.get("AGE_IDENTITY", "").strip()
            or os.environ.get("AGE_SECRET_KEY", "").strip()
            or (get_secret("age_secret_key") or "").strip()
            or (get_secret("age_identity") or "").strip()
        )

    if not ident_str:
        raise AgeEncryptionError(
            "Age decryption requires private identity string or identity file."
        )

    tmp_out = output_path.with_name(f".tmp_dec_{output_path.name}_{int(time.time())}")

    try:
        # Try pyrage first
        try:
            import pyrage
            from pyrage import ssh, x25519

            parsed_identity: Any = None
            # Extract non-comment lines
            active_lines = [
                line.strip()
                for line in ident_str.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if not active_lines:
                raise ValueError("No identity keys found in identity specification.")

            first_key = active_lines[0]
            if first_key.startswith("AGE-SECRET-KEY-1"):
                parsed_identity = x25519.Identity.from_str(first_key)
            elif "PRIVATE KEY" in ident_str:
                parsed_identity = ssh.Identity.from_buffer(ident_str.encode("utf-8"))
            else:
                parsed_identity = x25519.Identity.from_str(first_key)

            with open(ciphertext_path, "rb") as f_in:
                cipher_data = f_in.read()

            decrypted_bytes = pyrage.decrypt(cipher_data, [parsed_identity])

            with open(tmp_out, "wb") as f_out:
                f_out.write(decrypted_bytes)

        except (ImportError, Exception) as pyrage_err:
            # Fallback to age CLI binary
            age_bin = shutil.which("age") or shutil.which("rage")
            if not age_bin:
                raise AgeEncryptionError(
                    f"Age decryption failed via pyrage ({pyrage_err}) and no 'age' CLI binary found."
                ) from pyrage_err

            import subprocess

            with tempfile.NamedTemporaryFile(mode="w", delete=False) as f_key:
                f_key.write(ident_str)
                f_key_path = f_key.name

            try:
                cmd = [
                    age_bin,
                    "-d",
                    "-i",
                    f_key_path,
                    "-o",
                    str(tmp_out),
                    str(ciphertext_path),
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise AgeEncryptionError(
                        f"age CLI decryption failed: {proc.stderr.strip()}"
                    )
            finally:
                if os.path.exists(f_key_path):
                    try:
                        os.unlink(f_key_path)
                    except OSError:
                        pass

        os.replace(tmp_out, output_path)
        return output_path

    except Exception as e:
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass
        raise AgeEncryptionError(f"Age decryption failed: {e}") from e


def create_backup(
    output_dir: Path | None = None,
    recipient: str | None = None,
    include_profile: bool = True,
    require_stopped_profile: bool = True,
    quiesce_worker: bool = True,
    custom_db_path: Path | None = None,
    browser_engine: str = "chromium",
    allow_unencrypted: bool = False,
) -> Path:
    """
    Creates an atomic, consistent, and optionally age-encrypted system backup.
    Guarantees:
    1. Quiesced artifact consistency: Pauses worker during snapshot.
    2. Online SQLite snapshot: Captures WAL database cleanly via SQLite backup API.
    3. Stopped-profile consistency: Fails closed if browser is actively locked.
    4. Ephemeral lock sanitization: Excludes runtime sockets and file locks.
    5. Age encryption: Recipient public key encrypts archive; key is held off-server.
    """
    project_root = get_project_root()
    init_db(custom_db_path)

    now = datetime.now()
    now_str = now.strftime("%Y%m%d_%H%M%S")
    export_dir = output_dir or (project_root / "output" / "backups")
    export_dir.mkdir(parents=True, exist_ok=True)

    # 1. Check Age Recipient Key
    active_recipient = (
        recipient
        or os.environ.get("AGE_RECIPIENT", "").strip()
        or get_secret("age_recipient")
    )

    runtime_mode = os.environ.get("JOB_APPLIER_RUNTIME_MODE", "cli").lower()
    if not active_recipient and runtime_mode == "service" and not allow_unencrypted:
        raise AgeEncryptionError(
            "Production service backup requires Age encryption. "
            "Configure AGE_RECIPIENT / secret 'age_recipient', or pass allow_unencrypted=True."
        )

    # 2. Stopped-Profile Consistency Check
    profile_dir = get_default_profile_dir(browser_engine)
    if include_profile and profile_dir.exists():
        # Check if browser profile is currently locked by a running process
        lock_file = profile_dir / ".profile_ownership.lock"
        if lock_file.exists():
            # Attempt non-blocking test lock to see if another process holds flock
            test_lock = ProfileOwnershipLock(profile_dir, owner_type="backup_probe")
            try:
                test_lock.acquire()
                test_lock.release()
            except ProfileOwnershipError as e:
                if require_stopped_profile:
                    raise ProfileConsistencyError(
                        f"Cannot create consistent profile backup: {e}. "
                        "Please stop the Chromium runtime daemon before backing up, "
                        "or run with include_profile=False."
                    ) from e
                else:
                    print(
                        f"Warning: profile lock is held but require_stopped_profile=False: {e}"
                    )

    # 3. Quiesced Worker State
    was_paused = False
    if quiesce_worker:
        ctrl = get_runtime_control(custom_db_path)
        was_paused = ctrl.get("is_paused", False)
        if not was_paused:
            set_runtime_pause(True, custom_path=custom_db_path)
            record_event(
                job_id=None,
                app_id=None,
                event_type="backup_quiesce_engaged",
                level="INFO",
                step="backup",
                message="Worker paused for atomic backup snapshot.",
                custom_path=custom_db_path,
            )

    tmp_zip: Path | None = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="job_applier_backup_staging_"
        ) as stage_str:
            staging = Path(stage_str)

            # a. SQLite online backup snapshot
            staged_db = staging / "job_applier.db"
            backup_db(staged_db, custom_path=custom_db_path)
            db_hash = _compute_sha256(staged_db)
            schema_ver = get_schema_version(custom_path=staged_db)

            files_to_pack: list[tuple[Path, str]] = [
                (staged_db, "data/job_applier.db"),
            ]

            # b. Data directory files
            for fname in [
                "applications_tracker.csv",
                "processed_jobs.txt",
            ]:
                fpath = project_root / "data" / fname
                if fpath.exists():
                    files_to_pack.append((fpath, f"data/{fname}"))

            if include_profile:
                for fname in [
                    "candidate_profile.json",
                    "master_resume.json",
                ]:
                    fpath = project_root / "data" / fname
                    if fpath.exists():
                        files_to_pack.append((fpath, f"data/{fname}"))
            else:
                # Sanitize candidate profile snapshots in staged database for PII-free backup
                try:
                    with sqlite3.connect(str(staged_db)) as s_conn:
                        s_conn.execute(
                            "UPDATE application_attempts SET profile_snapshot = '{}';"
                        )
                except Exception:
                    pass

            # c. Application artifact directories
            apps_dir = project_root / "output" / "applications"
            app_count = 0
            if apps_dir.exists():
                for folder in apps_dir.iterdir():
                    if folder.is_dir():
                        app_count += 1
                        for file in folder.iterdir():
                            if file.is_file():
                                if not include_profile:
                                    fname_lower = file.name.lower()
                                    if (
                                        fname_lower.endswith(".pdf")
                                        or "cover_letter" in fname_lower
                                        or "resume" in fname_lower
                                    ):
                                        continue
                                files_to_pack.append(
                                    (
                                        file,
                                        f"output/applications/{folder.name}/{file.name}",
                                    )
                                )

            applied_dir = project_root / "output" / "applied"
            applied_count = 0
            if applied_dir.exists():
                for folder in applied_dir.iterdir():
                    if folder.is_dir():
                        applied_count += 1
                        for file in folder.iterdir():
                            if file.is_file():
                                if not include_profile:
                                    fname_lower = file.name.lower()
                                    if (
                                        fname_lower.endswith(".pdf")
                                        or "cover_letter" in fname_lower
                                        or "resume" in fname_lower
                                    ):
                                        continue
                                files_to_pack.append(
                                    (file, f"output/applied/{folder.name}/{file.name}")
                                )

            # d. Browser profile sanitization & snapshot
            if include_profile and profile_dir.exists():
                staged_profile = staging / "browser_profile"
                sanitize_profile_copy(profile_dir, staged_profile)
                for root, _, files in os.walk(staged_profile):
                    for f in files:
                        p_file = Path(root) / f
                        rel_path = p_file.relative_to(staged_profile)
                        files_to_pack.append((p_file, f"profile/{rel_path}"))

            # e. Manifest
            manifest = BackupManifest(
                version="1.0.0",
                exported_at=now.strftime("%Y-%m-%d %H:%M:%S"),
                machine=platform.node(),
                os_name=platform.system(),
                schema_version=schema_ver,
                files_count=len(files_to_pack),
                applications_count=app_count,
                applied_count=applied_count,
                include_profile=include_profile,
                db_sha256=db_hash,
            )

            # Build ZIP archive atomically
            zip_filename = f"backup_{now_str}_{schema_ver}.zip"
            tmp_zip = export_dir / f".tmp_{zip_filename}"

            with zipfile.ZipFile(str(tmp_zip), "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(manifest.to_dict(), indent=2))
                for src, arc in files_to_pack:
                    zf.write(str(src), arcname=arc)

            final_zip = export_dir / zip_filename

            # f. Encrypt with Age if recipient configured
            if active_recipient:
                age_filename = f"{zip_filename}.age"
                final_age = export_dir / age_filename
                encrypt_with_age(tmp_zip, final_age, active_recipient)
                if tmp_zip.exists():
                    tmp_zip.unlink()
                print(f"🔒 Encrypted backup published: {final_age}")
                return final_age
            else:
                os.replace(tmp_zip, final_zip)
                print(f"📦 Unencrypted backup published: {final_zip}")
                return final_zip

    finally:
        if tmp_zip and tmp_zip.exists():
            try:
                tmp_zip.unlink()
            except OSError:
                pass

        # Release quiescence if we engaged it
        if quiesce_worker and not was_paused:
            set_runtime_pause(False, custom_path=custom_db_path)
            record_event(
                job_id=None,
                app_id=None,
                event_type="backup_quiesce_released",
                level="INFO",
                step="backup",
                message="Worker unpaused following backup completion.",
                custom_path=custom_db_path,
            )


def apply_retention_policy(
    backups_dir: Path | None = None,
    daily_limit: int = 7,
    weekly_limit: int = 4,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Applies the backup retention policy:
    - Retains newest backup for each of the last 7 distinct calendar days.
    - Retains newest backup for each of the last 4 distinct ISO calendar weeks.
    - Prunes all backups outside the union of these two sets.
    """
    project_root = get_project_root()
    b_dir = backups_dir or (project_root / "output" / "backups")
    if not b_dir.exists():
        return {"retained": [], "pruned": [], "total_scanned": 0}

    # Find backup files matching pattern backup_YYYYMMDD_HHMMSS_*
    pattern = re.compile(r"^backup_(\d{8})_(\d{6})_.*?\.(?:zip|zip\.age)$")
    backups: list[tuple[datetime, Path]] = []

    for file in b_dir.iterdir():
        if not file.is_file():
            continue
        match = pattern.match(file.name)
        if match:
            date_part, time_part = match.groups()
            try:
                dt = datetime.strptime(f"{date_part}_{time_part}", "%Y%m%d_%H%M%S")
                backups.append((dt, file))
            except ValueError:
                continue
        elif file.name.endswith(".zip") or file.name.endswith(".zip.age"):
            # Fallback to mtime
            dt = datetime.fromtimestamp(file.stat().st_mtime)
            backups.append((dt, file))

    if not backups:
        return {"retained": [], "pruned": [], "total_scanned": 0}

    # Sort newest first
    backups.sort(key=lambda x: x[0], reverse=True)

    # 1. Collect Daily: newest per calendar date (YYYY-MM-DD)
    daily_map: dict[str, Path] = {}
    for dt, path in backups:
        day_str = dt.strftime("%Y-%m-%d")
        if day_str not in daily_map:
            daily_map[day_str] = path

    # Top daily_limit distinct days
    sorted_days = sorted(daily_map.keys(), reverse=True)[:daily_limit]
    retained_daily = {daily_map[day] for day in sorted_days}

    # 2. Collect Weekly: newest per ISO week (YYYY-Www)
    weekly_map: dict[str, Path] = {}
    for dt, path in backups:
        week_str = dt.strftime("%G-W%V")
        if week_str not in weekly_map:
            weekly_map[week_str] = path

    sorted_weeks = sorted(weekly_map.keys(), reverse=True)[:weekly_limit]
    retained_weekly = {weekly_map[week] for week in sorted_weeks}

    retained_all = retained_daily | retained_weekly

    pruned: list[str] = []
    retained: list[str] = []

    for _, path in backups:
        if path in retained_all:
            retained.append(str(path.name))
        else:
            pruned.append(str(path.name))
            if not dry_run:
                try:
                    path.unlink()
                except OSError as e:
                    print(f"Warning: could not prune {path.name}: {e}")

    return {
        "retained": retained,
        "pruned": pruned,
        "total_scanned": len(backups),
        "dry_run": dry_run,
    }


def restore_backup(
    backup_path: Path,
    identity: str | None = None,
    identity_file: Path | None = None,
    target_dir: Path | None = None,
    restore_profile: bool = True,
    overwrite_profile: bool = False,
    custom_db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Restores a system backup with strict downgrade protection.
    Guarantees:
    1. Age decryption using provided off-server identity.
    2. Manifest verification.
    3. Downgrade refusal: Refuses restore if backup schema version > code schema version.
    4. Database integrity verification.
    5. Clean profile restoration with stale lock removal.
    """
    backup_file = Path(backup_path)
    if not backup_file.exists():
        raise BackupOperationError(f"Backup file not found: {backup_file}")

    target_root = target_dir or get_project_root()
    report: dict[str, Any] = {
        "status": "success",
        "manifest": {},
        "restored_files": 0,
        "schema_version": 0,
        "profile_restored": False,
    }

    with tempfile.TemporaryDirectory(prefix="job_applier_restore_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        active_zip_path = tmp_dir / "archive.zip"

        # 1. Decrypt if .age
        if backup_file.name.endswith(".age"):
            decrypt_with_age(
                ciphertext_path=backup_file,
                output_path=active_zip_path,
                identity=identity,
                identity_file=identity_file,
            )
        else:
            shutil.copy2(backup_file, active_zip_path)

        # 2. Inspect manifest and schema version
        with zipfile.ZipFile(str(active_zip_path), "r") as zf:
            namelist = set(zf.namelist())
            if "manifest.json" in namelist:
                manifest_data = json.loads(zf.read("manifest.json").decode("utf-8"))
                report["manifest"] = manifest_data
                backup_schema = int(manifest_data.get("schema_version", 1))
            else:
                backup_schema = 1

            # Downgrade refusal check from manifest
            if backup_schema > CURRENT_SCHEMA_VERSION:
                raise DowngradeRefusalError(
                    f"Backup schema version ({backup_schema}) is newer than supported codebase version ({CURRENT_SCHEMA_VERSION}). "
                    "Refusing restore to prevent database corruption."
                )

            # 3. Check internal database schema version
            if "data/job_applier.db" in namelist:
                staged_db = tmp_dir / "extracted_check.db"
                with (
                    zf.open("data/job_applier.db") as src,
                    open(staged_db, "wb") as dst,
                ):
                    shutil.copyfileobj(src, dst)

                db_ver = get_schema_version(custom_path=staged_db)
                report["schema_version"] = db_ver
                if db_ver > CURRENT_SCHEMA_VERSION:
                    raise DowngradeRefusalError(
                        f"Database in backup has schema version {db_ver}, which is newer than supported codebase version {CURRENT_SCHEMA_VERSION}. "
                        "Refusing restore."
                    )

                # Verify database integrity check
                conn = sqlite3.connect(str(staged_db))
                try:
                    integrity = conn.execute("PRAGMA integrity_check;").fetchall()
                    if not integrity or integrity[0][0] != "ok":
                        raise BackupOperationError(
                            f"Backup database failed integrity check: {integrity}"
                        )
                finally:
                    conn.close()

                # Atomically replace target database
                dest_db = get_db_path(custom_db_path)
                dest_db.parent.mkdir(parents=True, exist_ok=True)
                tmp_dest_db = dest_db.with_name(f".tmp_restore_{dest_db.name}")
                shutil.copy2(staged_db, tmp_dest_db)
                os.replace(tmp_dest_db, dest_db)
                report["restored_files"] += 1

            # 4. Restore data files
            for member in namelist:
                if member in [
                    "data/applications_tracker.csv",
                    "data/processed_jobs.txt",
                ]:
                    t_path = target_root / member
                    t_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(t_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    report["restored_files"] += 1

                elif member in [
                    "data/candidate_profile.json",
                    "data/master_resume.json",
                ]:
                    t_path = target_root / member
                    if not t_path.exists() or overwrite_profile:
                        t_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(t_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        report["restored_files"] += 1

                elif member.startswith("output/applications/") or member.startswith(
                    "output/applied/"
                ):
                    # Path traversal safety
                    if member.startswith(("/", "\\")) or ".." in member:
                        continue
                    t_path = target_root / member
                    t_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(t_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    report["restored_files"] += 1

            # 5. Restore profile if requested
            if restore_profile:
                profile_members = [
                    m
                    for m in namelist
                    if m.startswith("profile/") and not m.endswith("/")
                ]
                if profile_members:
                    p_target = target_root / ".browser_profile"
                    p_target.mkdir(parents=True, exist_ok=True)
                    for pm in profile_members:
                        rel = pm[len("profile/") :]
                        f_dest = p_target / rel
                        f_dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(pm) as src, open(f_dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    report["profile_restored"] = True

    return report
