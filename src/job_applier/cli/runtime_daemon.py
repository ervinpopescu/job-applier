from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from job_applier.automation.browser_runtime import get_default_profile_dir, is_linux
from job_applier.automation.network_security import OutboundSecurityProxy
from job_applier.automation.queue import record_event
from job_applier.automation.profile_lock import (
    ProfileOwnershipError,
    ProfileOwnershipLock,
)
from job_applier.automation.runtime_lock import RuntimeSingletonLock, WorkerLockError
from job_applier.cli.worker import run_worker_loop
from job_applier.db import get_connection, init_db


class RuntimeDaemon:
    """
    Supervises the autonomous runtime container environment:
    - Singleton worker OS lock
    - Exclusive profile ownership lock
    - Xvfb virtual display
    - x11vnc loopback read-only default viewer transport
    - websockify WebSocket-to-VNC bridge
    - Enforceable outbound security proxy
    - Automation worker loop
    """

    def __init__(
        self,
        display: str = ":99",
        vnc_port: int = 5900,
        websockify_port: int = 6080,
        proxy_port: int = 8899,
        poll_interval: float = 3.0,
        browser_engine: str = "chromium",
        profile_dir: Path | None = None,
        novnc_dir: Path | str | None = None,
        custom_db_path: Path | None = None,
        worker_shutdown_timeout: float = 10.0,
    ):
        self.display = display
        self.vnc_port = vnc_port
        self.websockify_port = websockify_port
        self.proxy_port = proxy_port
        self.poll_interval = poll_interval
        self.browser_engine = browser_engine
        self.profile_dir = profile_dir or get_default_profile_dir(browser_engine)
        self.novnc_dir = (
            Path(novnc_dir)
            if novnc_dir
            else Path(os.environ.get("NOVNC_DIR", "/usr/share/novnc"))
        )
        self.custom_db_path = custom_db_path
        self.worker_shutdown_timeout = worker_shutdown_timeout

        self.singleton_lock = RuntimeSingletonLock()
        self.profile_lock = ProfileOwnershipLock(self.profile_dir, owner_type="runtime")
        self.outbound_proxy = OutboundSecurityProxy(port=self.proxy_port)

        self.xvfb_proc: subprocess.Popen[Any] | None = None
        self.wallpaper_proc: subprocess.Popen[Any] | None = None
        self.vnc_proc: subprocess.Popen[Any] | None = None
        self.websockify_proc: subprocess.Popen[Any] | None = None
        self._stop_requested = False
        self._shutdown_requested = threading.Event()
        self._worker_stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

    def start_display_subsystem(self) -> None:
        """Starts Xvfb, x11vnc (view-only default), and websockify if on Linux."""
        if not is_linux():
            return

        if all(
            proc is not None and proc.poll() is None
            for proc in (self.xvfb_proc, self.vnc_proc, self.websockify_proc)
        ):
            # Startup may be retried after an authentication/browser reopen.
            # Keep the existing supervised display stack singleton.
            return

        # 1. Start Xvfb virtual display
        if self.xvfb_proc is not None and self.xvfb_proc.poll() is None:
            os.environ["DISPLAY"] = self.display
        elif shutil.which("Xvfb"):
            xvfb_cmd = [
                "Xvfb",
                self.display,
                "-screen",
                "0",
                "1920x1080x24",
                "-nolisten",
                "tcp",
                "-ac",
            ]
            try:
                self.xvfb_proc = subprocess.Popen(
                    xvfb_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.5)
                os.environ["DISPLAY"] = self.display
                print(f"🖥️ Xvfb virtual display active on {self.display}")

                # 1b. Start idle background wallpaper on virtual display
                try:
                    wallpaper_script = (
                        "import os, tkinter as tk; "
                        "os.environ['XDG_CACHE_HOME'] = '/tmp/cache'; "
                        "root = tk.Tk(); "
                        "root.title('Job Applier Desktop'); "
                        "root.configure(bg='#020617'); "
                        "root.geometry('1920x1080+0+0'); "
                        "frame = tk.Frame(root, bg='#020617'); "
                        "frame.place(relx=0.5, rely=0.5, anchor='center'); "
                        "tk.Label(frame, text='Job Applier Browser Runtime — Display :99 Active', "
                        "font=('Helvetica', 24, 'bold'), fg='#38bdf8', bg='#020617').pack(pady=10); "
                        "tk.Label(frame, text='Status: Idle / Waiting for application or login session', "
                        "font=('Helvetica', 16), fg='#94a3b8', bg='#020617').pack(pady=5); "
                        "root.mainloop()"
                    )
                    self.wallpaper_proc = subprocess.Popen(
                        [sys.executable, "-c", wallpaper_script],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    print("🎨 Idle desktop wallpaper active on virtual display")
                except Exception as we:
                    print(f"Notice: Failed starting desktop wallpaper: {we}")
            except Exception as e:
                print(f"Notice: Failed starting Xvfb: {e}")
        else:
            print("Notice: Xvfb binary not found; using ambient display if available.")

        # 2. Start x11vnc: Loopback only
        if self.vnc_proc is None or self.vnc_proc.poll() is not None:
            if shutil.which("x11vnc"):
                vnc_cmd = [
                    "x11vnc",
                    "-display",
                    self.display,
                    "-rfbport",
                    str(self.vnc_port),
                    "-localhost",  # Loopback only
                    "-forever",
                    "-shared",
                    "-nopw",
                ]
                try:
                    self.vnc_proc = subprocess.Popen(
                        vnc_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    print(f"🔒 x11vnc active on 127.0.0.1:{self.vnc_port}")
                except Exception as e:
                    print(f"Notice: Failed starting x11vnc: {e}")
            else:
                print("Notice: x11vnc binary not found.")

        # 3. Start websockify: Bridges internal WebSocket transport to local x11vnc
        if self.websockify_proc is None or self.websockify_proc.poll() is not None:
            if shutil.which("websockify"):
                ws_cmd = ["websockify"]
                if self.novnc_dir and self.novnc_dir.is_dir():
                    browser_symlink = self.novnc_dir / "browser"
                    if not browser_symlink.exists():
                        try:
                            browser_symlink.symlink_to(".", target_is_directory=True)
                        except Exception:
                            pass
                    ws_cmd.extend(["--web", str(self.novnc_dir)])
                ws_cmd.extend(
                    [
                        f"0.0.0.0:{self.websockify_port}",
                        f"127.0.0.1:{self.vnc_port}",
                    ]
                )
                try:
                    self.websockify_proc = subprocess.Popen(
                        ws_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        preexec_fn=os.setsid,
                    )
                    print(
                        f"🌐 websockify active on port {self.websockify_port} -> 127.0.0.1:{self.vnc_port}"
                    )
                except Exception as e:
                    print(f"Notice: Failed starting websockify: {e}")
            else:
                print("Notice: websockify binary not found.")

    def _reconcile_inflight_worker(self, worker_id: str) -> list[str]:
        """Fence and reconcile jobs left owned if a worker cannot stop in time."""
        init_db(self.custom_db_path)
        conn = get_connection(self.custom_db_path)
        reconciled: list[str] = []
        try:
            with conn:
                rows = conn.execute(
                    """
                    SELECT id, app_id, state
                    FROM automation_jobs
                    WHERE lease_owner = ?
                      AND state NOT IN ('applied', 'cancelled', 'failed_permanent', 'skipped');
                    """,
                    (worker_id,),
                ).fetchall()
                for row in rows:
                    state = row["state"]
                    if state in {"submit_intent", "verifying"}:
                        next_state = "ambiguous_submission"
                        error_code = "SHUTDOWN_DURING_SUBMISSION"
                        message = (
                            "Runtime shutdown interrupted an in-flight submission; "
                            "manual outcome verification is required."
                        )
                        conn.execute(
                            """
                            UPDATE applications
                            SET status = 'ambiguous', notes = ?, updated_at = datetime('now')
                            WHERE id = ?;
                            """,
                            (message, row["app_id"]),
                        )
                        conn.execute(
                            """
                            UPDATE application_attempts
                            SET outcome = 'ambiguous', error_details = ?,
                                is_ambiguous = 1, completed_at = datetime('now')
                            WHERE id = (
                                SELECT id FROM application_attempts
                                WHERE job_id = ? AND completed_at IS NULL
                                ORDER BY created_at DESC LIMIT 1
                            );
                            """,
                            (message, row["id"]),
                        )
                    elif state in {
                        "auth_required",
                        "mfa_required",
                        "captcha_required",
                        "unknown_question",
                    }:
                        next_state = state
                        error_code = "RUNTIME_SHUTDOWN"
                        message = (
                            "Runtime shutdown closed the browser session; "
                            "operator authentication must be reopened."
                        )
                    else:
                        next_state = "ready"
                        error_code = "RUNTIME_SHUTDOWN"
                        message = "Runtime shutdown interrupted pre-submit automation; job was safely returned to the queue."

                    updated = conn.execute(
                        """
                        UPDATE automation_jobs
                        SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                            fencing_generation = fencing_generation + 1,
                            checkpoint = 'runtime_shutdown', error_code = ?,
                            error_message = ?, updated_at = datetime('now')
                        WHERE id = ? AND lease_owner = ?;
                        """,
                        (
                            next_state,
                            error_code,
                            message,
                            row["id"],
                            worker_id,
                        ),
                    ).rowcount
                    if updated:
                        record_event(
                            job_id=row["id"],
                            app_id=row["app_id"],
                            event_type="runtime_shutdown_reconciled",
                            level="WARN",
                            step=next_state,
                            message=message,
                            details={"previous_state": state, "worker_id": worker_id},
                            custom_path=self.custom_db_path,
                            conn=conn,
                        )
                        reconciled.append(row["id"])
        finally:
            conn.close()
        return reconciled

    def shutdown(self) -> None:
        """Stops the worker before terminating supervised child processes."""
        print("\n🛑 Shutting down runtime daemon and releasing resources...")
        self._stop_requested = True
        self._worker_stop_event.set()

        worker_thread = self._worker_thread
        if worker_thread and worker_thread.is_alive():
            worker_thread.join(timeout=self.worker_shutdown_timeout)
        worker_id = getattr(self, "_worker_id", "")
        if worker_id:
            reconciled = self._reconcile_inflight_worker(worker_id)
            if reconciled:
                print(
                    f"⚠️ Reconciled {len(reconciled)} in-flight job(s) after worker shutdown."
                )

        try:
            self.outbound_proxy.stop()
        except Exception:
            pass

        for proc_name, proc in [
            ("wallpaper", self.wallpaper_proc),
            ("websockify", self.websockify_proc),
            ("x11vnc", self.vnc_proc),
            ("Xvfb", self.xvfb_proc),
        ]:
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        try:
            self.profile_lock.release()
        except Exception:
            pass

        try:
            self.singleton_lock.release()
        except Exception:
            pass

        print("👋 Runtime daemon terminated cleanly.")

    def run(self) -> int:
        """Executes the full runtime lifecycle."""
        # 1. Acquire singleton OS worker lock
        try:
            if not self.singleton_lock.acquire():
                print("❌ Another runtime daemon is already active (singleton lock).")
                return 1
        except WorkerLockError as e:
            print(f"❌ Failed acquiring runtime singleton lock: {e}")
            return 1

        # 2. Acquire exclusive profile lock
        try:
            self.profile_lock.acquire()
            print(f"📦 Acquired exclusive profile lock for {self.profile_dir}")
        except ProfileOwnershipError as e:
            print(f"❌ Profile lock acquisition failed: {e}")
            self.singleton_lock.release()
            return 1

        # 3. Start display subsystem
        self.start_display_subsystem()

        # 4. Start outbound security proxy
        try:
            self.outbound_proxy.start()
            os.environ["JOB_APPLIER_OUTBOUND_PROXY"] = self.outbound_proxy.proxy_url
            print(
                f"🛡️ Outbound security proxy active on {self.outbound_proxy.proxy_url}"
            )
        except Exception as e:
            print(f"❌ Failed starting outbound security proxy: {e}")
            self.shutdown()
            return 1

        # Set runtime mode
        os.environ["JOB_APPLIER_RUNTIME_MODE"] = "service"

        def _handle_sig(sig: int, frame: Any) -> None:
            print(
                f"\n🛑 Shutdown signal received ({sig}); stopping worker before display teardown..."
            )
            self._shutdown_requested.set()
            self._worker_stop_event.set()

        signal.signal(signal.SIGINT, _handle_sig)
        signal.signal(signal.SIGTERM, _handle_sig)

        # 5. Run the synchronous Playwright worker outside the daemon's main
        # thread. Playwright's sync API refuses to start when the current thread
        # owns an asyncio event loop (for example when embedded by a supervisor).
        # Keeping the worker in a dedicated thread also leaves daemon lifecycle
        # and display supervision responsive.
        worker_result: list[int] = []
        worker_error: list[BaseException] = []
        self._worker_id = f"runtime_{os.getpid()}_{threading.get_native_id()}"

        def run_worker() -> None:
            try:
                worker_result.append(
                    run_worker_loop(
                        poll_interval=self.poll_interval,
                        headless=False,
                        browser_engine=self.browser_engine,
                        skip_lock=True,
                        worker_id=self._worker_id,
                        custom_db_path=self.custom_db_path,
                        stop_event=self._worker_stop_event,
                    )
                )
            except BaseException as exc:
                worker_error.append(exc)

        worker_thread = threading.Thread(
            target=run_worker,
            name="job-applier-worker",
            daemon=True,
        )
        self._worker_thread = worker_thread
        worker_thread.start()
        try:
            while worker_thread.is_alive() and not self._shutdown_requested.is_set():
                worker_thread.join(timeout=0.5)
            if worker_error:
                raise worker_error[0]
            return worker_result[0] if worker_result else 0
        finally:
            self.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Job Applier Runtime Daemon")
    parser.add_argument(
        "--display",
        default=os.environ.get("DISPLAY", ":99"),
        help="Virtual display identifier",
    )
    parser.add_argument(
        "--vnc-port",
        type=int,
        default=int(os.environ.get("VNC_PORT", "5900")),
        help="x11vnc port (bound to localhost)",
    )
    parser.add_argument(
        "--websockify-port",
        type=int,
        default=int(os.environ.get("WEBSOCKIFY_PORT", "6080")),
        help="websockify internal bridge port",
    )
    parser.add_argument(
        "--proxy-port",
        type=int,
        default=int(os.environ.get("PROXY_PORT", "8899")),
        help="Outbound security proxy port",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=3.0,
        help="Queue poll interval in seconds",
    )
    parser.add_argument(
        "--novnc-dir",
        default=os.environ.get("NOVNC_DIR", "/usr/share/novnc"),
        help="Directory containing noVNC web assets",
    )
    parser.add_argument(
        "--browser",
        default="chromium",
        choices=["chromium", "firefox", "chrome"],
        help="Browser engine",
    )

    args = parser.parse_args()

    daemon = RuntimeDaemon(
        display=args.display,
        vnc_port=args.vnc_port,
        websockify_port=args.websockify_port,
        proxy_port=args.proxy_port,
        poll_interval=args.poll_interval,
        browser_engine=args.browser,
        novnc_dir=args.novnc_dir,
    )
    code = daemon.run()
    sys.exit(code)


if __name__ == "__main__":
    main()
