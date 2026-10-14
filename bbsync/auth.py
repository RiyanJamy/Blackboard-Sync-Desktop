"""Persistent browser session for Blackboard Ultra SSO login."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .config import PROFILE_DIR

log = logging.getLogger("bbsync")

# Tried in order: public REST first, private SPA API as fallback.
_ME_ENDPOINTS = (
    "/learn/api/public/v1/users/me",
    "/learn/api/v1/users/me",
)


def _ultra_url_pattern(base_url: str) -> re.Pattern[str]:
    return re.compile(re.escape(urlparse(base_url).netloc) + r"/ultra")


@contextmanager
def browser(headless: bool = True):
    import json
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    cookie_file = PROFILE_DIR.parent / "session-cookies.json"
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=headless)
        try:
            if cookie_file.exists():
                try:
                    ctx.add_cookies(json.loads(cookie_file.read_text()))
                except (ValueError, PlaywrightError):
                    cookie_file.unlink(missing_ok=True)
            yield ctx
        finally:
            # Only persist a validated session; failed checks cannot poison the snapshot.
            from .config import Config
            try:
                if current_user(ctx, Config.load().base_url):
                    temp = cookie_file.with_suffix(".tmp")
                    temp.write_text(json.dumps(ctx.cookies()), encoding="utf-8")
                    temp.replace(cookie_file)
            finally:
                ctx.close()


def current_user(ctx: BrowserContext, base_url: str) -> dict | None:
    for path in _ME_ENDPOINTS:
        try:
            resp = ctx.request.get(base_url + path, timeout=10000)
            if resp.ok:
                data = resp.json()
                if isinstance(data, dict) and data.get("id"):
                    return data
        except (PlaywrightError, ValueError):
            continue
    return None


def ensure_session(ctx: BrowserContext, base_url: str, *, interactive: bool = False) -> dict | None:
    import time
    user = current_user(ctx, base_url)
    if user:
        return user
    page = ctx.new_page()
    try:
        page.goto(base_url + "/ultra", wait_until="domcontentloaded", timeout=30000)
        deadline = time.monotonic() + (300 if interactive else 30)
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            user = current_user(ctx, base_url)
            if user:
                return user
        return None
    except PlaywrightError:
        return None
    finally:
        page.close()
