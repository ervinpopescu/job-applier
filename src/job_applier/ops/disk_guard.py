"""
Disk-Pressure Guard and Resource Hygiene Module.

Provides:
1. Continuous storage headroom monitoring across data, output, and profile volumes.
2. Fail-closed automation halting when disk free space drops below critical threshold (<1GB or <5%).
3. Durable alert dispatch on disk pressure to prevent database corruption or incomplete writes.
4. Retention and TTL sweeping for debug traces and failure screenshots (7-day default TTL).
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from job_applier.automation.queue import (
    enqueue_notification,
    record_event,
    set_runtime_pause,
)
from job_applier.utils import get_project_root


class DiskPressureError(Exception):
    """Raised when server disk free space is below the safe operational threshold."""

    pass


# Default thresholds: 1 GiB free space and 5.0% free volume headroom
DEFAULT_MIN_FREE_BYTES = 1024 * 1024 * 1024  # 1 GB
DEFAULT_MIN_FREE_PERCENT = 5.0  # 5%
DEFAULT_DEBUG_TTL_DAYS = 7


@dataclass(frozen=True)
class DiskPressureStatus:
    is_under_pressure: bool
    free_bytes: int
    total_bytes: int
    free_percent: float
    monitored_path: str
    reason: str | None = None

    @property
    def free_mb(self) -> float:
        return round(self.free_bytes / (1024 * 1024), 2)

    @property
    def total_mb(self) -> float:
        return round(self.total_bytes / (1024 * 1024), 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_under_pressure": self.is_under_pressure,
            "free_bytes": self.free_bytes,
            "free_mb": self.free_mb,
            "total_bytes": self.total_bytes,
            "total_mb": self.total_mb,
            "free_percent": round(self.free_percent, 2),
            "monitored_path": self.monitored_path,
            "reason": self.reason,
        }


def check_disk_pressure(
    paths: list[Path] | None = None,
    min_free_bytes: int | None = None,
    min_free_percent: float | None = None,
) -> DiskPressureStatus:
    """
    Evaluates storage headroom across critical application paths (data, output, browser profile).
    Returns DiskPressureStatus indicating whether disk pressure is active.
    """
    project_root = get_project_root()
    env_min_bytes = os.environ.get("MIN_DISK_FREE_BYTES")
    env_min_pct = os.environ.get("MIN_DISK_FREE_PERCENT")

    effective_min_bytes = (
        min_free_bytes
        if min_free_bytes is not None
        else (int(env_min_bytes) if env_min_bytes else DEFAULT_MIN_FREE_BYTES)
    )
    effective_min_pct = (
        min_free_percent
        if min_free_percent is not None
        else (float(env_min_pct) if env_min_pct else DEFAULT_MIN_FREE_PERCENT)
    )

    targets = paths or [
        project_root / "data",
        project_root / "output",
        project_root / ".browser_profile",
    ]

    for p in targets:
        check_target = p
        # If target doesn't exist yet, inspect its existing parent
        while not check_target.exists() and check_target != check_target.parent:
            check_target = check_target.parent

        try:
            usage = shutil.disk_usage(check_target)
            free_bytes = usage.free
            total_bytes = usage.total
            free_pct = (free_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0

            if free_bytes < effective_min_bytes:
                free_mb = round(free_bytes / (1024 * 1024), 1)
                req_mb = round(effective_min_bytes / (1024 * 1024), 1)
                return DiskPressureStatus(
                    is_under_pressure=True,
                    free_bytes=free_bytes,
                    total_bytes=total_bytes,
                    free_percent=free_pct,
                    monitored_path=str(check_target),
                    reason=f"Free disk space ({free_mb} MB) is below minimum required headroom ({req_mb} MB).",
                )

            if free_pct < effective_min_pct:
                return DiskPressureStatus(
                    is_under_pressure=True,
                    free_bytes=free_bytes,
                    total_bytes=total_bytes,
                    free_percent=free_pct,
                    monitored_path=str(check_target),
                    reason=f"Free disk percentage ({round(free_pct, 1)}%) is below required threshold ({effective_min_pct}%).",
                )

        except OSError as e:
            # If disk_usage fails, report pressure to fail closed
            return DiskPressureStatus(
                is_under_pressure=True,
                free_bytes=0,
                total_bytes=0,
                free_percent=0.0,
                monitored_path=str(check_target),
                reason=f"Failed inspecting filesystem storage: {e}",
            )

    # All targets have adequate headroom
    ref_usage = shutil.disk_usage(project_root)
    return DiskPressureStatus(
        is_under_pressure=False,
        free_bytes=ref_usage.free,
        total_bytes=ref_usage.total,
        free_percent=(ref_usage.free / ref_usage.total * 100.0)
        if ref_usage.total > 0
        else 100.0,
        monitored_path=str(project_root),
        reason=None,
    )


def enforce_disk_pressure_guard(
    custom_db_path: Path | None = None,
    min_free_bytes: int | None = None,
    min_free_percent: float | None = None,
) -> bool:
    """
    Checks storage status and enforces fail-closed behavior:
    If disk pressure is detected:
    1. Pauses the worker runtime (set_runtime_pause(True)).
    2. Emits a critical durable notification to the operator.
    3. Records an audit event.
    4. Returns False (indicating pressure active).
    Returns True when headroom is sufficient.
    """
    status = check_disk_pressure(
        min_free_bytes=min_free_bytes,
        min_free_percent=min_free_percent,
    )

    if not status.is_under_pressure:
        return True

    # Engage fail-closed pause
    set_runtime_pause(True, custom_path=custom_db_path)

    # Emit critical notification to in-app notifications AND outbox
    enqueue_notification(
        category="system_failure",
        title="Disk Pressure Guard Triggered",
        message=(
            f"Server storage headroom critical ({status.free_mb} MB free). "
            "Automation has been paused to prevent database corruption."
        ),
        urgency="critical",
        severity="critical",
        payload=status.to_dict(),
        custom_path=custom_db_path,
    )

    # Record event
    record_event(
        job_id=None,
        app_id=None,
        event_type="disk_pressure_guard_triggered",
        level="ERROR",
        step="disk_guard",
        message=f"Fail-stop pause engaged: {status.reason}",
        details=status.to_dict(),
        custom_path=custom_db_path,
    )

    return False


def cleanup_expired_debug_artifacts(
    max_age_days: int = DEFAULT_DEBUG_TTL_DAYS,
    base_dir: Path | None = None,
) -> int:
    """
    Sweeps debug artifacts (Playwright traces, failure screenshots, diagnostic DOM dumps)
    older than max_age_days (default: 7 days) to enforce minimal evidence storage and
    prevent disk pressure accumulation.
    Returns the count of purged files.
    """
    project_root = base_dir or get_project_root()
    cutoff_time = time.time() - (max_age_days * 86400)
    purged_count = 0

    debug_filenames = {
        "diagnostic_dom.html",
        "submission_failed.png",
        "submission_fill_only.png",
        "diagnostics.json",
    }

    # 1. Sweep output/traces/
    traces_dir = project_root / "output" / "traces"
    if traces_dir.exists():
        for item in traces_dir.iterdir():
            try:
                if item.is_file() and item.stat().st_mtime < cutoff_time:
                    item.unlink()
                    purged_count += 1
                elif item.is_dir() and item.stat().st_mtime < cutoff_time:
                    shutil.rmtree(item, ignore_errors=True)
                    purged_count += 1
            except OSError:
                pass

    # 2. Sweep diagnostic files inside output/applications and output/applied
    for parent in ["applications", "applied"]:
        p_dir = project_root / "output" / parent
        if not p_dir.exists():
            continue
        for app_folder in p_dir.iterdir():
            if not app_folder.is_dir():
                continue
            for f in app_folder.iterdir():
                if f.name in debug_filenames and f.is_file():
                    try:
                        if f.stat().st_mtime < cutoff_time:
                            f.unlink()
                            purged_count += 1
                    except OSError:
                        pass

    return purged_count
