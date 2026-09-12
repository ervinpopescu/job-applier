from __future__ import annotations

import fcntl
import os
from pathlib import Path

from job_applier.utils import get_project_root


class WorkerLockError(Exception):
    """Raised when an automation worker process cannot acquire the singleton OS lock."""


class RuntimeSingletonLock:
    """
    Enforces a single running worker process on the host via OS-level file locking.
    Uses POSIX fcntl.flock which automatically releases even if the process crashes.
    """

    def __init__(self, lock_path: Path | None = None):
        if lock_path is None:
            self.lock_path = get_project_root() / "data" / ".worker.lock"
        else:
            self.lock_path = Path(lock_path)
        self._fd: int | None = None
        self._is_locked: bool = False

    def acquire(self) -> bool:
        if self._is_locked:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._is_locked = True
            os.ftruncate(self._fd, 0)
            os.write(self._fd, f"{os.getpid()}\n".encode("utf-8"))
            return True
        except (BlockingIOError, OSError):
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            self._is_locked = False
            return False

    def release(self) -> None:
        if self._is_locked and self._fd is not None:
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

    def __enter__(self) -> RuntimeSingletonLock:
        if not self.acquire():
            raise WorkerLockError(
                f"Another automation worker is already running (locked at {self.lock_path})."
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()
