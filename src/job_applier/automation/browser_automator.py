from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.automation.browser_runtime import (
    VirtualDisplayManager,
    get_default_profile_dir,
    is_display_available,
    resolve_browser_engine,
    validate_browser_engine,
)
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
    load_candidate_profile,
)
from job_applier.automation.cloudflare import (  # type: ignore[import-not-found]
    extract_ray_id,
    is_cloudflare_challenge,
    solve_cloudflare_turnstile,
)
from job_applier.automation.question_solver import (  # type: ignore[import-not-found]
    QuestionSolver,
)
from job_applier.logger import log_event  # type: ignore[import-not-found]
from job_applier.scrapers.activity_checker import (  # type: ignore[import-not-found]
    is_job_active,
)
from job_applier.tracker import record_application  # type: ignore[import-not-found]


def clean_stale_chrome_locks(profile_dir: Path) -> None:
    """Removes stale Chrome lock symlinks ONLY if the holding process is confirmed dead."""
    lock_path = profile_dir / "SingletonLock"
    if lock_path.is_symlink() or lock_path.exists():
        try:
            target = os.readlink(str(lock_path))
            parts = target.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                pid = int(parts[1])
                try:
                    os.kill(pid, 0)
                    # Process is still actively running — do NOT kill or touch it!
                    return
                except OSError:
                    # Process is confirmed dead — safe to remove stale symlink
                    lock_path.unlink(missing_ok=True)
            else:
                lock_path.unlink(missing_ok=True)
        except OSError:
            lock_path.unlink(missing_ok=True)

    # Clean leftover socket symlinks only if lock is gone
    if not lock_path.is_symlink() and not lock_path.exists():
        for name in ["SingletonCookie", "SingletonSocket", "lockfile"]:
            lp = profile_dir / name
            try:
                if lp.is_symlink() or lp.exists():
                    lp.unlink(missing_ok=True)
            except OSError:
                pass


ACTIVE_AUTOMATOR: BrowserAutomator | None = None


def get_active_automator() -> BrowserAutomator | None:
    """Returns the currently active BrowserAutomator instance, if any."""
    return ACTIVE_AUTOMATOR


