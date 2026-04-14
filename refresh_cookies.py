#!/usr/bin/env python3
"""
GoPro Cookie Refresher
-----------------------
Logs into GoPro using email/password via Playwright headless browser,
extracts session cookies, and saves them to gopro_cookies.json.
Run before the uploader to ensure cookies are always fresh.
"""

import json
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

EMAIL    = os.environ["GOPRO_EMAIL"]
PASSWORD = os.environ["GOPRO_PASSWORD"]
OUT_PATH = Path(__file__).parent / "gopro_cookies.json"

GOPRO_LOGIN_URL = "https://login.gopro.com"
GOPRO_APP_URL   = "https://plus.gopro.com"


def refresh():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        print("Navigating to GoPro login...")
        page.goto(GOPRO_LOGIN_URL, wait_until="networkidle", timeout=30000)

        # Fill email
        print("Entering email...")
        page.fill('input[type="email"], input[name="email"], input[id*="email"]', EMAIL)

        # Some flows have a separate "Next" button before password
        try:
            next_btn = page.locator('button:has-text("Next"), button:has-text("Continue")')
            if next_btn.count() > 0:
                next_btn.first.click()
                page.wait_for_timeout(1500)
        except Exception:
            pass

        # Fill password
        print("Entering password...")
        page.fill('input[type="password"]', PASSWORD)

        # Submit
        print("Submitting...")
        page.click('button[type="submit"], button:has-text("Sign In"), button:has-text("Log In")')

        # Wait for redirect to GoPro app
        try:
            page.wait_for_url("**/plus.gopro.com/**", timeout=15000)
        except PlaywrightTimeout:
            # Try waiting for any navigation away from login
            try:
                page.wait_for_url(lambda url: "login.gopro.com" not in url, timeout=10000)
            except PlaywrightTimeout:
                print("ERROR: Login may have failed — still on login page")
                print(f"Current URL: {page.url}")
                browser.close()
                sys.exit(1)

        print(f"Logged in. Current URL: {page.url}")

        # Navigate to the app to ensure cloud session cookies are set
        if "plus.gopro.com" not in page.url:
            page.goto(GOPRO_APP_URL, wait_until="networkidle", timeout=20000)

        # Extract all cookies
        cookies = context.cookies()
        browser.close()

        if not cookies:
            print("ERROR: No cookies extracted after login")
            sys.exit(1)

        # Save as a simple dict keyed by name for easy loading
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        with open(OUT_PATH, "w") as f:
            json.dump(cookie_dict, f, indent=2)

        print(f"Saved {len(cookie_dict)} cookies to {OUT_PATH.name}")


if __name__ == "__main__":
    refresh()
