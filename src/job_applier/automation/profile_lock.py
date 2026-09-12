from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


class ProfileOwnershipError(Exception):
    """Raised when browser profile directory is locked or owned by another process/service."""

    pass


class ProfileOwnershipLock:
    """
    Enforces exclusive ownership of the browser profile directory via OS-level file locking.
    Distinguishes 'runtime' (the autonomous background daemon) from 'cli' (interactive operator tools).
    If a runtime service owns the profile, CLI automation strictly refuses to proceed.
    """

    def __init__(
        self,
        profile_dir: Path,
        owner_type: str = "runtime",
        owner_id: str | None = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.owner_type = owner_type.strip().lower()
        self.owner_id = owner_id or f"{self.owner_type}_{os.getpid()}"
        self.lock_file = self.profile_dir / ".profile_ownership.lock"
        self._fd: int | None = None
        self._is_locked: bool = False

    def acquire(self) -> bool:
        """
        Attempts to acquire an exclusive non-blocking lock on the profile directory.
        Raises ProfileOwnershipError if the profile is owned by another process,
        giving a descriptive error when runtime owns it and CLI attempts access.
        """
        if self._is_locked:
            return True

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._is_locked = True

            meta = {
                "owner_type": self.owner_type,
                "owner_id": self.owner_id,
                "pid": os.getpid(),
                "acquired_at": datetime.now().isoformat(),
            }
            os.ftruncate(self._fd, 0)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, json.dumps(meta).encode("utf-8"))
            return True

        except (BlockingIOError, OSError) as e:
            existing_info: dict[str, Any] = {}
            if self.lock_file.exists():
                try:
                    content = self.lock_file.read_text(encoding="utf-8").strip()
                    if content:
                        existing_info = json.loads(content)
                except Exception:
                    pass

            existing_owner = existing_info.get("owner_type", "unknown")
            existing_pid = existing_info.get("pid", "unknown")

            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            self._is_locked = False

            if existing_owner == "runtime" and self.owner_type == "cli":
                raise ProfileOwnershipError(
                    f"Browser profile '{self.profile_dir}' is exclusively owned by the running "
                    f"automation runtime service (PID {existing_pid}). CLI automation cannot proceed "
                    f"while the runtime service is active. Please use the web dashboard or stop the runtime service."
                ) from e

            raise ProfileOwnershipError(
                f"Browser profile '{self.profile_dir}' is already locked by {existing_owner} (PID {existing_pid})."
            ) from e

    def release(self) -> None:
        """Releases the exclusive profile lock."""
        if self._is_locked and self._fd is not None:
            try:
                os.ftruncate(self._fd, 0)
            except OSError:
                pass
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._is_locked = False

    def is_locked(self) -> bool:
        return self._is_locked

    def __enter__(self) -> ProfileOwnershipLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()