class BrowserAutomator:
    """Automates job application submission and form autofill using Playwright."""

    def __init__(
        self,
        profile: CandidateProfile | None = None,
        headless: bool | None = None,
        use_persistent_profile: bool = True,
        profile_dir: Path | None = None,
        api_key: str | None = None,
        status_callback: Callable[[str, str, str, dict[str, Any]], None] | None = None,
        browser: str | None = None,
    ):
        self.profile = profile or load_candidate_profile()
        self.status_callback = status_callback

        self.raw_browser = validate_browser_engine(browser)
        self.engine, self.executable_path = resolve_browser_engine(self.raw_browser)

        # Auto-detect display: check if an X11/Wayland/native display is available or can be provided by Xvfb
        display_ok = is_display_available()
        if not display_ok:
            disp = VirtualDisplayManager.ensure_display(allow_xvfb=True)
            if disp:
                display_ok = True

        if headless is None:
            self.headless = not display_ok
        elif not headless:
            if not display_ok:
                raise RuntimeError(
                    "Headed mode requested (headless=False), but no graphical display server "
                    "(DISPLAY or WAYLAND_DISPLAY) was detected and virtual display is unavailable. "
                    "Run in headless mode or ensure a display server is running."
                )
            self.headless = False
        else:
            self.headless = True

        self.use_persistent_profile = use_persistent_profile
        self.api_key = api_key
        self.question_solver = QuestionSolver(self.profile, api_key=self.api_key)
        self.waiting_for_code: bool = False
        self.provided_code: str | None = None

        self.profile_dir = profile_dir or get_default_profile_dir(self.engine)

        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None

    def _notify(
        self,
        step: str,
        message: str,
        level: str = "INFO",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Dispatches structured log event and triggers live status callback."""
        log_event(message, level=level, category="Auto-Apply")
        if self.status_callback:
            try:
                self.status_callback(step, message, level, details or {})
            except Exception as cb_err:
                print(f"Notice in status callback: {cb_err}")

    def start(self) -> None:
        """Starts Playwright and launches the browser with automatic fallbacks."""
        if self.context is not None:
            return

        try:
            from playwright.sync_api import (
                sync_playwright,  # type: ignore[import-not-found, import-untyped]
            )

            self.playwright = sync_playwright().start()
            global ACTIVE_AUTOMATOR
            ACTIVE_AUTOMATOR = self

            if self.engine == "firefox":
                browser_type = self.playwright.firefox
                if self.use_persistent_profile:
                    self.profile_dir.mkdir(parents=True, exist_ok=True)
                    kwargs: dict[str, Any] = {
                        "user_data_dir": str(self.profile_dir),
                        "headless": self.headless,
                        "viewport": {"width": 1280, "height": 900}
                        if not self.headless
                        else {"width": 1920, "height": 1080},
                    }
                    self.context = browser_type.launch_persistent_context(**kwargs)
                    self.page = (
                        self.context.pages[0]
                        if self.context.pages
                        else self.context.new_page()
                    )
                else:
                    self.browser = browser_type.launch(headless=self.headless)
                    self.context = self.browser.new_context(
                        viewport={"width": 1280, "height": 900}
                        if not self.headless
                        else {"width": 1920, "height": 1080},
                    )
                    self.page = self.context.new_page()
            else:
                browser_type = self.playwright.chromium
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--start-maximized",
                ]

                if self.use_persistent_profile:
                    self.profile_dir.mkdir(parents=True, exist_ok=True)
                    clean_stale_chrome_locks(self.profile_dir)
                    kwargs = {
                        "user_data_dir": str(self.profile_dir),
                        "headless": self.headless,
                        "args": launch_args,
                        "viewport": {"width": 1280, "height": 900}
                        if not self.headless
                        else {"width": 1920, "height": 1080},
                    }
                    if self.executable_path and Path(self.executable_path).exists():
                        kwargs["executable_path"] = self.executable_path

                    try:
                        self.context = browser_type.launch_persistent_context(**kwargs)
                    except Exception as pe:
                        print(
                            f"Notice: Persistent browser launch failed ({pe}). Cleaning locks and retrying..."
                        )
                        clean_stale_chrome_locks(self.profile_dir)
                        time.sleep(1)
                        # Retry with persistent context to preserve authenticated cookies
                        self.context = browser_type.launch_persistent_context(**kwargs)

                    self.page = (
                        self.context.pages[0]
                        if self.context.pages
                        else self.context.new_page()
                    )
                else:
                    kwargs = {
                        "headless": self.headless,
                        "args": launch_args,
                    }
                    if self.executable_path and Path(self.executable_path).exists():
                        kwargs["executable_path"] = self.executable_path

                    self.browser = browser_type.launch(**kwargs)
                    self.context = self.browser.new_context(
                        viewport={"width": 1280, "height": 900}
                        if not self.headless
                        else {"width": 1920, "height": 1080},
                        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    )
                    self.page = self.context.new_page()

            # Set sensible navigation timeouts
            self.page.set_default_navigation_timeout(30000)
            self.page.set_default_timeout(10000)

        except Exception as e:
            print(f"Error starting Playwright browser: {e}")
            self.close()
            raise

    def close(self) -> None:
        """Closes browser and stops Playwright engine."""
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        finally:
            self.context = None
            self.browser = None
            self.playwright = None
            self.page = None
            global ACTIVE_AUTOMATOR
            if ACTIVE_AUTOMATOR is self:
                ACTIVE_AUTOMATOR = None

    def detect_platform(self, url: str) -> str:
        """Detects the underlying ATS or job board from URL."""
        url_lower = url.lower()
        if "greenhouse.io" in url_lower or "grnh.se" in url_lower:
            return "Greenhouse"
        if "lever.co" in url_lower:
            return "Lever"
        if "myworkdayjobs.com" in url_lower or "workday" in url_lower:
            return "Workday"
        if "indeed.com" in url_lower:
            return "Indeed"
        if "linkedin.com" in url_lower:
            return "LinkedIn"
        if "ashbyhq.com" in url_lower:
            return "Ashby"
        if "smartrecruiters.com" in url_lower:
            return "SmartRecruiters"
        return "Generic"

    def navigate_and_open_form(self, job_url: str) -> bool:
        """
        Navigates to job URL and attempts to click 'Apply' / 'Apply now' if on an overview page.
        """
        if not self.page:
            self.start()

        print(f" Navigating to: {job_url}")
        try:
            self.page.goto(job_url, wait_until="domcontentloaded")
            time.sleep(2)
        except Exception as e:
            print(f" Navigation error: {e}")
            return False

        # Check if Cloudflare challenge is present
        if is_cloudflare_challenge(self.page):
            solve_cloudflare_turnstile(self.page)

        # Dismiss modal overlays and cookie consent banners
        self._dismiss_overlays()

        # Check if page indicates the job is closed or expired
        try:
            page_html = self.page.content()
            active_ok, active_reason = is_job_active(job_url, html=page_html)
            if not active_ok:
                print(f"⚠️ Page indicates job is not active: {active_reason}")
                self._notify(
                    "job_expired",
                    f"Job posting is inactive: {active_reason}",
                    level="WARN",
                )
                return False
        except Exception:
            pass

        # If it opened a new tab/popup upon clicking an apply button
        initial_pages_count = len(self.context.pages)

        # Check for common "Apply" / "Easy Apply" / "Apply on company website" buttons
        apply_selectors = [
            # Romanian Platforms (BestJobs, eJobs, Hipo, Undelucram)
            "button:has-text('Aplică & Începe interviul')",
            "a:has-text('Aplică & Începe interviul')",
            "button:has-text('Aplică extern')",
            "a:has-text('Aplică extern')",
            "button:has-text('Aplică acum')",
            "a:has-text('Aplică acum')",
            "button:has-text('Aplică')",
            "a:has-text('Aplică')",
            "button:has-text('Aplica')",
            "a:has-text('Aplica')",
            "button:has-text('Trimite CV')",
            "a:has-text('Trimite CV')",
            "[data-test*='apply']",
            # Standard English & International ATS
            "#indeedApplyButton",
            "[id*='indeedApply']",
            "button:has-text('Apply now')",
            "a:has-text('Apply now')",
            "button:has-text('Apply on company website')",
            "a:has-text('Apply on company website')",
            "a:has-text('Apply Now')",
            "button:has-text('Apply Now')",
            "button:has-text('Easy Apply')",
            "a:has-text('Apply for this job')",
            "button:has-text('Apply for this job')",
            "a[href*='apply']:not([href*='login']):not([href*='help'])",
            "a:has-text('Apply')",
            "button:has-text('Apply')",
            "[data-automation-id='adventureButton']",
            "[data-automation-id='applyButton']",
        ]

        # Only click apply if we are not already on an application form page
        has_form = (
            self.page.locator(
                "form, input[type=file], input#email, input[name=email]"
            ).count()
            > 0
        )
        if not has_form:
            for selector in apply_selectors:
                try:
                    locator = self.page.locator(selector).first
                    if locator.is_visible(timeout=1000):
                        print(f" Clicking '{selector}' to open application form...")
                        locator.click()
                        time.sleep(3)
                        # Check if a new tab was opened
                        if len(self.context.pages) > initial_pages_count:
                            self.page = self.context.pages[-1]
                            self.page.bring_to_front()
                            time.sleep(2)
                        break
                except Exception:
                    continue

        return True

    def _dismiss_overlays(self) -> None:
        """Dismisses modal overlays (LinkedIn sign-in prompts) and cookie banners."""
        try:
            # Remove modal scrims and sign-in containers that intercept clicks
            self.page.evaluate("""() => {
                document.querySelectorAll('.top-level-modal-container, .modal__overlay, .contextual-sign-in-modal, [data-test-modal-id]').forEach(el => el.remove());
            }""")
        except Exception:
            pass

        # Click accept on cookie consent dialogs if present
        for sel in [
            "button#onetrust-accept-btn-handler",
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Accept Cookies')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
        ]:
            try:
                btn = self.page.locator(sel).first
                if btn.is_visible(timeout=300):
                    btn.click()
                    time.sleep(0.2)
                    break
            except Exception:
                pass

    def _fill_location_autocomplete(self, report: dict[str, Any]) -> None:
        """Fills location autocomplete inputs (e.g. Greenhouse #candidate-location)."""
        city = (self.profile.city or "").strip()
        if not city:
            return

        loc_selectors = [
            "#candidate-location",
            "#job_application_location",
            "input[name*='location']",
            "input[id*='location']",
        ]
        for sel in loc_selectors:
            try:
                inp = self.page.locator(sel).first
                if inp.is_visible(timeout=500):
                    val = inp.input_value() or ""
                    if len(val.strip()) < 2:
                        inp.click()
                        inp.fill(city)
                        time.sleep(0.8)
                        opt = self.page.locator(f"text={city}").first
                        if opt.is_visible(timeout=1000):
                            opt.click()
                        else:
                            self.page.keyboard.press("ArrowDown")
                            self.page.keyboard.press("Enter")
                        report["fields_filled"].append("Location Autocomplete")
                        break
            except Exception:
                pass

    def _fill_combobox_inputs(
        self, report: dict[str, Any], job_title: str, company: str
    ) -> None:
        """Fills custom React-Select / ARIA Combobox dropdowns (e.g. Greenhouse custom questions)."""
        try:
            combos = self.page.locator("input[role='combobox']").all()
            for c in combos:
                try:
                    if not c.is_visible(timeout=300):
                        continue
                    val = c.input_value() or ""
                    if len(val.strip()) > 2:
                        continue

                    label_text = ""
                    try:
                        parent = c.locator(
                            "xpath=ancestor::div[contains(@class, 'field') or contains(@class, 'remix')]"
                        ).first
                        label_text = (
                            parent.inner_text(timeout=500).split("\n")[0].strip()
                        )
                    except Exception:
                        label_text = self._get_element_descriptor(c)

                    if not label_text or len(label_text) < 3:
                        continue

                    ans = self.question_solver.answer_question(
                        label_text,
                        job_title=job_title,
                        company=company,
                    )
                    if ans:
                        c.click()
                        c.fill(ans)
                        time.sleep(0.2)
                        self.page.keyboard.press("Enter")
                        time.sleep(0.2)
                        report["fields_filled"].append(f"Combobox: {label_text[:25]}")
                except Exception:
                    continue
        except Exception:
            pass

    def _get_target_scopes(self) -> list[Any]:
        """Returns the main page plus any child frames to find inputs in iframes."""
        scopes = [self.page]
        try:
            for frame in self.page.frames:
                if frame != self.page.main_frame:
                    scopes.append(frame)
        except Exception:
            pass
        return scopes

    def fill_application_form(
        self,
        app_dir: Path,
        job_title: str = "",
        company: str = "",
    ) -> dict[str, Any]:
        """
        Fills form inputs (text, email, tel, select, file uploads, textareas)
        with candidate information, CV PDF, and cover letter.
        """
        if not self.page:
            self.start()

        # Check if Cloudflare challenge is present before filling
        if is_cloudflare_challenge(self.page):
            solve_cloudflare_turnstile(self.page)

        report: dict[str, Any] = {
            "fields_filled": [],
            "resume_uploaded": False,
            "cover_letter_filled": False,
            "questions_answered": [],
            "platform": self.detect_platform(self.page.url),
        }

        # 1. Locate CV PDF and Cover Letter
        cv_pdf = next(app_dir.glob("CV_*.pdf"), None)
        cover_letter_file = app_dir / "cover_letter.txt"
        cover_letter_text = ""
        if cover_letter_file.exists():
            try:
                with open(cover_letter_file, encoding="utf-8") as f:
                    cover_letter_text = f.read().strip()
            except Exception:
                pass

        # 2. Upload Resume PDF
        if cv_pdf and cv_pdf.exists():
            report["resume_uploaded"] = self._upload_resume(str(cv_pdf.resolve()))

        # 3. Fill text, email, tel, url, and textarea inputs
        self._fill_standard_inputs(report, cover_letter_text)

        # 4. Fill location autocomplete (Greenhouse #candidate-location)
        self._fill_location_autocomplete(report)

        # 5. Fill custom React-Select / ARIA comboboxes
        self._fill_combobox_inputs(report, job_title, company)

        # 6. Fill dropdowns / select elements
        self._fill_select_inputs(report)

        # 5. If 0 fields filled, check if an unclicked Apply button is present
        if len(report["fields_filled"]) == 0:
            apply_buttons = [
                "#indeedApplyButton",
                "[id*='indeedApply']",
                "button:has-text('Apply now')",
                "a:has-text('Apply now')",
                "a:has-text('Apply on company website')",
                "button:has-text('Apply on company website')",
                "button:has-text('Apply for this job')",
                "a:has-text('Apply for this job')",
                "button:has-text('Apply')",
                "a:has-text('Apply')",
            ]
            for btn_sel in apply_buttons:
                try:
                    locator = self.page.locator(btn_sel).first
                    if locator.is_visible(timeout=1000):
                        print(
                            f" Found unclicked apply button '{btn_sel}'. Clicking to open form..."
                        )
                        locator.click()
                        time.sleep(3)
                        self._fill_standard_inputs(report, cover_letter_text)
                        self._fill_select_inputs(report)
                        if not report["resume_uploaded"] and cv_pdf and cv_pdf.exists():
                            report["resume_uploaded"] = self._upload_resume(
                                str(cv_pdf.resolve())
                            )
                        break
                except Exception:
                    continue

        # 6. Answer screening questions
        self._answer_custom_questions(report, job_title, company)

        # 7. Check common consent / agreement checkboxes
        self._check_consent_boxes()

        # 8. Inject visual highlight / banner in the browser
        self._inject_autofill_indicator(
            len(report["fields_filled"]), report["resume_uploaded"]
        )

        return report

    def _upload_resume(self, cv_path: str) -> bool:
        """Uploads the tailored CV PDF to file inputs."""
        try:
            file_inputs = self.page.locator("input[type=file]")
            count = file_inputs.count()
            if count > 0:
                print(
                    f" Found {count} file input(s). Attaching resume: {Path(cv_path).name}"
                )
                # Target the first file input (typically resume/CV)
                file_inputs.first.set_input_files(cv_path)
                time.sleep(1)
                return True
        except Exception as e:
            print(f" Notice: Resume upload failed or input not standard: {e}")
        return False

    def _fill_standard_inputs(
        self, report: dict[str, Any], cover_letter_text: str
    ) -> None:
        """Fills text, email, phone, link, and textarea fields."""
        inputs = self.page.locator(
            "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=checkbox]):not([type=radio]), textarea"
        )
        count = inputs.count()

        for i in range(count):
            try:
                el = inputs.nth(i)
                if not el.is_visible():
                    continue

                # Get element metadata
                name_attr = (el.get_attribute("name") or "").lower()
                id_attr = (el.get_attribute("id") or "").lower()
                placeholder = (el.get_attribute("placeholder") or "").lower()
                aria_label = (el.get_attribute("aria-label") or "").lower()
                data_auto = (el.get_attribute("data-automation-id") or "").lower()
                autocomplete = (el.get_attribute("autocomplete") or "").lower()

                # Get associated label text
                label_text = ""
                try:
                    # Check parent or preceding label
                    label_text = el.evaluate("""el => {
                        const lbl = el.labels && el.labels[0] ? el.labels[0].innerText : "";
                        const aria = el.getAttribute('aria-label') || "";
                        const placeholder = el.getAttribute('placeholder') || "";
                        return (lbl + " " + aria + " " + placeholder).toLowerCase();
                    }""")
                except Exception:
                    pass

                descriptor = f"{name_attr} {id_attr} {placeholder} {aria_label} {data_auto} {autocomplete} {label_text}"

                # Skip if already has substantial content and not cover letter
                current_val = el.input_value() or ""
                if (
                    len(current_val) > 2
                    and "cover" not in descriptor
                    and "letter" not in descriptor
                ):
                    continue

                val = None
                field_name = None

                # Match field patterns
                if any(
                    w in descriptor
                    for w in [
                        "first_name",
                        "firstname",
                        "first name",
                        "given_name",
                        "given-name",
                        "forename",
                    ]
                ):
                    val = self.profile.first_name
                    field_name = "First Name"
                elif any(
                    w in descriptor
                    for w in [
                        "last_name",
                        "lastname",
                        "last name",
                        "family_name",
                        "family-name",
                        "surname",
                    ]
                ):
                    val = self.profile.last_name
                    field_name = "Last Name"
                elif any(
                    w in descriptor
                    for w in [
                        "full_name",
                        "fullname",
                        "full name",
                        "applicant_name",
                        "candidate_name",
                    ]
                ) or (
                    "name" in descriptor
                    and not any(
                        w in descriptor
                        for w in ["company", "user", "file", "org", "school"]
                    )
                ):
                    val = self.profile.full_name
                    field_name = "Full Name"
                elif any(w in descriptor for w in ["email", "e-mail"]):
                    val = self.profile.email
                    field_name = "Email"
                elif any(w in descriptor for w in ["phone", "mobile", "tel", "cell"]):
                    val = self.profile.phone
                    field_name = "Phone"
                elif "linkedin" in descriptor:
                    val = self.profile.linkedin_url
                    field_name = "LinkedIn"
                elif "github" in descriptor:
                    val = self.profile.github_url
                    field_name = "GitHub"
                elif any(w in descriptor for w in ["website", "portfolio", "blog"]):
                    val = self.profile.portfolio_url or self.profile.github_url
                    field_name = "Portfolio / Website"
                elif any(w in descriptor for w in ["city", "town"]):
                    val = self.profile.city
                    field_name = "City"
                elif any(w in descriptor for w in ["address", "street"]):
                    val = self.profile.address
                    field_name = "Address"
                elif any(w in descriptor for w in ["postal", "zip"]):
                    val = self.profile.postal_code
                    field_name = "Postal Code"
                elif any(
                    w in descriptor
                    for w in ["company", "employer", "organization", "org"]
                ):
                    val = self.profile.current_company
                    field_name = "Current Company"
                elif any(
                    w in descriptor
                    for w in ["title", "role", "designation", "headline"]
                ):
                    val = self.profile.current_title
                    field_name = "Current Title"
                elif any(
                    w in descriptor
                    for w in [
                        "cover",
                        "letter",
                        "additional_info",
                        "comments",
                        "why us",
                        "note",
                    ]
                ):
                    if cover_letter_text:
                        val = cover_letter_text
                        field_name = "Cover Letter"
                        report["cover_letter_filled"] = True

                if val and field_name:
                    el.fill(val)
                    self._highlight_element(el)
                    report["fields_filled"].append(field_name)

            except Exception:
                continue

    def _fill_select_inputs(self, report: dict[str, Any]) -> None:
        """Selects options for dropdown fields like Country, Authorization, Sponsorship."""
        selects = self.page.locator("select")
        count = selects.count()

        for i in range(count):
            try:
                el = selects.nth(i)
                if not el.is_visible():
                    continue

                desc = self._get_element_descriptor(el)
                options = el.locator("option").all_inner_texts()

                matched_val = None
                if "country" in desc:
                    matched_val = self.question_solver._match_to_options(
                        self.profile.country, options
                    )
                elif any(w in desc for w in ["authorized", "eligib", "right to work"]):
                    matched_val = self.question_solver._match_to_options("Yes", options)
                elif any(w in desc for w in ["sponsorship", "visa"]):
                    matched_val = self.question_solver._match_to_options("No", options)
                elif "gender" in desc:
                    matched_val = self.question_solver._match_to_options(
                        self.profile.gender, options
                    )

                if matched_val:
                    el.select_option(label=matched_val)
                    self._highlight_element(el)
                    report["fields_filled"].append(f"Dropdown: {matched_val}")

            except Exception:
                continue

    def _answer_custom_questions(
        self, report: dict[str, Any], job_title: str, company: str
    ) -> None:
        """Detects remaining unfilled fields and uses QuestionSolver to answer them."""
        unfilled_inputs = self.page.locator(
            "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=file]):not([type=checkbox]):not([type=radio]), textarea"
        )
        count = unfilled_inputs.count()

        for i in range(count):
            try:
                el = unfilled_inputs.nth(i)
                if not el.is_visible():
                    continue

                val = el.input_value() or ""
                if val.strip():
                    continue  # Already filled

                label_text = self._get_element_descriptor(el)
                if len(label_text.strip()) > 5:
                    answer = self.question_solver.answer_question(
                        label_text,
                        job_title=job_title,
                        company=company,
                    )
                    if answer:
                        el.fill(answer)
                        self._highlight_element(el)
                        report["questions_answered"].append(
                            {"question": label_text[:50], "answer": answer}
                        )
                        report["fields_filled"].append(f"Question: {label_text[:30]}")

            except Exception:
                continue

    def _check_consent_boxes(self) -> None:
        """Checks required consent or agreement checkboxes (e.g. Privacy policy)."""
        try:
            checkboxes = self.page.locator("input[type=checkbox]")
            count = checkboxes.count()
            for i in range(count):
                cb = checkboxes.nth(i)
                if cb.is_visible() and not cb.is_checked():
                    desc = self._get_element_descriptor(cb)
                    if any(
                        w in desc
                        for w in [
                            "agree",
                            "consent",
                            "terms",
                            "policy",
                            "privacy",
                            "acknowledge",
                        ]
                    ):
                        cb.check()
        except Exception:
            pass

    def _get_element_descriptor(self, el: Any) -> str:
        """Extracts text, label, aria, and id descriptor for an element."""
        try:
            return el.evaluate("""el => {
                const lbl = el.labels && el.labels[0] ? el.labels[0].innerText : "";
                const aria = el.getAttribute('aria-label') || "";
                const placeholder = el.getAttribute('placeholder') || "";
                const name = el.getAttribute('name') || "";
                const id = el.getAttribute('id') || "";
                return (lbl + " " + aria + " " + placeholder + " " + name + " " + id).toLowerCase();
            }""")
        except Exception:
            return ""

    def _highlight_element(self, el: Any) -> None:
        """Applies a green border highlight to indicate automated fill."""
        try:
            el.evaluate("""el => {
                el.style.backgroundColor = '#f1f8e9';
                el.style.border = '2px solid #43a047';
            }""")
        except Exception:
            pass

    def _inject_autofill_indicator(self, filled_count: int, resume_ok: bool) -> None:
        """Injects a floating toast notification in the page."""
        try:
            cf_active = is_cloudflare_challenge(self.page)
            if cf_active:
                ray_id = extract_ray_id(self.page)
                ray_msg = f" (Ray ID: {ray_id})" if ray_id else ""
                msg = f"🛡️ Cloudflare verification required{ray_msg}! Please complete check in browser."
                bg = "#b91c1c"
            elif filled_count == 0:
                msg = "⚡ JobApplier: 0 fields found. Click 'Apply Now' on the page to open the application form!"
                bg = "#b45309"
            else:
                msg = f"⚡ JobApplier: {filled_count} fields autofilled" + (
                    " + Resume attached!" if resume_ok else ""
                )
                bg = "#2e7d32"

            self.page.evaluate(f"""() => {{
                const d = document.createElement('div');
                d.style.position = 'fixed';
                d.style.top = '15px';
                d.style.right = '15px';
                d.style.zIndex = '999999';
                d.style.background = '{bg}';
                d.style.color = '#ffffff';
                d.style.padding = '12px 18px';
                d.style.borderRadius = '6px';
                d.style.fontSize = '14px';
                d.style.fontWeight = 'bold';
                d.style.boxShadow = '0 4px 12px rgba(0,0,0,0.3)';
                d.innerText = "{msg}";
                document.body.appendChild(d);
                setTimeout(() => d.remove(), 8000);
            }}""")
        except Exception:
            pass

    def find_submit_button(self) -> Any | None:
        """Locates the final submit button on the application page."""
        submit_selectors = [
            "button[type=submit]",
            "input[type=submit]",
            "button:has-text('Submit Application')",
            "button:has-text('Submit application')",
            "button:has-text('Submit resume')",
            "button:has-text('Review Application')",
            "button:has-text('Send Application')",
            "button:has-text('Apply')",
            "[data-automation-id='bottom-navigation-next-button']",
        ]
        for sel in submit_selectors:
            try:
                btn = self.page.locator(sel).first
                if btn.is_visible(timeout=1000):
                    return btn
            except Exception:
                continue
        return None

    def _detect_validation_errors(self) -> list[str]:
        """Detects visible validation error messages on the page."""
        errors = []
        error_selectors = [
            ".error",
            ".field-error",
            ".validation-error",
            "[aria-invalid='true']",
            "div:has-text('This field is required')",
            "span:has-text('This field is required')",
            "p:has-text('This field is required')",
            "div:has-text('Please enter your location')",
            "div:has-text('Please correct the errors')",
        ]
        for sel in error_selectors:
            try:
                loc = self.page.locator(sel)
                count = loc.count()
                for i in range(min(count, 5)):
                    if loc.nth(i).is_visible(timeout=200):
                        txt = loc.nth(i).inner_text(timeout=200).strip()
                        if txt and txt not in errors and len(txt) < 80:
                            errors.append(txt)
            except Exception:
                pass
        return errors

    def supply_verification_code(self, code: str) -> bool:
        """Supplies a 2FA or email verification code received externally (e.g. from web UI)."""
        self.provided_code = code.strip()
        self.waiting_for_code = False
        return True

    def _detect_login_wall(self) -> tuple[bool, str]:
        """Detects if the page is currently blocked behind a login or sign-up wall."""
        if not self.page:
            return False, ""
        url = (self.page.url or "").lower()
        if any(
            w in url
            for w in [
                "/login",
                "/signin",
                "/cold-join",
                "/sign-in",
                "accounts.google.com",
            ]
        ):
            return True, "Redirected to platform sign-in page"

        try:
            pwd_inputs = self.page.locator("input[type='password']")
            if pwd_inputs.count() > 0 and pwd_inputs.first.is_visible(timeout=300):
                return True, "Password input visible (account login required)"
        except Exception:
            pass
        return False, ""

    def _detect_verification_challenge(self) -> tuple[bool, Any | None]:
        """Detects if the page is currently asking for an email/SMS verification code or OTP."""
        if not self.page:
            return False, None
        otp_selectors = [
            "input[autocomplete='one-time-code']",
            "input[name*='code' i]:not([name*='postal']):not([name*='zip']):not([name*='country'])",
            "input[name*='otp' i]",
            "input[id*='verification' i]",
            "input[placeholder*='verification' i]",
            "input[placeholder*='code' i]:not([placeholder*='postal']):not([placeholder*='zip'])",
        ]
        for sel in otp_selectors:
            try:
                inp = self.page.locator(sel).first
                if inp.is_visible(timeout=300):
                    return True, inp
            except Exception:
                continue
        return False, None

    def _handle_verification_challenge(
        self, inp_element: Any, timeout: int = 90
    ) -> bool:
        """Prompts for and fills in the email/SMS verification code."""
        self._notify(
            "verification_code_required",
            "🔑 Email/SMS verification code required by application form!",
            level="WARN",
        )
        self.waiting_for_code = True
        self.provided_code = None

        code = ""
        is_interactive = sys.stdin.isatty() and not self.headless

        if is_interactive:
            try:
                print("\n" + "=" * 60)
                print("🔑 VERIFICATION CODE REQUIRED")
                print("   A security code was sent to your email / phone.")
                code = input("   Enter verification code: ").strip()
                print("=" * 60 + "\n")
            except (EOFError, KeyboardInterrupt):
                code = ""
        else:
            print(
                f" Waiting for verification code via API / Web Dashboard (timeout: {timeout}s)..."
            )
            start = time.time()
            while time.time() - start < timeout:
                if self.provided_code:
                    code = self.provided_code
                    break
                time.sleep(1)

        self.waiting_for_code = False

        if code:
            try:
                inp_element.fill(code)
                time.sleep(1)
                for sub in [
                    "button:has-text('Verify')",
                    "button:has-text('Confirm')",
                    "button:has-text('Submit')",
                    "button:has-text('Continuă')",
                    "button:has-text('Verifică')",
                    "button[type=submit]",
                ]:
                    btn = self.page.locator(sub).first
                    if btn.is_visible(timeout=500):
                        btn.click()
                        time.sleep(3)
                        return True
            except Exception as e:
                print(f"Notice filling verification code: {e}")
        return False

    def _verify_submission(self) -> tuple[bool, str]:
        """Checks whether the application submission was genuinely accepted by the site."""
        errors = self._detect_validation_errors()
        if errors:
            return False, f"Validation errors on page: {', '.join(errors[:2])}"

        if is_cloudflare_challenge(self.page):
            return False, "Blocked by Cloudflare verification challenge"

        # Check if an OTP / verification challenge popped up
        has_otp, otp_inp = self._detect_verification_challenge()
        if has_otp and otp_inp:
            solved = self._handle_verification_challenge(otp_inp)
            if solved:
                time.sleep(3)
                return self._verify_submission()
            return False, "Verification code required but not supplied"

        # Check if a login wall intercepted
        is_login, login_msg = self._detect_login_wall()
        if is_login:
            return False, f"Blocked by platform login wall: {login_msg}"

        url_lower = (self.page.url or "").lower()
        if any(
            w in url_lower
            for w in [
                "/confirmation",
                "/thank_you",
                "/thank-you",
                "/submitted",
                "/applied",
                "/success",
            ]
        ):
            return True, "Submission confirmed by redirect to confirmation page"

        try:
            body_text = self.page.locator("body").inner_text(timeout=1000).lower()
            if any(
                w in body_text
                for w in [
                    "application submitted",
                    "thank you for your application",
                    "thank you for applying",
                    "we have received your application",
                    "application received",
                    "aplicația a fost trimisă",
                    "aplicație trimisă",
                ]
            ):
                return True, "Submission confirmed by success message"
        except Exception:
            pass

        submit_btn = self.find_submit_button()
        if submit_btn and submit_btn.is_visible(timeout=500):
            return False, "Form still active with submit button visible"

        return False, "Submission could not be confirmed by destination site"

    def _save_diagnostics(
        self,
        app_dir: Path,
        company: str,
        job_title: str,
        job_url: str,
        report: dict[str, Any],
        reason: str,
    ) -> None:
        """Saves diagnostics JSON and DOM HTML snippet upon submission issues."""
        diag_file = app_dir / "diagnostics.json"
        try:
            with open(diag_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "company": company,
                        "title": job_title,
                        "job_url": job_url,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "status": "failed",
                        "reason": reason,
                        "platform": report.get("platform", "Unknown"),
                        "fields_filled": report.get("fields_filled", []),
                        "resume_uploaded": report.get("resume_uploaded", False),
                        "validation_errors": self._detect_validation_errors(),
                    },
                    f,
                    indent=2,
                )
        except OSError as err:
            print(f"Notice: Could not save diagnostics.json: {err}")

        dom_file = app_dir / "diagnostic_dom.html"
        try:
            with open(dom_file, "w", encoding="utf-8") as f:
                f.write(self.page.content() if self.page else "")
        except OSError as err:
            print(f"Notice: Could not save diagnostic_dom.html: {err}")

    def run_assisted_apply(
        self,
        app_dir: Path,
        job_url: str,
        company: str,
        job_title: str,
        non_interactive: bool = False,
    ) -> tuple[str, str]:
        """
        Assisted Apply Mode:
        1. Navigates to job URL and opens application form.
        2. Fills all fields, attaches tailored CV PDF, fills cover letter, answers questions.
        3. Highlights submit button and presents a review prompt to the user.
        4. User can confirm submit with [Enter] or submit manually in the browser.
        """
        print(f"\n🚀 Starting Assisted Auto-Apply for {company} - {job_title}")
        success = self.navigate_and_open_form(job_url)
        if not success:
            return "failed", "Could not navigate to job URL"

        report = self.fill_application_form(
            app_dir, job_title=job_title, company=company
        )
        fields_str = ", ".join(report["fields_filled"][:6])
        if len(report["fields_filled"]) > 6:
            fields_str += f" (+{len(report['fields_filled']) - 6} more)"

        print("\n Application Form Autofilled!")
        print(f"  • Platform Detected: {report['platform']}")
        print(
            f"  • Resume Attached:   {' Yes' if report['resume_uploaded'] else '⚠️  Manual upload needed'}"
        )
        print(
            f"  • Cover Letter:      {' Yes' if report['cover_letter_filled'] else ' None'}"
        )
        print(f"  • Fields Filled:     {fields_str or 'None'}")
        if report["questions_answered"]:
            print(
                f"  • AI Questions:      {len(report['questions_answered'])} answered"
            )

        submit_btn = self.find_submit_button()
        if submit_btn:
            try:
                submit_btn.scroll_into_view_if_needed()
                submit_btn.evaluate("""el => {
                    el.style.outline = '4px solid #ff9800';
                    el.style.boxShadow = '0 0 15px #ff9800';
                }""")
            except Exception:
                pass

        is_interactive = (
            sys.stdin.isatty() and not self.headless and not non_interactive
        )
        if not is_interactive:
            print(" Non-interactive/headless mode: Auto-confirming submit...")
            choice = ""
        else:
            print(
                f"\n👉 {self.engine.capitalize()} is open with the filled application form."
            )
            print("   Review the fields in your browser window.")
            print("   Options:")
            print("     [Enter] -> Auto-click the Submit button")
            print(
                "     [m]     -> You will submit manually in the browser (mark as applied)"
            )
            print("     [s]     -> Skip this application")

            try:
                choice = input("   Select action ([Enter]/m/s): ").strip().lower()
            except EOFError:
                choice = ""

        if choice == "s":
            record_application(
                company=company,
                title=job_title,
                job_url=job_url,
                status="skipped",
                platform=report["platform"],
                submission_type="assisted_browser",
                notes="Skipped by user during assisted review",
            )
            return "skipped", "User skipped"

        if choice == "" or choice == "enter":
            if submit_btn:
                try:
                    print(" Submitting application...")
                    submit_btn.click()
                    time.sleep(4)
                except Exception as e:
                    print(
                        f"⚠️ Could not auto-click submit: {e}. Please click submit in browser."
                    )

            submitted_ok, verif_msg = self._verify_submission()
            proof_path = app_dir / (
                "submission_proof.png" if submitted_ok else "submission_failed.png"
            )
            try:
                self.page.screenshot(path=str(proof_path))
            except Exception:
                pass

            if submitted_ok or choice == "m":
                self._notify(
                    "submitted",
                    f"Successfully submitted and verified for {company} - {job_title}",
                    level="SUCCESS",
                )
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    status="applied",
                    platform=report["platform"],
                    submission_type="assisted_browser",
                    proof_path=str(proof_path) if proof_path.exists() else "",
                    notes=f"Verified: {verif_msg}",
                )
                return "applied", "Successfully completed assisted apply"
            else:
                self._save_diagnostics(
                    app_dir, company, job_title, job_url, report, verif_msg
                )
                self._notify(
                    "submission_failed",
                    f"Submission blocked for {company} - {job_title}: {verif_msg}",
                    level="WARN",
                )
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    status="failed",
                    platform=report["platform"],
                    submission_type="assisted_browser",
                    proof_path=str(proof_path) if proof_path.exists() else "",
                    notes=f"Unconfirmed: {verif_msg}",
                )
                return "failed", f"Submission unconfirmed: {verif_msg}"

        return "skipped", "User did not submit"

    def run_autonomous_apply(
        self,
        app_dir: Path,
        job_url: str,
        company: str,
        job_title: str,
    ) -> tuple[str, str]:
        """
        Autonomous Apply Mode:
        Completely automated: Navigates -> Fills form -> Attaches CV -> Submits -> Records proof.
        """
        print(f"\n⚡ Starting Autonomous Auto-Apply for {company} - {job_title}")
        success = self.navigate_and_open_form(job_url)
        if not success:
            return "failed", "Could not navigate to job URL"

        report = self.fill_application_form(
            app_dir, job_title=job_title, company=company
        )
        time.sleep(2)

        submit_btn = self.find_submit_button()
        if not submit_btn:
            # Save screenshot for diagnosis
            fail_shot = app_dir / "submission_failed.png"
            try:
                self.page.screenshot(path=str(fail_shot))
            except Exception:
                pass
            self._save_diagnostics(
                app_dir,
                company,
                job_title,
                job_url,
                report,
                "Submit button could not be located",
            )
            self._notify(
                "button_not_found",
                f"Submit button not found for {company} - {job_title}",
                level="WARN",
            )
            record_application(
                company=company,
                title=job_title,
                job_url=job_url,
                status="failed",
                platform=report["platform"],
                submission_type="autonomous_browser",
                notes="Submit button could not be located",
            )
            return "failed", "Submit button not found"

        try:
            self._notify(
                "submitting",
                f"Auto-submitting application for {company} - {job_title}...",
                level="INFO",
            )
            print(" Auto-submitting application...")
            submit_btn.click()
            time.sleep(5)

            submitted_ok, verif_msg = self._verify_submission()
            proof_path = app_dir / (
                "submission_proof.png" if submitted_ok else "submission_failed.png"
            )
            try:
                self.page.screenshot(path=str(proof_path))
            except Exception:
                pass

            if submitted_ok:
                self._notify(
                    "submitted",
                    f"Verified application submission for {company} - {job_title}",
                    level="SUCCESS",
                )
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    status="applied",
                    platform=report["platform"],
                    submission_type="autonomous_browser",
                    proof_path=str(proof_path) if proof_path.exists() else "",
                    notes=f"Submission verified. Fields filled: {len(report['fields_filled'])}",
                )
                return "applied", f"Successfully auto-submitted ({verif_msg})"
            else:
                self._save_diagnostics(
                    app_dir, company, job_title, job_url, report, verif_msg
                )
                self._notify(
                    "submission_failed",
                    f"Submission unconfirmed for {company} - {job_title}: {verif_msg}",
                    level="WARN",
                )
                record_application(
                    company=company,
                    title=job_title,
                    job_url=job_url,
                    status="failed",
                    platform=report["platform"],
                    submission_type="autonomous_browser",
                    proof_path=str(proof_path) if proof_path.exists() else "",
                    notes=f"Unconfirmed: {verif_msg}",
                )
                return "failed", verif_msg

        except Exception as e:
            record_application(
                company=company,
                title=job_title,
                job_url=job_url,
                status="failed",
                platform=report["platform"],
                submission_type="autonomous_browser",
                notes=f"Error during submission: {e}",
            )
            return "failed", str(e)
