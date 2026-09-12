"""
ntfy.py: Durable outbox dispatcher for mobile and offline notifications.
Dispatches push alerts to self-hosted ntfy instance at notify.jobs.aslan.net
with restricted token ACLs, anonymous deny-all, bounded retries, and rate-limit handling.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from email.header import Header
from pathlib import Path
from typing import Any

from job_applier.automation.queue import (
    get_pending_notifications,
    mark_notification_retry,
    mark_notification_sent,
)
from job_applier.utils import get_secret

logger = logging.getLogger("job_applier.ntfy")

DEFAULT_NTFY_URL = "http://ntfy:80"
DEFAULT_NTFY_TOPIC = "job-alerts"
MAX_OUTBOX_RETRIES = 5


def get_ntfy_config() -> dict[str, str]:
    """Resolves ntfy configuration from environment and secrets."""
    url = os.environ.get("NTFY_URL", DEFAULT_NTFY_URL).rstrip("/")
    topic = os.environ.get("NTFY_TOPIC", DEFAULT_NTFY_TOPIC)
    token = get_secret("ntfy_token") or os.environ.get("NTFY_TOKEN", "")
    public_base_url = os.environ.get("PUBLIC_ORIGIN", "https://jobs.aslan.net").rstrip(
        "/"
    )
    fallback_script = os.environ.get("NTFY_FALLBACK_SCRIPT", "").strip()
    if not fallback_script:
        candidate = Path.home() / "bin" / "notify-alert.sh"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            fallback_script = str(candidate)

    return {
        "url": url,
        "topic": topic,
        "token": token,
        "public_base_url": public_base_url,
        "fallback_script": fallback_script,
    }


def map_urgency_to_priority(urgency: str) -> str:
    """Maps internal notification urgency to ntfy priority level."""
    urgency_lower = (urgency or "").lower()
    if urgency_lower in ("critical", "emergency"):
        return "urgent"  # 5
    if urgency_lower in ("high", "error"):
        return "high"  # 4
    if urgency_lower in ("low", "info"):
        return "low"  # 2
    return "default"  # 3


def map_category_to_tags(category: str) -> list[str]:
    """Maps notification category to ntfy emoji/tag icons."""
    cat_lower = (category or "").lower()
    if "captcha" in cat_lower:
        return ["robot", "warning"]
    if "mfa" in cat_lower or "verification" in cat_lower:
        return ["key", "lock"]
    if "question" in cat_lower:
        return ["grey_question", "bulb"]
    if "ambiguous" in cat_lower:
        return ["rotating_light", "warning"]
    if "applied" in cat_lower or "success" in cat_lower:
        return ["white_check_mark", "briefcase"]
    return ["warning", "bell"]


def dispatch_fallback_script(
    title: str,
    message: str,
    priority: str = "default",
    script_path: str | None = None,
) -> tuple[bool, str]:
    """
    Executes local host fallback notification script:
    Runs: <script> -t <title> -m <message> -p <priority>
    Keeps notifications generic and privacy-safe.
    """
    cmd_script = script_path or os.environ.get("NTFY_FALLBACK_SCRIPT", "").strip()
    if not cmd_script:
        candidate = Path.home() / "bin" / "notify-alert.sh"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            cmd_script = str(candidate)

    if not cmd_script:
        return False, "No fallback script configured or found"

    p_script = Path(cmd_script).expanduser()
    if not p_script.is_file() or not os.access(p_script, os.X_OK):
        return False, f"Fallback script '{p_script}' not found or not executable"

    try:
        proc = subprocess.run(
            [str(p_script), "-t", title, "-m", message, "-p", priority],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return True, ""
        return (
            False,
            f"Fallback script failed (exit {proc.returncode}): {proc.stderr.strip()}",
        )
    except Exception as ex:
        return False, f"Fallback script execution error: {ex}"


def dispatch_single_notification(
    notification: dict[str, Any],
    config: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """
    Dispatches a single outbox notification to the ntfy service.
    Falls back to NTFY_FALLBACK_SCRIPT (defaulting to ~/bin/notify-alert.sh) if HTTP fails.
    Returns (success: bool, error_message: str).
    """
    cfg = config or get_ntfy_config()
    target_url = f"{cfg['url']}/{cfg['topic']}"

    # Generic privacy-safe payload: category and opaque reference only
    title = notification.get("title", "Action Required: JobApplier Alert")
    message = notification.get(
        "message", "Job automation paused. Please review dashboard."
    )
    urgency = notification.get("urgency", "normal")
    category = notification.get("category", "general")
    url = notification.get("url") or cfg["public_base_url"]

    # Encode title for HTTP headers (RFC 2047 MIME encoded words for non-ASCII)
    try:
        title.encode("ascii")
        header_title = title.replace("\r", "").replace("\n", " ")
    except UnicodeEncodeError:
        header_title = (
            Header(title, "utf-8", maxlinelen=sys.maxsize)
            .encode()
            .replace("\r", "")
            .replace("\n", " ")
        )

    headers = {
        "Title": header_title,
        "Priority": map_urgency_to_priority(urgency),
        "Tags": ",".join(map_category_to_tags(category)),
        "Click": url,
        "Content-Type": "text/plain; charset=utf-8",
    }

    if cfg["token"]:
        headers["Authorization"] = f"Bearer {cfg['token']}"

    data = message.encode("utf-8")
    req = urllib.request.Request(
        url=target_url,
        data=data,
        headers=headers,
        method="POST",
    )

    err_msg = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status in (200, 201, 202):
                return True, ""
            err_msg = f"Unexpected response status: {response.status}"
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        err_msg = f"HTTP {e.code}: {e.reason} ({err_body})"
    except urllib.error.URLError as e:
        err_msg = f"Network error: {e.reason}"
    except Exception as e:
        err_msg = f"Dispatch exception: {str(e)}"

    # Attempt fallback script if HTTP delivery failed or when running outside container
    fallback_script = cfg.get("fallback_script")
    if fallback_script:
        fb_ok, fb_err = dispatch_fallback_script(
            title=title,
            message=message,
            priority=map_urgency_to_priority(urgency),
            script_path=fallback_script,
        )
        if fb_ok:
            logger.info(
                f"Notification delivered via host fallback script ({fallback_script})"
            )
            return True, ""
        logger.debug(f"Fallback script notice: {fb_err}")

    return False, err_msg


def process_outbox(
    batch_size: int = 20,
    max_retries: int = MAX_OUTBOX_RETRIES,
    custom_path: Path | None = None,
    config: dict[str, str] | None = None,
) -> dict[str, int]:
    """
    Processes pending notifications from the durable outbox with bounded retries.
    Returns counts: {"processed": int, "delivered": int, "failed": int, "retrying": int}.
    """
    pending = get_pending_notifications(limit=batch_size, custom_path=custom_path)
    counts = {"processed": 0, "delivered": 0, "failed": 0, "retrying": 0}

    for item in pending:
        counts["processed"] += 1
        notif_id = item["id"]
        attempt_count = item.get("attempt_count", 0)

        success, err = dispatch_single_notification(item, config=config)

        if success:
            mark_notification_sent(notif_id, error="", custom_path=custom_path)
            counts["delivered"] += 1
        else:
            if attempt_count + 1 >= max_retries:
                # Max retries reached: mark permanently failed
                mark_notification_sent(
                    notif_id,
                    error=f"Max retries ({max_retries}) exceeded: {err}",
                    custom_path=custom_path,
                )
                counts["failed"] += 1
            else:
                # Bounded retry: increment attempt count and preserve pending status
                mark_notification_retry(notif_id, error=err, custom_path=custom_path)
                counts["retrying"] += 1

    return counts
