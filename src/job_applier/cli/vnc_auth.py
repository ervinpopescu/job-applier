"""Runtime-generated x11vnc authentication material."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from job_applier.utils import get_secret

VNC_PASSWORD_ENV = "VNC_PASSWORD"
VNC_PASSWORD_MAX_BYTES = 8


def load_vnc_password() -> str:
    """Load and validate the shared classic-VNC password without logging it."""
    password = get_secret(VNC_PASSWORD_ENV)
    if not password:
        raise RuntimeError(
            "VNC_PASSWORD is required (set it in the deployment environment or Docker secret)"
        )
    try:
        encoded = password.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError("VNC_PASSWORD must contain only ASCII characters") from exc
    if not 1 <= len(encoded) <= VNC_PASSWORD_MAX_BYTES:
        raise RuntimeError("VNC_PASSWORD must be between 1 and 8 ASCII bytes")
    if any(char in encoded for char in (0, 10, 13)):
        raise RuntimeError("VNC_PASSWORD must not contain NUL or newline characters")
    return password


def create_vnc_password_file(password: str) -> Path:
    """Create an x11vnc encrypted password file with mode 0600.

    x11vnc's ``-rfbauth`` format is not plaintext.  Its own password-store
    command performs the classic VNC DES transformation, while stdin keeps the
    secret out of process arguments and captured output.
    """
    password = _validate_password(password)
    with tempfile.TemporaryDirectory(prefix="job-applier-vnc-store-") as home:
        home_path = Path(home)
        (home_path / ".vnc").mkdir(mode=0o700)
        env = {
            key: value
            for key, value in os.environ.items()
            if key.lower() != VNC_PASSWORD_ENV.lower()
        }
        env["HOME"] = home
        result = subprocess.run(
            ["x11vnc", "-storepasswd"],
            input=f"{password}\n{password}\ny\n",
            text=True,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        source = home_path / ".vnc" / "passwd"
        if result.returncode != 0 or not source.is_file():
            raise RuntimeError(
                "x11vnc could not create its password file; verify x11vnc is installed"
            )

        fd, name = tempfile.mkstemp(prefix="job-applier-vnc-", suffix=".passwd")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as destination:
                destination.write(source.read_bytes())
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            os.close(fd)
            Path(name).unlink(missing_ok=True)
            raise
        return Path(name)


def _validate_password(password: str) -> str:
    """Validate an already-loaded password before passing it to x11vnc."""
    try:
        encoded = password.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError("VNC_PASSWORD must contain only ASCII characters") from exc
    if not 1 <= len(encoded) <= VNC_PASSWORD_MAX_BYTES:
        raise RuntimeError("VNC_PASSWORD must be between 1 and 8 ASCII bytes")
    if any(char in encoded for char in (0, 10, 13)):
        raise RuntimeError("VNC_PASSWORD must not contain NUL or newline characters")
    return password


def vnc_process_environment() -> dict[str, str]:
    """Return a child environment that does not expose the source secret."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.lower() != VNC_PASSWORD_ENV.lower()
    }


def cleanup_vnc_password_file(path: Path | None) -> None:
    """Remove generated authentication material without exposing its contents."""
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
