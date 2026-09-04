from __future__ import annotations

import argparse
import webbrowser
from threading import Timer

import uvicorn


def run_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
    reload: bool = True,
) -> None:
    """Runs the FastAPI web server for Job Applier."""
    url = f"http://{host}:{port}"
    print("\n=======================================================")
    print(" 🚀 Starting Job Applier Web Dashboard")
    print(f" URL: {url}")
    print("=======================================================\n")

    if open_browser:
        Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run("job_applier.web.app:app", host=host, port=port, reload=reload)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch Job Applier Unified Web Dashboard"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to listen on (default: 8000)"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="Do not open browser automatically"
    )
    parser.add_argument("--no-reload", action="store_true", help="Disable auto-reload")

    args = parser.parse_args()
    run_server(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        reload=not args.no_reload,
    )


if __name__ == "__main__":
    main()
