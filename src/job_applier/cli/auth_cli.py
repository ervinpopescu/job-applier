from __future__ import annotations

import argparse
import sys

from job_applier.automation.auth_manager import (
    AuthManager,  # type: ignore[import-not-found]
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage platform logins (LinkedIn, BestJobs, eJobs, Google) and 2FA authentication."
    )
    subparsers = parser.add_subparsers(dest="command", help="Auth commands")

    # Status command
    status_parser = subparsers.add_parser(
        "status", help="Check platform authentication status"
    )
    status_parser.add_argument(
        "--browser",
        choices=["auto", "chrome", "chromium", "firefox"],
        default=None,
        help="Browser engine to check status for (default: auto)",
    )

    # Login command
    login_parser = subparsers.add_parser(
        "login", help="Launch interactive login session for a platform"
    )
    login_parser.add_argument(
        "platform",
        choices=["linkedin", "bestjobs", "ejobs", "google"],
        help="Target platform to log in to",
    )
    login_parser.add_argument(
        "--browser",
        choices=["auto", "chrome", "chromium", "firefox"],
        default=None,
        help="Browser engine to use for login (default: auto)",
    )
    login_parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Timeout in seconds to complete login in the browser (default: 180s)",
    )

    args = parser.parse_args()

    if not args.command or args.command == "status":
        manager = AuthManager(browser=getattr(args, "browser", None))
        report = manager.check_auth_status()
        print("\n=======================================================")
        print(
            f"🔐 Platform Authentication Status ({report.get('browser', 'auto').capitalize()})"
        )
        print("=======================================================")
        print(f"Profile Directory: {report['profile_dir']}\n")
        for plat, info in report["platforms"].items():
            status_symbol = "✅" if info["configured"] else "❌"
            print(
                f"  {status_symbol} {plat.upper():<12} | {info['status']:<14} | {info['login_url']}"
            )
        print(
            "\nTo connect an account, run: python src/job_applier/cli/auth_cli.py login <platform> [--browser <engine>]"
        )
        return

    if args.command == "login":
        manager = AuthManager(browser=args.browser)
        result = manager.launch_interactive_login(
            platform=args.platform,
            timeout_seconds=args.timeout,
            browser=args.browser,
        )
        if result.get("status") == "success":
            print(f"\n✅ {result.get('message')}")
            sys.exit(0)
        else:
            print(f"\n⚠️  {result.get('message')}")
            sys.exit(1)


if __name__ == "__main__":
    main()
