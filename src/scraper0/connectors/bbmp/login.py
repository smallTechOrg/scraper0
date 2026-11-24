"""SmartOne BLR login (port of legacy portals/bbmp_login.py) + shared browser helpers.

Playwright is imported lazily so importing this module never needs browsers installed.
The session (Playwright storage_state) lives in ctx.state, keyed per portal username — never on disk.
"""
from __future__ import annotations

import json
from typing import Callable

import structlog

from scraper0.connectors.base import AI, StateStore
from scraper0.connectors.bbmp.captcha import solve_captcha

log = structlog.get_logger(__name__)

PORTAL_HOME = "https://www.smartoneblr.com/BBMPServices.htm"
CAPTCHA_ATTEMPTS = 3
BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
LOGIN_DWR_PATTERN = "**/dwr/call/plaincall/HomePagelogin.LoginVaildation.dwr"

CAPTCHA_INPUT_SELECTORS = [
    "input[placeholder*='captcha' i]",
    "input[name='WordVerification']",
    "#MXX",
    "input[name='captcha']",
    "input[id*='captcha']",
    "input[placeholder*='verification' i]",
    "input:not([name*='username']):not([name*='password']):visible",
]


# ---------------------------------------------------------------------------
# Browser + session state helpers
# ---------------------------------------------------------------------------

def launch_chromium(p):
    """Headless (per settings) Chromium with Cloud-Run-safe flags."""
    from scraper0.config import get_settings
    return p.chromium.launch(headless=get_settings().browser_headless, args=BROWSER_ARGS)


def state_key(username: str) -> str:
    return f"storage_state:{username}"


def load_storage_state(state: StateStore, username: str) -> dict | None:
    raw = state.get(state_key(username))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError):
        log.warning("bbmp_storage_state_corrupt")
        return None


def save_storage_state(state: StateStore, username: str, storage_state: dict) -> None:
    state.set(state_key(username), json.dumps(storage_state))


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def check_user_logged_in(page) -> bool:
    try:
        welcome = page.locator("text=Welcome")
        if welcome.count() > 0 and welcome.first.is_visible(timeout=60000):
            log.info("bbmp_logged_in")
            return True
        login_button = page.locator("text=Login/Register")
        if login_button.count() > 0 and login_button.first.is_visible(timeout=60000):
            log.info("bbmp_not_logged_in")
            return False
        log.info("bbmp_login_status_unclear")
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("bbmp_login_check_failed", error=str(e))
        return False


def _find_captcha_input(modal_body):
    from playwright.sync_api import TimeoutError as PWTimeout

    for i, selector in enumerate(CAPTCHA_INPUT_SELECTORS):
        loc = modal_body.locator(selector)
        try:
            loc.wait_for(state="visible", timeout=60000 if i == 0 else 3000)
            return loc
        except PWTimeout:
            continue
    return None


def _screenshot_captcha(login_modal) -> bytes:
    frame = login_modal.frame_locator("#image")
    frame.locator("body").wait_for(state="visible", timeout=60000)
    try:
        return frame.locator("img").screenshot()
    except Exception:  # noqa: BLE001 — fall back to the frame body
        return frame.locator("body").screenshot()


def login_user(mobile: str, password: str, page, ai: AI,
               save_state: Callable[[dict], None] | None = None,
               progress: Callable[[str, dict], None] | None = None) -> tuple[bool, str]:
    """Log in on the already-open portal page. On success the context's storage_state is handed to
    `save_state`. Returns (success, message) with the legacy messages."""
    from playwright.sync_api import TimeoutError as PWTimeout

    try:
        if "smartoneblr.com" not in page.url:
            return False, "Unknown error in login_user"

        page.click("text=Login/Register")
        login_modal = page.locator(".modal-dialog.modal-sm", has=page.locator("#username"))
        login_modal.wait_for(state="visible", timeout=60000)
        login_modal.locator("#username").fill(mobile)
        login_modal.locator("#password").fill(password)
        page.wait_for_timeout(500)

        success = False
        for attempt in range(1, CAPTCHA_ATTEMPTS + 1):
            if progress:
                progress(f"Login: captcha attempt {attempt}", {"attempt": attempt})
            captcha_text = solve_captcha(_screenshot_captcha(login_modal), ai)
            log.info("bbmp_captcha_attempt", attempt=attempt, solved=bool(captcha_text))
            if not captcha_text:
                login_modal.locator("i.fa-refresh").click()
                page.wait_for_timeout(600)
                continue

            modal_body = login_modal.locator(".modal-body")
            captcha_input = _find_captcha_input(modal_body)
            if captcha_input is None:
                raise Exception("Cannot find captcha input element!")
            captcha_input.clear()
            captcha_input.scroll_into_view_if_needed()
            captcha_input.fill(captcha_text)
            page.wait_for_timeout(300)

            sign_in = modal_body.locator("button:has-text('Sign In')")
            sign_in.scroll_into_view_if_needed()
            sign_in.wait_for(state="visible", timeout=60000)
            page.wait_for_timeout(200)

            # The site validates the captcha client-side; only a correct captcha fires the DWR login
            # call, whose body contains "loginid:" for valid credentials. Read it via route.fetch()
            # (the page navigates the instant the response lands, so reading it afterwards races).
            dwr = {"body": None, "received": False}

            def _handle(route):  # exactly one parameter (Playwright inspects arity)
                response = route.fetch()
                try:
                    dwr["body"] = response.text()
                except Exception:  # noqa: BLE001
                    dwr["body"] = None
                dwr["received"] = True
                route.fulfill(response=response)

            page.route(LOGIN_DWR_PATTERN, _handle)
            sign_in.click()
            waited = 0
            while not dwr["received"] and waited < 15000:
                page.wait_for_timeout(200)
                waited += 200
            page.unroute(LOGIN_DWR_PATTERN, _handle)

            body = dwr["body"] if dwr["received"] else None
            if body is None:
                log.info("bbmp_captcha_rejected_client_side", attempt=attempt)
                login_modal.locator("i.fa-refresh").click()
                page.wait_for_timeout(800)
                continue
            if "loginid:" not in body:
                log.info("bbmp_credentials_rejected")
                return False, "Invalid mobile number or password"

            try:
                page.locator("text=Welcome").wait_for(state="visible", timeout=15000)
                success = True
                break
            except PWTimeout:
                try:
                    login_modal.wait_for(state="hidden", timeout=5000)
                    success = True
                    break
                except PWTimeout:
                    log.info("bbmp_login_incomplete_retry", attempt=attempt)
                    login_modal.locator("i.fa-refresh").click()
                    page.wait_for_timeout(800)

        if not success:
            return False, "Captcha solving failed after retries"

        if save_state:
            save_state(page.context.storage_state())
        log.info("bbmp_login_success")
        return True, "Login successful (captcha auto-solved)"

    except PWTimeout:
        log.warning("bbmp_login_timeout", exc_info=True)
        return False, "Login failed due to timeout"
    except Exception as e:  # noqa: BLE001
        log.warning("bbmp_login_error", exc_info=True)
        return False, f"Error: {e}"
