from __future__ import annotations

import re
import time
from typing import Any


def extract_ray_id(page: Any) -> str:
    """Extracts Cloudflare Ray ID from the page if present."""
    try:
        content = page.content()
        match = re.search(r"Ray ID:\s*([a-f0-9]+)", content, re.IGNORECASE)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


def is_cloudflare_challenge(page: Any) -> bool:
    """Detects if the current page is a Cloudflare verification / Turnstile challenge."""
    try:
        title = (page.title() or "").lower()
        if any(
            w in title
            for w in [
                "just a moment...",
                "attention required",
                "security check",
                "cloudflare",
            ]
        ):
            return True

        url = (page.url or "").lower()
        if "challenges.cloudflare.com" in url:
            return True

        # Check DOM elements
        has_cf_element = (
            page.locator(
                "iframe[src*='challenges.cloudflare.com'], #challenge-stage, #turnstile-wrapper, .ray-id"
            ).count()
            > 0
        )
        if has_cf_element:
            return True

        # Check text in body
        body_text = page.locator("body").inner_text(timeout=1000) or ""
        body_lower = body_text.lower()
        if any(
            w in body_lower
            for w in [
                "additional verification required",
                "verify you are human",
                "checking your browser",
                "ray id",
                "cloudflare",
            ]
        ):
            return True
    except Exception:
        pass

    return False


def solve_cloudflare_turnstile(page: Any, max_wait_sec: int = 15) -> bool:
    """
    Detects and attempts to auto-solve Cloudflare Turnstile / Managed Challenges.
    1. Identifies challenge and Ray ID.
    2. Searches for Turnstile iframe and clicks verification checkbox.
    3. Waits for verification clearance and page redirect.
    """
    if not is_cloudflare_challenge(page):
        return True

    ray_id = extract_ray_id(page)
    ray_info = f" (Ray ID: {ray_id})" if ray_id else ""
    print(
        f"\n🛡️ Cloudflare verification detected{ray_info}. Attempting automated bypass..."
    )

    start_time = time.time()

    # Step 1: Wait 2-3 seconds for managed/non-interactive challenges to solve automatically
    time.sleep(2.5)
    if not is_cloudflare_challenge(page):
        print(" Cloudflare challenge passed automatically!")
        return True

    # Step 2: Attempt to click the Turnstile checkbox
    try:
        # Check inside child frames for Cloudflare challenges
        clicked = False
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url or "cloudflare" in frame.url:
                checkbox = frame.locator(
                    "input[type='checkbox'], #challenge-stage, .ctp-checkbox-label, .mark"
                )
                if checkbox.count() > 0 and checkbox.first.is_visible(timeout=1000):
                    print(" Found Turnstile checkbox in challenge frame. Clicking...")
                    checkbox.first.hover()
                    time.sleep(0.3)
                    checkbox.first.click()
                    clicked = True
                    break

        # If not clicked via frame locator, try coordinate click on the iframe bounding box
        if not clicked:
            tf_iframe = page.locator("iframe[src*='challenges.cloudflare.com']").first
            if tf_iframe.is_visible(timeout=1500):
                box = tf_iframe.bounding_box()
                if box:
                    # The Turnstile checkbox is typically at ~30px from the left, middle vertically
                    click_x = box["x"] + 30
                    click_y = box["y"] + (box["height"] / 2)
                    print(
                        f" Clicking Turnstile checkbox at coordinates ({click_x}, {click_y})..."
                    )
                    page.mouse.move(click_x, click_y, steps=5)
                    time.sleep(0.2)
                    page.mouse.click(click_x, click_y)
                    clicked = True

    except Exception as e:
        print(f" Notice while clicking Turnstile: {e}")

    # Step 3: Wait for clearance cookie or redirect
    print(" Waiting for Cloudflare clearance...")
    while time.time() - start_time < max_wait_sec:
        time.sleep(1)
        if not is_cloudflare_challenge(page):
            print(" Cloudflare verification passed! Proceeding to application form.")
            return True

    print(f"⚠️ Cloudflare challenge still active after {max_wait_sec}s.")
    return False
