from __future__ import annotations

import logging
import sys
from collections import deque
from datetime import datetime

# In-memory buffer for web dashboard log streaming (capped at 300 entries)
_LOG_BUFFER: deque[dict[str, str]] = deque(maxlen=300)


class DashboardLogHandler(logging.Handler):
    """Custom logging handler that buffers clean structured logs for the web UI."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            level = record.levelname.upper()
            if level == "WARNING":
                level = "WARN"

            category = getattr(record, "category", "System")
            time_str = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")

            _LOG_BUFFER.append(
                {
                    "time": time_str,
                    "level": level,
                    "category": str(category),
                    "message": msg,
                }
            )
        except Exception as e:
            sys.stderr.write(f"[LogHandler Error] {e}\n")


def get_recent_logs(limit: int = 150) -> list[dict[str, str]]:
    """Returns the most recent buffered log entries."""
    logs_list = list(_LOG_BUFFER)
    return logs_list[-limit:]


def clear_logs() -> None:
    """Clears the log buffer."""
    _LOG_BUFFER.clear()


def log_event(
    message: str,
    level: str = "INFO",
    category: str = "Pipeline",
) -> None:
    """Logs a clean structured event to both console and dashboard buffer."""
    time_str = datetime.now().strftime("%H:%M:%S")
    clean_msg = message.strip()
    lvl = level.upper()

    # Append to web buffer
    _LOG_BUFFER.append(
        {
            "time": time_str,
            "level": lvl,
            "category": category,
            "message": clean_msg,
        }
    )

    # Formatted terminal print
    level_symbols = {
        "INFO": "ℹ️ ",
        "SUCCESS": "✅",
        "WARN": "⚠️ ",
        "ERROR": "❌",
    }
    symbol = level_symbols.get(lvl, "•")
    print(f"[{time_str}] {symbol} [{category}] {clean_msg}", flush=True)


# Configure root/library loggers to prevent messy raw spam
def setup_logging() -> None:
    """Configures project-wide logging and silences noisy third-party scrapers."""
    root = logging.getLogger()
    if not any(isinstance(h, DashboardLogHandler) for h in root.handlers):
        handler = DashboardLogHandler()
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        root.addHandler(handler)
        root.setLevel(logging.INFO)

    # Silence noisy external libraries
    for logger_name in ["urllib3", "requests", "playwright", "JobSpy"]:
        lib_logger = logging.getLogger(logger_name)
        lib_logger.setLevel(logging.WARNING)


setup_logging()
