from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from job_applier.automation.autofill_script import (  # type: ignore[import-not-found]
    save_autofill_assets,
)
from job_applier.automation.browser_automator import (  # type: ignore[import-not-found]
    BrowserAutomator,
)
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    load_candidate_profile,
)
from job_applier.tracker import (  # type: ignore[import-not-found]
    get_tracker_stats,
    print_tracker_summary,
    record_application,
)
from job_applier.utils import get_project_root, parse_app_folder_info

# Clipboard support check
try:
    import pyperclip  # type: ignore[import-not-found, import-untyped]

    HAS_CLIPBOARD = True
except ImportError:
    pyperclip = None  # type: ignore[assignment]
    HAS_CLIPBOARD = False


def copy_to_clipboard(text: str) -> bool:
    """Safely copies text to system clipboard if pyperclip is available."""
    if HAS_CLIPBOARD and pyperclip is not None:
        try:
            pyperclip.copy(text)
            return True
        except Exception as e:
            print(f"Notice: Clipboard copy failed: {e}")
            return False
    return False


def open_directory_in_explorer(path: Path) -> None:
    """Opens directory in cross-platform system file manager."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
        print(f" Opened folder: {path}")
    except Exception as e:
        print(f" Could not open folder: {e}")


def run_batch_auto_apply(
    apps: list[Path],
    batch_size: int = 5,
    autonomous: bool = False,
    headless: bool = False,
    browser: str | None = None,
) -> None:
    """Runs automated browser application sequentially across a batch of applications."""
    project_root = get_project_root()
    applied_dir = project_root / "output" / "applied"
    applied_dir.mkdir(parents=True, exist_ok=True)

    automator = BrowserAutomator(headless=headless, browser=browser)
    processed = 0

    print(
        f"\n🚀 Starting Batch Auto-Apply ({'Autonomous' if autonomous else 'Assisted'}) for {min(batch_size, len(apps))} jobs..."
    )

    try:
        automator.start()
        for i, app_folder in enumerate(apps[:batch_size], 1):
            info = parse_app_folder_info(app_folder)
            print(
                f"\n[{i}/{min(batch_size, len(apps))}] Processing: {info['company']} - {info['title']}"
            )

            if not info["job_url"].startswith("http"):
                print("  Skipping: Invalid or missing URL.")
                continue

            if autonomous:
                status, msg = automator.run_autonomous_apply(
                    app_dir=app_folder,
                    job_url=info["job_url"],
                    company=info["company"],
                    job_title=info["title"],
                )
            else:
                status, msg = automator.run_assisted_apply(
                    app_dir=app_folder,
                    job_url=info["job_url"],
                    company=info["company"],
                    job_title=info["title"],
                )

            if status == "applied":
                target_dir = applied_dir / app_folder.name
                shutil.move(str(app_folder), str(target_dir))
                print(f" Application completed and moved to: {target_dir.name}")
                processed += 1
            elif status == "skipped":
                print(" Application skipped.")
            else:
                print(f" Notice: {msg}")

    finally:
        automator.close()

    print(
        f"\n Batch Auto-Apply Finished. Total successfully applied: {processed}/{min(batch_size, len(apps))}"
    )


def apply_assistant(
    filter_keyword: str | None = None,
    auto_mode: bool = False,
    autonomous: bool = False,
    batch_count: int | None = None,
    headless: bool = False,
    browser: str | None = None,
) -> None:
    project_root = get_project_root()
    applications_dir = project_root / "output" / "applications"
    applied_dir = project_root / "output" / "applied"
    applied_dir.mkdir(parents=True, exist_ok=True)

    if not applications_dir.exists():
        print(
            "\n No pending applications found! Run the orchestrator to generate more."
        )
        return

    all_apps = sorted([d for d in applications_dir.iterdir() if d.is_dir()])
    if not all_apps:
        print(
            "\n No pending applications found! Run the orchestrator to generate more."
        )
        return

    # Filter applications if requested
    if filter_keyword:
        filtered = [d for d in all_apps if filter_keyword.lower() in d.name.lower()]
        print(
            f" Filter '{filter_keyword}': matched {len(filtered)} of {len(all_apps)} applications."
        )
        all_apps = filtered
        if not all_apps:
            return

    # If batch count or auto mode requested from CLI, execute directly
    if batch_count or auto_mode:
        count = batch_count if batch_count else len(all_apps)
        run_batch_auto_apply(
            all_apps,
            batch_size=count,
            autonomous=autonomous,
            headless=headless,
            browser=browser,
        )
        return

    # Load profile and prepare reusable automator
    candidate_profile = load_candidate_profile()
    browser_automator: BrowserAutomator | None = None

    stats = get_tracker_stats()
    print("\n===============================================================")
    print(" 🚀 Smart Application Assistant & Form Autofill Automator")
    print("===============================================================")
    print(
        f" Pending: {len(all_apps)} | Applied so far: {stats['applied']} | Auto-filled: {stats['auto_filled']}"
    )
    print("---------------------------------------------------------------")

    current_idx = 0

    while current_idx < len(all_apps):
        app_folder = all_apps[current_idx]
        if not app_folder.exists():
            current_idx += 1
            continue

        info = parse_app_folder_info(app_folder)

        # Ensure 1-click autofill bookmarklet assets exist in application folder
        save_autofill_assets(
            app_dir=app_folder,
            profile=candidate_profile,
            cover_letter=info["cover_letter_text"],
            cv_path=info["cv_path"],
        )

        print(
            f"\n[{current_idx + 1}/{len(all_apps)}] Application: {info['company']} - {info['title']}"
        )
        print(f"  URL:          {info['job_url']}")
        print(f"  CV:           {info['cv_name']}")
        print(
            f"  Cover Letter: {info['has_cover_letter']} ({len(info['cover_letter_text'])} chars)"
        )

        while True:
            print("\n  Actions:")
            print(
                "    [a]  ⚡ Auto-Apply / Auto-Fill with Browser (Assisted Mode - Recommended)"
            )
            print("    [A]  🤖 Auto-Apply Autonomous (Full Auto-Submit)")
            print("    [b]  📦 Batch Auto-Apply (Run next N jobs sequentially)")
            print(
                "    [o]  🌐 Quick Open URL (Copies cover letter & CV path to clipboard)"
            )
            print("    [c]  📋 Copy Cover Letter to Clipboard")
            print("    [r]  📄 Copy Resume PDF Path to Clipboard")
            print("    [p]  👁️  Preview Cover Letter")
            print("    [f]  📂 Open Application Folder")
            print("    [d]  ✅ Mark as DONE (Move to 'applied' & update tracker)")
            print("    [s]  ⏭️  Skip to Next")
            print("    [/]  🔍 Filter/Search queue")
            print("    [t]  📊 View Tracker Statistics")
            print("    [q]  ❌ Quit")

            try:
                choice = input("  Select action: ").strip()
            except EOFError:
                choice = "q"

            if choice == "a":
                # Assisted Auto-Apply
                if browser_automator is None:
                    browser_automator = BrowserAutomator(
                        profile=candidate_profile, headless=headless, browser=browser
                    )
                if not browser_automator:
                    continue
                status, msg = browser_automator.run_assisted_apply(
                    app_dir=app_folder,
                    job_url=info["job_url"],
                    company=info["company"],
                    job_title=info["title"],
                )
                if status == "applied":
                    target_dir = applied_dir / app_folder.name
                    try:
                        shutil.move(str(app_folder), str(target_dir))
                        print(f" Application completed and moved to: {target_dir.name}")
                    except Exception as e:
                        print(f" Notice: Could not move folder to {target_dir}: {e}")
                    current_idx += 1
                    break
                elif status == "skipped":
                    current_idx += 1
                    break

            elif choice == "A":
                # Autonomous Auto-Apply
                if browser_automator is None:
                    browser_automator = BrowserAutomator(
                        profile=candidate_profile, headless=headless, browser=browser
                    )
                if not browser_automator:
                    continue
                status, msg = browser_automator.run_autonomous_apply(
                    app_dir=app_folder,
                    job_url=info["job_url"],
                    company=info["company"],
                    job_title=info["title"],
                )
                if status == "applied":
                    target_dir = applied_dir / app_folder.name
                    try:
                        shutil.move(str(app_folder), str(target_dir))
                        print(
                            f" Application auto-submitted and moved to: {target_dir.name}"
                        )
                    except Exception as e:
                        print(f" Notice: Could not move folder to {target_dir}: {e}")
                    current_idx += 1
                    break
                else:
                    print(f" Autonomous apply result: {msg}")

            elif choice.lower() == "b":
                try:
                    num_str = input(
                        "  How many applications to process in batch? [default 5]: "
                    ).strip()
                    num = int(num_str) if num_str else 5
                except ValueError:
                    num = 5
                auto_choice = (
                    input(
                        "  Run autonomous (auto-submit) or assisted? [assisted/auto]: "
                    )
                    .strip()
                    .lower()
                )
                is_auto = auto_choice in ["auto", "autonomous", "a"]
                run_batch_auto_apply(
                    all_apps[current_idx:],
                    batch_size=num,
                    autonomous=is_auto,
                    headless=headless,
                    browser=browser,
                )
                # Refresh app list after batch
                all_apps = sorted([d for d in applications_dir.iterdir() if d.is_dir()])
                break

            elif choice.lower() == "o":
                if info["job_url"].startswith("http"):
                    print(f" Opening {info['job_url']}...")
                    webbrowser.open(info["job_url"])
                    # Auto-copy cover letter and show CV path
                    if info["cover_letter_text"]:
                        copy_to_clipboard(info["cover_letter_text"])
                        print(" Cover letter copied to clipboard automatically!")
                    if info["cv_path"]:
                        print(f" CV Path: {info['cv_path']}")
                else:
                    print(" Invalid URL.")

            elif choice.lower() == "c":
                if info["cover_letter_text"]:
                    if copy_to_clipboard(info["cover_letter_text"]):
                        print(" Cover letter copied to clipboard!")
                    else:
                        print(" Notice: Install pyperclip or copy manually.")
                else:
                    print(" No cover letter found.")

            elif choice.lower() == "r":
                if info["cv_path"]:
                    if copy_to_clipboard(info["cv_path"]):
                        print(f" CV Path copied to clipboard: {info['cv_path']}")
                    else:
                        print(f" CV Path: {info['cv_path']}")
                else:
                    print(" No CV PDF found.")

            elif choice.lower() == "p":
                if info["cover_letter_text"]:
                    print("\n--- Cover Letter Preview ---")
                    print(info["cover_letter_text"])
                    print("----------------------------")
                else:
                    print(" No cover letter available.")

            elif choice.lower() == "f":
                open_directory_in_explorer(app_folder)

            elif choice.lower() == "d":
                target_dir = applied_dir / app_folder.name
                try:
                    shutil.move(str(app_folder), str(target_dir))
                except Exception as e:
                    print(f" Notice: Could not move folder to {target_dir}: {e}")
                record_application(
                    company=info["company"],
                    title=info["title"],
                    job_url=info["job_url"],
                    status="applied",
                    submission_type="manual_assistant",
                    cv_path=info["cv_path"],
                )
                print(
                    f" Application moved to {target_dir.name} and recorded in tracker!"
                )
                current_idx += 1
                break

            elif choice.lower() == "s":
                print(" Skipping...")
                current_idx += 1
                break

            elif choice.strip() == "/":
                kw = input(" Enter search term (company or title): ").strip()
                if kw:
                    all_apps = sorted(
                        [
                            d
                            for d in applications_dir.iterdir()
                            if d.is_dir() and kw.lower() in d.name.lower()
                        ]
                    )
                    current_idx = 0
                    print(f" Found {len(all_apps)} matching applications.")
                    break

            elif choice.lower() == "t":
                print_tracker_summary()

            elif choice.lower() == "q":
                print("\n Exiting Assistant. Good luck with your applications!")
                if browser_automator:
                    browser_automator.close()
                return

            else:
                print(" Invalid option. Please select a valid key.")

    if browser_automator:
        browser_automator.close()

    print("\n All pending applications in queue processed!")
    print_tracker_summary()


def main():
    parser = argparse.ArgumentParser(
        description="Smart Job Application Assistant and Auto-Applier"
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Automatically run assisted auto-apply on pending queue",
    )
    parser.add_argument(
        "--autonomous", action="store_true", help="Run full autonomous auto-submit mode"
    )
    parser.add_argument(
        "--batch", type=int, help="Number of applications to process in batch"
    )
    parser.add_argument(
        "--filter", dest="filter_kw", help="Filter applications by company or title"
    )
    parser.add_argument(
        "--headless", action="store_true", help="Run browser in headless mode"
    )
    parser.add_argument(
        "--browser",
        choices=["auto", "chrome", "chromium", "firefox"],
        default=None,
        help="Browser engine to use (auto, chrome, chromium, firefox)",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show application tracker statistics and exit",
    )

    args = parser.parse_args()

    if args.stats:
        print_tracker_summary()
        return

    apply_assistant(
        filter_keyword=args.filter_kw,
        auto_mode=args.auto,
        autonomous=args.autonomous,
        batch_count=args.batch,
        headless=args.headless,
        browser=args.browser,
    )


if __name__ == "__main__":
    main()
