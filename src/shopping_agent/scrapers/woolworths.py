import asyncio
import base64
import json
import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from ..database import async_session
from ..log_utils import scrub
from ..models.product import Store
from ..models.store_cookies import StoreCookies
from ..config import (
    PLAYWRIGHT_DELAY_AFTER_EMAIL_MS,
    PLAYWRIGHT_DELAY_AFTER_HOMEPAGE_MS,
    PLAYWRIGHT_DELAY_AFTER_MFA_MS,
    PLAYWRIGHT_DELAY_AFTER_PASSWORD_MS,
    WOOLWORTHS_PRICE_FETCH_DELAY_S,
    WOOLWORTHS_PRICE_FETCH_JITTER_S,
    settings,
)
from .base import BaseScraper, ScrapedOrder, ScrapedOrderItem, ScrapedProduct


@dataclass
class _PendingLogin:
    """Holds live Playwright objects while waiting for an MFA code."""
    playwright: Any
    context: Any  # BrowserContext
    page: Any
    created_at: float = field(default_factory=time.time)

logger = logging.getLogger(__name__)

WOOLWORTHS_BASE = "https://www.woolworths.com.au"
MOBILE_API_BASE = "https://prod.mobile-api.woolworths.com.au"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Origin": WOOLWORTHS_BASE,
    "Referer": f"{WOOLWORTHS_BASE}/",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


class WoolworthsScraper(BaseScraper):
    store = Store.WOOLWORTHS
    _cookie_domain: str = ".woolworths.com.au"

    def __init__(self, user_id: uuid.UUID | None = None) -> None:
        self.user_id = user_id
        self._client: httpx.AsyncClient | None = None
        self._price_client: httpx.AsyncClient | None = None
        self._pending_login: _PendingLogin | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the httpx client with current cookies.

        Always bootstraps Akamai session cookies (_abck etc.) on every new client
        creation, regardless of whether user login cookies are stored.  Akamai session
        cookies are short-lived and required for all API access; user login cookies are
        only needed for authenticated endpoints (order history, cart).
        """
        if self._client is None or self._client.is_closed:
            cookies = await self._load_cookies()
            self._client = httpx.AsyncClient(
                base_url=WOOLWORTHS_BASE,
                cookies=cookies,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    **DEFAULT_HEADERS,
                },
                follow_redirects=True,
                timeout=30.0,
            )
            await self._bootstrap_akamai_cookies()
        return self._client

    async def _get_price_client(self) -> httpx.AsyncClient:
        """Get a lightweight client for unauthenticated price fetching.

        Unlike _get_client(), this client never loads stored user login cookies.
        It only carries the basic Akamai session cookies obtained from the homepage,
        which is sufficient for the public product-detail API.  This avoids the
        situation where expired user cookies trigger Akamai 403s on price refreshes.
        """
        if self._price_client is None or self._price_client.is_closed:
            self._price_client = httpx.AsyncClient(
                base_url=WOOLWORTHS_BASE,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    **DEFAULT_HEADERS,
                },
                follow_redirects=True,
                timeout=30.0,
            )
            # Visit homepage to pick up basic Akamai session cookies (bm_s, bm_sz, etc.)
            try:
                await self._price_client.get(
                    "/", headers={"Accept": "text/html,application/xhtml+xml,*/*"}
                )
            except Exception:
                logger.warning("[Woolworths] Price-client homepage seed failed", exc_info=True)
        return self._price_client

    async def _bootstrap_akamai_cookies(self) -> None:
        """Visit the Woolworths homepage to obtain Akamai bot-session cookies.

        The Akamai CDN requires session cookies (_abck, bm_s, etc.) to let API
        requests through.  A real browser obtains these automatically; we get
        them by making a single homepage request, which causes the server to
        set them via HTTP Set-Cookie headers.  No login is required.

        Cookies are persisted so subsequent restarts don't need to re-bootstrap.
        """
        if self._client is None:
            return
        try:
            logger.info("[Woolworths] Bootstrapping Akamai session cookies from homepage")
            resp = await self._client.get(
                "/",
                headers={"Accept": "text/html,application/xhtml+xml,*/*"},
            )
            if resp.status_code == 200 and self._client.cookies.get("_abck"):
                logger.info("[Woolworths] Akamai bootstrap succeeded")
                await self._save_cookies_from_client()
            else:
                logger.warning(
                    "[Woolworths] Bootstrap did not yield _abck cookie (status=%d)",
                    resp.status_code,
                )
        except Exception:
            logger.warning("[Woolworths] Failed to bootstrap Akamai cookies", exc_info=True)

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> httpx.Response | None:
        """Make a request to woolworths.com.au, handling auth failures gracefully."""
        client = await self._get_client()
        params = kwargs.get("params", {})
        params_str = f" {params}" if params else ""
        logger.info("[Woolworths] → %s %s%s", method, path, params_str)
        t0 = time.perf_counter()
        try:
            resp = await client.request(method, path, **kwargs)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "[Woolworths] ← %d %s %s (%.0f ms)",
                resp.status_code, method, path, elapsed_ms,
            )
            if resp.status_code in (401, 403):
                try:
                    body_snippet = resp.text[:500]
                except Exception:
                    body_snippet = "<unreadable>"
                logger.warning(
                    "[Woolworths] Auth failure (%d) on %s %s — body: %s",
                    resp.status_code, method, path, body_snippet,
                )
                # Close and discard the client so _get_client() recreates it with a
                # fresh Akamai bootstrap on the next request.  Simply nulling the client
                # is not enough: without the null the next call reuses the same blocked
                # session; with the null but without always-bootstrapping, the recreated
                # client would just reload the same stale cookies.
                if self._client and not self._client.is_closed:
                    await self._client.aclose()
                self._client = None
                return None
            return resp
        except httpx.HTTPError:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.exception(
                "[Woolworths] HTTP error on %s %s (%.0f ms)", method, path, elapsed_ms
            )
            return None

    def _decode_jwt_claims(self, token: str) -> dict:
        """Decode the payload claims from a JWT without verifying signature."""
        try:
            payload = token.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            return json.loads(base64.b64decode(payload))
        except Exception:
            return {}

    async def _get_auth_token(self) -> str:
        """Get the wow-auth-token value from stored cookies in the database."""
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(StoreCookies).where(
                        StoreCookies.store == Store.WOOLWORTHS,
                        StoreCookies.user_id == self.user_id,
                    )
                )
                row = result.scalar_one_or_none()
                if not row:
                    return ""
                cookie_dict = {c["name"]: c["value"] for c in json.loads(row.cookies_json)}
                return cookie_dict.get("wow-auth-token") or cookie_dict.get("prodwow-auth-token", "")
        except Exception:
            return ""

    def _is_token_expired(self, token: str) -> bool:
        """Return True if the JWT is expired or expiring within 60 seconds."""
        import time
        claims = self._decode_jwt_claims(token)
        exp = claims.get("exp", 0)
        return time.time() > (exp - 60)

    async def _get_shopper_id(self) -> str | None:
        """Decode the shopper ID from the wow-auth-token JWT."""
        try:
            token = await self._get_auth_token()
            if not token:
                return None
            claims = self._decode_jwt_claims(token)
            return str(claims.get("sid") or claims.get("aub") or "")
        except Exception:
            logger.debug("Failed to decode shopper ID from JWT", exc_info=True)
            return None

    async def _refresh_auth_token(self) -> str:
        """Use the site cookies to get a fresh JWT from the token refresh endpoint."""
        client = await self._get_client()
        try:
            resp = await client.post("/api/ui/v2/token/refresh")
            if resp.status_code == 200:
                new_token = resp.cookies.get("wow-auth-token") or resp.cookies.get("prodwow-auth-token")
                if new_token:
                    logger.info("Refreshed Woolworths auth token")
                    # Persist the updated cookies (includes the new token cookie)
                    await self._save_cookies_from_client()
                    return new_token
        except httpx.HTTPError:
            logger.warning("Token refresh request failed", exc_info=True)
        return ""

    async def _get_valid_auth_token(self) -> str:
        """Return a valid (non-expired) JWT, refreshing if needed."""
        token = await self._get_auth_token()
        if not token or self._is_token_expired(token):
            logger.info("Woolworths auth token expired, refreshing...")
            token = await self._refresh_auth_token()
        return token

    async def _mobile_request(
        self, method: str, path: str, **kwargs
    ) -> httpx.Response | None:
        """Make a request to the Woolworths mobile API, auto-refreshing the JWT."""
        auth_token = await self._get_valid_auth_token()
        if not auth_token:
            logger.warning("[Woolworths Mobile] No valid auth token")
            return None

        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-AU,en;q=0.9",
            "Authorization": f"Bearer {auth_token}",
            "X-Api-Key": settings.woolworths_api_key or "",
            "Origin": WOOLWORTHS_BASE,
            "Referer": f"{WOOLWORTHS_BASE}/",
        }
        params = kwargs.get("params", {})
        params_str = f" {params}" if params else ""
        logger.info("[Woolworths Mobile] → %s %s%s", method, path, params_str)
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                base_url=MOBILE_API_BASE,
                headers=headers,
                follow_redirects=True,
                timeout=30.0,
            ) as client:
                resp = await client.request(method, path, **kwargs)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.info(
                    "[Woolworths Mobile] ← %d %s %s (%.0f ms)",
                    resp.status_code, method, path, elapsed_ms,
                )
                if resp.status_code in (401, 403):
                    logger.warning(
                        "[Woolworths Mobile] Auth failure (%d) on %s %s",
                        resp.status_code, method, path,
                    )
                    return None
                return resp
        except httpx.HTTPError:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            logger.exception(
                "[Woolworths Mobile] HTTP error on %s %s (%.0f ms)", method, path, elapsed_ms
            )
            return None

    # ── Auth ─────────────────────────────────────────────────────────

    async def is_authenticated(self) -> bool:
        """Return True if Woolworths cookies are stored in the database."""
        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(
                    StoreCookies.store == Store.WOOLWORTHS,
                    StoreCookies.user_id == self.user_id,
                )
            )
            row = result.scalar_one_or_none()
            if not row:
                return False
            try:
                return len(json.loads(row.cookies_json)) > 0
            except Exception:
                return False

    async def import_cookies(self, cookie_json: str) -> bool:
        """Import cookies from a JSON string (e.g. from Cookie-Editor extension)."""
        try:
            raw_cookies = json.loads(cookie_json)
            if not isinstance(raw_cookies, list) or not raw_cookies:
                return False

            # Normalise to Playwright-compatible format
            normalised = []
            for c in raw_cookies:
                normalised.append(
                    {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ".woolworths.com.au"),
                        "path": c.get("path", "/"),
                        "secure": c.get("secure", False),
                        "httpOnly": c.get("httpOnly", False),
                    }
                )

            cookies_json = json.dumps(normalised, indent=2)
            async with async_session() as session:
                result = await session.execute(
                    select(StoreCookies).where(
                        StoreCookies.store == Store.WOOLWORTHS,
                        StoreCookies.user_id == self.user_id,
                    )
                )
                row = result.scalar_one_or_none()
                if row:
                    row.cookies_json = cookies_json
                else:
                    session.add(StoreCookies(store=Store.WOOLWORTHS, user_id=self.user_id, cookies_json=cookies_json))
                await session.commit()
            logger.info("Imported %d cookies for woolworths", len(normalised))

            # Reset client so it picks up new cookies
            if self._client and not self._client.is_closed:
                await self._client.aclose()
                self._client = None

            return True
        except Exception:
            logger.exception("Failed to import Woolworths cookies")
            return False

    async def validate_cookies(self) -> dict:
        """Make a real API call to verify the stored cookies actually work.
        Returns {"ok": bool, "detail": str}.
        """
        if not await self.is_authenticated():
            return {"ok": False, "detail": "No cookies stored"}
        shopper_id = await self._get_shopper_id()
        if not shopper_id:
            return {"ok": False, "detail": "No valid auth token (wow-auth-token) found in cookies"}
        resp = await self._mobile_request(
            "GET",
            "/wow/v1/orders/api/orders",
            params={"shopperId": shopper_id, "pageNumber": 1, "pageSize": 1},
        )
        if resp is None:
            return {"ok": False, "detail": "API returned 401/403 — cookies expired or invalid"}
        if resp.status_code == 200:
            return {"ok": True, "detail": f"API reachable (HTTP {resp.status_code})"}
        return {"ok": False, "detail": f"Unexpected response: HTTP {resp.status_code}"}

    async def login_interactive(self) -> bool:
        """Not supported; use login_with_credentials() instead."""
        return False

    async def login_with_credentials(
        self,
        email: str,
        password: str,
        on_progress: Callable[[str], None] | None = None,
    ) -> str:
        """Use Playwright to log into Woolworths with email/password.

        Returns one of:
          "ok"           – login succeeded and cookies are stored.
          "mfa_required" – an MFA code is needed; call complete_mfa() next.
          "failed:<msg>" – login failed with the given reason.
        """
        def _progress(msg: str) -> None:
            logger.info("[Woolworths] Playwright: %s", msg)
            if on_progress:
                on_progress(msg)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return "failed:playwright not installed — run: pip install playwright && playwright install chromium"

        await self.cancel_pending_login()

        pw = await async_playwright().start()
        page = None
        try:
            _progress("Launching browser")
            import tempfile
            raw_dir = settings.woolworths_playwright_profile_dir or (
                str(Path(settings.playwright_profile_dir) / "woolworths")
                if settings.playwright_profile_dir
                else None
            )
            if raw_dir:
                try:
                    Path(raw_dir).mkdir(parents=True, exist_ok=True)
                    user_data_dir = raw_dir
                except OSError:
                    logger.warning(
                        "[Woolworths] Cannot use configured profile dir %s; falling back to tempdir",
                        raw_dir,
                    )
                    user_data_dir = tempfile.mkdtemp(prefix="woolworths-playwright-")
            else:
                user_data_dir = tempfile.mkdtemp(prefix="woolworths-playwright-")

            launch_kwargs: dict = {}
            if settings.playwright_channel:
                launch_kwargs["channel"] = settings.playwright_channel
            context = await pw.chromium.launch_persistent_context(
                user_data_dir,
                headless=settings.playwright_headless,
                **launch_kwargs,
                args=["--disable-blink-features=AutomationControlled"],
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 800},
            )

            # Preserve Akamai bot-detection cookies across sessions so Woolworths
            # doesn't flag the browser as a new/untrusted bot on every login.
            all_cookies = await context.cookies()
            keep = [c for c in all_cookies if c["name"] in ("_abck", "ak_bmsc", "bm_sz")]
            await context.clear_cookies()
            if keep:
                await context.add_cookies(keep)  # type: ignore[arg-type]

            try:
                from playwright_stealth import Stealth  # type: ignore[import-untyped]
                await Stealth().apply_stealth_async(context)
            except ImportError:
                logger.warning("[Woolworths] playwright-stealth not installed; browser may be detected as automated")
                await context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )

            page = await context.new_page()

            _progress("Navigating to Woolworths homepage")
            try:
                await page.goto("https://www.woolworths.com.au/", wait_until="load", timeout=15000)
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass
            await page.wait_for_timeout(PLAYWRIGHT_DELAY_AFTER_HOMEPAGE_MS)

            _progress("Navigating to login page")
            try:
                # Click the "Log in or Sign up" button to trigger the OAuth redirect
                login_btn = page.locator("button").filter(has_text="Log in or Sign up")
                await login_btn.first.click(timeout=10000)
                await page.wait_for_url("**/auth.woolworths.com.au/**", timeout=15000)
            except Exception:
                # Fall back: navigate to account page which triggers redirect
                try:
                    await page.goto("https://www.woolworths.com.au/shop/myaccount", wait_until="load", timeout=15000)
                    await page.wait_for_url("**/auth.woolworths.com.au/**", timeout=15000)
                except Exception:
                    # Best-effort browser step; failures here are non-fatal and ignored.
                    pass

            current_url = page.url
            logger.info("[Woolworths] Playwright: auth URL: %s", current_url)

            # Already logged in?
            if "www.woolworths.com.au" in current_url and "auth." not in current_url:
                _progress("Already logged in — saving cookies")
                cookies = await context.cookies()
                await context.close()
                await pw.stop()
                return await self._finish_playwright_login(cookies)  # type: ignore[arg-type]

            if "auth.woolworths.com.au" not in current_url:
                await context.close()
                await pw.stop()
                return "failed:Could not reach Woolworths login page"

            # Fill email — Auth0 renders the field as name="username" / id="username"
            email_selector = 'input[name="username"], #username'
            await page.wait_for_selector(email_selector, timeout=10000)
            await page.fill(email_selector, email)
            _progress("Filled email")
            await page.wait_for_timeout(PLAYWRIGHT_DELAY_AFTER_EMAIL_MS)

            await page.get_by_role("button", name="Log in", exact=True).click()

            # Wait for password field (hidden on page load, shown after email submit)
            try:
                await page.wait_for_selector('input[type="password"]', timeout=15000)
            except Exception:
                error_msg = await self._extract_page_error(page)
                await context.close()
                await pw.stop()
                return f"failed:{error_msg or 'Could not find password field after email step'}"

            await page.fill('input[type="password"]', password)
            _progress("Filled password — about to submit")
            await page.wait_for_timeout(PLAYWRIGHT_DELAY_AFTER_PASSWORD_MS)

            await page.get_by_role("button", name="Log in", exact=True).click()

            _progress("Waiting for redirect")
            try:
                await page.wait_for_url(
                    lambda url: "www.woolworths.com.au" in url or self._looks_like_mfa(url, ""),
                    timeout=20000,
                )
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass

            current_url = page.url
            logger.info("[Woolworths] Playwright: post-submit URL: %s", current_url)

            if "www.woolworths.com.au" in current_url:
                _progress("Login successful — saving cookies")
                cookies = await context.cookies()
                await context.close()
                await pw.stop()
                return await self._finish_playwright_login(cookies)  # type: ignore[arg-type]

            page_text = (await page.inner_text("body")).lower()
            if self._looks_like_mfa(current_url, page_text):
                logger.info("[Woolworths] Playwright: MFA detected at URL %s — page text: %s", current_url, page_text[:300])

                # mfa-phone-challenge: user must choose SMS/Call then click Continue.
                # Do this automatically so the SMS is sent before we ask for the code.
                if "mfa-phone-challenge" in current_url:
                    _progress("Sending MFA code via SMS…")
                    try:
                        await page.get_by_role("button", name="Continue").click()
                        await page.wait_for_url("**/mfa-sms-challenge**", timeout=15000)
                        logger.info("[Woolworths] Playwright: SMS sent, now on %s", page.url)
                    except Exception as sms_exc:
                        logger.warning("[Woolworths] Playwright: could not auto-advance past phone-challenge: %s", sms_exc)

                _progress("MFA code required")
                self._pending_login = _PendingLogin(playwright=pw, context=context, page=page)
                return "mfa_required"

            error_msg = await self._extract_page_error(page)
            await context.close()
            await pw.stop()
            return f"failed:{error_msg or 'Login unsuccessful — check your credentials'}"

        except Exception as exc:
            try:
                if page is not None:
                    logger.error(
                        "[Woolworths] Playwright login error — url=%s error=%s",
                        page.url, exc,
                    )
                else:
                    logger.error("[Woolworths] Playwright login error (before page created): %s", exc)
            except Exception:
                logger.exception("[Woolworths] Playwright login error")
            try:
                await pw.stop()
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass
            return f"failed:{exc}"

    async def complete_mfa(self, code: str) -> str:
        """Submit the MFA code for a pending Playwright login.

        Returns "ok" or "failed:<msg>".
        """
        if not self._pending_login:
            return "failed:No pending login — please start the login process again"

        pending = self._pending_login
        if time.time() - pending.created_at > 600:
            await self.cancel_pending_login()
            return "failed:Login session expired (10 min limit) — please start again"

        try:
            page = pending.page
            logger.info("[Woolworths] Playwright MFA: current URL = %s", page.url)
            try:
                page_body = (await page.inner_text("body"))[:500]
                logger.info("[Woolworths] Playwright MFA: page text = %s", page_body)
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass

            # If the page is showing a push/Guardian screen, try to switch to OTP entry
            try:
                for try_another in (
                    page.get_by_role("button", name="Try another method"),
                    page.get_by_role("link", name="Try another method"),
                    page.get_by_role("button", name="Use a one-time code"),
                    page.get_by_role("link", name="Use a one-time code"),
                    page.get_by_role("button", name="Enter a code"),
                ):
                    if await try_another.is_visible(timeout=500):
                        await try_another.click()
                        await page.wait_for_timeout(1000)
                        break
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass

            mfa_selector = (
                'input[name="code"], '
                'input[autocomplete="one-time-code"], '
                'input[type="tel"], '
                'input[inputmode="numeric"], '
                'input[name="credentials.passcode"], '
                'input[id="code"], '
                'input[placeholder*="code" i], '
                'input[placeholder*="passcode" i]'
            )

            # Detect how many visible inputs the page has — Auth0 SMS pages sometimes
            # use 6 individual single-digit boxes rather than one full-length input.
            found_input = False
            try:
                await page.wait_for_selector(mfa_selector, timeout=15000)
                all_inputs = await page.locator(mfa_selector).all()
                logger.info("[Woolworths] Playwright MFA: matched inputs = %d", len(all_inputs))
                for inp in all_inputs:
                    attrs = await inp.evaluate(
                        "el => ({name: el.name, type: el.type, id: el.id, maxlength: el.maxLength})"
                    )
                    logger.info("[Woolworths] Playwright MFA: input attrs = %s", attrs)

                if len(all_inputs) >= 6:
                    # Individual digit boxes — fill one character per box
                    for i, digit in enumerate(code):
                        if i < len(all_inputs):
                            await all_inputs[i].click()
                            await all_inputs[i].fill(digit)
                            await page.wait_for_timeout(50)
                else:
                    # Single code input — use fill() to properly trigger React onChange
                    inp = all_inputs[0] if all_inputs else page.locator(mfa_selector).first
                    await inp.click()
                    await inp.fill(code)

                found_input = True
            except Exception as e:
                logger.error("[Woolworths] Playwright MFA: input fill failed: %s", e)

            if not found_input:
                return "failed:Could not find MFA input field — check logs for page details"

            await page.wait_for_timeout(PLAYWRIGHT_DELAY_AFTER_MFA_MS)

            # Submit — try named buttons with short timeouts so we don't hang 30s on each
            submitted = False
            for btn_name in ("Continue", "Verify", "Submit"):
                try:
                    btn = page.get_by_role("button", name=btn_name, exact=True)
                    if await btn.is_visible(timeout=1500):
                        await btn.click()
                        submitted = True
                        break
                except Exception:
                    # Best-effort browser step; failures here are non-fatal and ignored.
                    pass
            if not submitted:
                try:
                    await page.locator('button[type="submit"]').first.click(timeout=3000)
                except Exception:
                    # Best-effort browser step; failures here are non-fatal and ignored.
                    pass

            try:
                await page.wait_for_url(
                    lambda url: "www.woolworths.com.au" in url,
                    timeout=30000,
                )
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass

            current_url = page.url
            logger.info("[Woolworths] Playwright MFA: post-submit URL: %s", current_url)

            if "www.woolworths.com.au" in current_url:
                cookies = await pending.context.cookies()
                await pending.context.close()
                await pending.playwright.stop()
                self._pending_login = None
                return await self._finish_playwright_login(cookies)  # type: ignore[arg-type]

            # MFA failed — look for an error on the page
            mfa_error: str | None = None
            try:
                for sel in ['[role="alert"]', 'p[class*="error"]', 'p[class*="Error"]']:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=500):
                        mfa_error = (await el.inner_text()).strip()[:200]
                        break
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass
            # The page-derived mfa_error is returned to the caller below but kept
            # out of the log: the login page content is treated as sensitive.
            logger.error("[Woolworths] Playwright MFA failed; still on auth domain after submit")
            return f"failed:{mfa_error or 'Invalid or expired MFA code — please try again'}"

        except Exception as exc:
            logger.exception("[Woolworths] Playwright MFA error")
            return f"failed:{exc}"

    async def cancel_pending_login(self) -> None:
        """Tear down any in-progress Playwright login session."""
        if self._pending_login:
            try:
                await self._pending_login.context.close()
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass
            try:
                await self._pending_login.playwright.stop()
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass
            self._pending_login = None

    async def _finish_playwright_login(self, cookies: list[dict]) -> str:
        """Store Woolworths cookies captured from a Playwright session."""
        ww_cookies = [c for c in cookies if "woolworths" in c.get("domain", "").lower()]
        if not ww_cookies:
            return "failed:No Woolworths cookies found after login — login may not have completed"
        success = await self.import_cookies(json.dumps(ww_cookies))
        return "ok" if success else "failed:Could not persist the captured cookies"

    @staticmethod
    def _looks_like_mfa(url: str, page_text: str) -> bool:
        url_lower = url.lower()
        mfa_url_hints = ("mfa", "otp", "challenge", "verify", "step-up", "factor", "authenticate")
        mfa_text_hints = (
            "verify your identity", "authentication code", "enter code",
            "one-time", "passcode", "authenticator", "sms code",
            "check your mobile", "mobile for a code", "verification code",
        )
        return (
            any(h in url_lower for h in mfa_url_hints)
            or any(h in page_text for h in mfa_text_hints)
        )

    @staticmethod
    async def _extract_page_error(page: Any) -> str | None:
        """Try to read a visible error message from the current page."""
        selectors = [
            '[role="alert"]',
            '[data-testid*="error"]',
            '[class*="error"]',
            '[class*="alert"]',
            'p[class*="Error"]',
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=500):
                    text = (await el.inner_text()).strip()
                    if text:
                        return text[:200]
            except Exception:
                # Best-effort browser step; failures here are non-fatal and ignored.
                pass
        return None

    async def logout(self) -> None:
        """Delete stored Woolworths cookies and close the HTTP client."""
        async with async_session() as session:
            result = await session.execute(
                select(StoreCookies).where(
                    StoreCookies.store == Store.WOOLWORTHS,
                    StoreCookies.user_id == self.user_id,
                )
            )
            row = result.scalar_one_or_none()
            if row:
                await session.delete(row)
                await session.commit()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── Order History ────────────────────────────────────────────────

    async def get_order_history(self, limit: int = 10) -> list[ScrapedOrder]:
        """Fetch up to `limit` past orders from Woolworths via the mobile API.

        Retrieves the order list and then fetches full item details for each order.

        Args:
            limit: Maximum number of orders to return.

        Returns:
            List of ScrapedOrder objects with items populated.
        """
        orders: list[ScrapedOrder] = []
        try:
            shopper_id = await self._get_shopper_id()
            if not shopper_id:
                logger.warning("Could not determine Woolworths shopper ID from token")
                return orders

            # List orders from mobile API
            resp = await self._mobile_request(
                "GET",
                "/wow/v1/orders/api/orders",
                params={"shopperId": shopper_id, "pageNumber": 1, "pageSize": limit},
            )
            if not resp or resp.status_code != 200:
                logger.warning("Failed to list Woolworths orders from mobile API")
                return orders

            order_list = resp.json().get("items") or []
            logger.info("Found %d Woolworths orders", len(order_list))

            # Fetch full details (with items) for each order
            for summary in order_list[:limit]:
                order_id = summary.get("OrderId")
                if not order_id:
                    continue
                detail_resp = await self._mobile_request(
                    "GET", f"/wow/v1/orders/api/orders/{order_id}"
                )
                if detail_resp and detail_resp.status_code == 200:
                    order = self._parse_mobile_order(str(order_id), summary, detail_resp.json())
                    if order:
                        orders.append(order)
                else:
                    # Fall back to summary only
                    order = self._parse_mobile_order(str(order_id), summary, {})
                    if order:
                        orders.append(order)

            await self._save_cookies_from_client()
        except Exception:
            logger.exception("Failed to fetch Woolworths order history")

        return orders

    # ── Product Search ───────────────────────────────────────────────

    async def search_product(self, query: str) -> list[ScrapedProduct]:
        """Search Woolworths for products matching the query.

        Args:
            query: Free-text search query.

        Returns:
            List of ScrapedProduct results from the Woolworths search API.
        """
        products: list[ScrapedProduct] = []
        try:
            resp = await self._request(
                "GET",
                "/apis/ui/Search/products",
                params={
                    "searchTerm": query,
                    "pageNumber": 1,
                    "pageSize": 20,
                    "sortType": "TraderRelevance",
                },
            )
            if resp and resp.status_code == 200:
                result = resp.json()
                for item in (
                    result.get("Products")
                    or result.get("products")
                    or result.get("Items")
                    or []
                ):
                    product_data = (
                        item.get("Products", [item])
                        if isinstance(item, dict)
                        else [item]
                    )
                    for pd in product_data:
                        p = self._parse_search_result(pd)
                        if p:
                            products.append(p)
        except Exception:
            logger.exception("Woolworths search failed for: %s", scrub(query))

        return products

    # ── Product Price ────────────────────────────────────────────────

    async def get_product_price(self, store_product_id: str, product_name: str | None = None, timeout: float | None = None) -> ScrapedProduct | None:
        """Fetch the current price for a specific Woolworths product.

        Args:
            store_product_id: Woolworths stockcode to look up.
            product_name: Unused; kept for interface compatibility.

        Returns:
            ScrapedProduct with current price, or None if the product no longer exists.

        Raises:
            RuntimeError: If the request fails due to auth/network issues (transient —
                callers should not mark the product as not_found in this case).
        """
        if WOOLWORTHS_PRICE_FETCH_DELAY_S or WOOLWORTHS_PRICE_FETCH_JITTER_S:
            jitter = random.uniform(0.0, WOOLWORTHS_PRICE_FETCH_JITTER_S) if WOOLWORTHS_PRICE_FETCH_JITTER_S else 0.0
            await asyncio.sleep(WOOLWORTHS_PRICE_FETCH_DELAY_S + jitter)
        # Use the unauthenticated price client — stored user login cookies are not
        # needed for the public product-detail endpoint and cause Akamai 403s when
        # they expire.
        client = await self._get_price_client()
        kwargs: dict = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = await client.get(f"/apis/ui/product/detail/{store_product_id}", **kwargs)
        except httpx.HTTPError as e:
            if self._price_client and not self._price_client.is_closed:
                await self._price_client.aclose()
            self._price_client = None
            raise RuntimeError(f"Woolworths price fetch network error for {store_product_id}: {e}") from e
        if resp.status_code in (401, 403):
            # Session rejected — discard price client so next call re-seeds from homepage.
            if self._price_client and not self._price_client.is_closed:
                await self._price_client.aclose()
            self._price_client = None
            raise RuntimeError(
                f"Woolworths auth failure ({resp.status_code}) for product {store_product_id}"
            )
        if resp.status_code == 200:
            try:
                result = resp.json()
                product_data = result.get("Product") or result
                logger.debug(
                    "[WW price] %s raw fields: IsAvailable=%s IsInStoreOnly=%s IsPurchasable=%s Price=%s",
                    store_product_id,
                    product_data.get("IsAvailable"),
                    product_data.get("IsInStoreOnly"),
                    product_data.get("IsPurchasable"),
                    product_data.get("Price"),
                )
                return self._parse_search_result(product_data)
            except Exception:
                logger.exception("Woolworths price parse failed for: %s", store_product_id)
                raise
        # Non-200 response (e.g. 404) means the product genuinely no longer exists.
        return None

    # ── Add to Cart ──────────────────────────────────────────────────

    async def add_to_cart(self, items: list[tuple[str, int]]) -> dict[str, bool]:
        """Add items to the Woolworths cart, trying multiple payload formats.

        Attempts several API payload structures in order until one succeeds.

        Args:
            items: List of (store_product_id, quantity) tuples to add.

        Returns:
            Dict mapping store_product_id to True if added successfully, False otherwise.
        """
        client = await self._get_client()
        akamai_cookies = {
            name: "present" if value else "empty"
            for name, value in {
                "_abck": client.cookies.get("_abck"),
                "ak_bmsc": client.cookies.get("ak_bmsc"),
                "bm_sz": client.cookies.get("bm_sz"),
            }.items()
        }
        logger.info("[Woolworths] Akamai cookie state before add-to-cart: %s", akamai_cookies)

        attempts = [
            ("/apis/ui/Trolley/items", "POST", lambda pid, qty: {"Stockcode": pid, "Quantity": qty, "IsInTrolley": False}),
            ("/apis/ui/Trolley/items", "POST", lambda pid, qty: [{"Stockcode": pid, "Quantity": qty, "IsInTrolley": False}]),
            ("/apis/ui/Trolley/items", "POST", lambda pid, qty: {"items": [{"Stockcode": pid, "Quantity": qty, "IsInTrolley": False}]}),
        ]
        results: dict[str, bool] = {}
        try:
            for product_id, quantity in items:
                success = False
                try:
                    for endpoint, method, make_payload in attempts:
                        payload = make_payload(int(product_id), quantity)
                        resp = await self._request(method, endpoint, json=payload)
                        body = resp.text[:300] if resp else None
                        logger.info(
                            "Woolworths add-to-cart %s %s product=%s status=%s body=%s",
                            method, endpoint, product_id,
                            resp.status_code if resp else None,
                            body,
                        )
                        if resp and resp.status_code == 200 and resp.text.strip().startswith("{"):
                            data = resp.json()
                            if data.get("AvailableItems") or data.get("BundleItems"):
                                success = True
                                break
                except Exception:
                    logger.exception("Woolworths add to cart failed for product %s", product_id)
                if not success:
                    logger.warning("Failed to add Woolworths product %s to cart", product_id)
                results[str(product_id)] = success
            await self._save_cookies_from_client()
        except Exception:
            logger.exception("Woolworths add to cart failed")
            for product_id, _ in items:
                if str(product_id) not in results:
                    results[str(product_id)] = False
        return results

    async def get_cart_url(self) -> str:
        """Return the Woolworths homepage URL for the user to review/submit their cart."""
        return WOOLWORTHS_BASE

    # ── Parsing helpers ──────────────────────────────────────────────

    def _parse_mobile_order(self, order_id: str, summary: dict, detail: dict) -> ScrapedOrder | None:
        """Parse an order from the mobile API list + detail responses."""
        try:
            date_str = summary.get("CreatedDate") or summary.get("OriginalOrderCreatedDate") or ""
            try:
                order_date = datetime.fromisoformat(date_str).date()
            except (ValueError, AttributeError):
                order_date = datetime.now().date()

            items = []
            for product in detail.get("OrderProducts") or []:
                ordered = product.get("Ordered") or {}
                supplied = product.get("Supplied") or {}
                stock_code = str(ordered.get("StockCode") or "")
                if not stock_code:
                    continue
                # Use supplied quantity if available (accounts for substitutions/out-of-stock)
                quantity = int(supplied.get("Quantity") or ordered.get("Quantity") or 1)
                price = float(
                    ordered.get("SalePrice", {}).get("Value")
                    or ordered.get("Total")
                    or 0
                )
                items.append(ScrapedOrderItem(
                    store_product_id=stock_code,
                    name=ordered.get("Name") or "Unknown",
                    quantity=quantity,
                    price_paid=price,
                    brand=ordered.get("Brand"),
                    unit_size=ordered.get("Size"),
                    image_url=f"https://cdn0.woolworths.media/content/wowproductimages/large/{stock_code}.jpg",
                ))

            return ScrapedOrder(
                store_order_id=order_id,
                order_date=order_date,
                total_amount=float(summary.get("Total") or detail.get("Total") or 0),
                status=summary.get("CurrentStatus") or detail.get("CurrentStatus"),
                items=items,
            )
        except Exception:
            logger.debug("Failed to parse Woolworths mobile order %s", order_id, exc_info=True)
            return None

    def _parse_invoice(self, data: dict) -> ScrapedOrder | None:
        """Parse an invoice summary from /api/v3/ui/invoices/search."""
        try:
            invoice_id = str(data.get("InvoiceId") or "")
            if not invoice_id:
                return None

            # CollectionDate is a human-readable string like "22 February 2026"
            date_str = data.get("CollectionDate") or ""
            try:
                from datetime import datetime
                order_date = datetime.strptime(date_str, "%d %B %Y").date()
            except (ValueError, AttributeError):
                order_date = datetime.now().date()

            return ScrapedOrder(
                store_order_id=invoice_id,
                order_date=order_date,
                total_amount=float(data.get("Total") or 0),
                status=data.get("CollectionType"),
                items=[],  # Item details not available from summary API
            )
        except Exception:
            logger.debug("Failed to parse Woolworths invoice", exc_info=True)
            return None

    def _parse_api_order(self, data: dict) -> ScrapedOrder | None:
        """Parse an order from the Woolworths REST API response format."""
        try:
            order_id = str(
                data.get("OrderId")
                or data.get("orderId")
                or data.get("orderNumber")
                or data.get("id", "")
            )
            if not order_id:
                return None

            date_str = (
                data.get("OrderDate")
                or data.get("orderDate")
                or data.get("DeliveryDate")
                or data.get("deliveryDate")
                or ""
            )
            try:
                order_date = datetime.fromisoformat(
                    date_str.replace("Z", "+00:00")
                ).date()
            except (ValueError, AttributeError):
                order_date = datetime.now().date()

            items = []
            for item_data in (
                data.get("OrderItems")
                or data.get("items")
                or data.get("Products")
                or []
            ):
                item = ScrapedOrderItem(
                    store_product_id=str(
                        item_data.get("Stockcode")
                        or item_data.get("stockcode")
                        or item_data.get("productId")
                        or item_data.get("id", "")
                    ),
                    name=(
                        item_data.get("DisplayName")
                        or item_data.get("name")
                        or item_data.get("Name")
                        or "Unknown"
                    ),
                    quantity=int(
                        item_data.get("Quantity") or item_data.get("quantity") or 1
                    ),
                    price_paid=float(
                        item_data.get("SalePrice")
                        or item_data.get("price")
                        or item_data.get("Price")
                        or 0
                    ),
                    brand=item_data.get("Brand") or item_data.get("brand"),
                    unit_size=(
                        item_data.get("PackageSize")
                        or item_data.get("packageSize")
                        or item_data.get("Size")
                    ),
                    image_url=(
                        item_data.get("MediumImageFile")
                        or item_data.get("imageUrl")
                        or item_data.get("ImageUrl")
                    ),
                )
                items.append(item)

            return ScrapedOrder(
                store_order_id=order_id,
                order_date=order_date,
                total_amount=float(
                    data.get("TotalPrice")
                    or data.get("totalAmount")
                    or data.get("Total")
                    or 0
                ),
                status=data.get("Status") or data.get("status"),
                items=items,
            )
        except Exception:
            logger.debug("Failed to parse Woolworths order", exc_info=True)
            return None

    def _parse_search_result(self, data: dict) -> ScrapedProduct | None:
        """Parse a product from a Woolworths search or product detail response."""
        try:
            return ScrapedProduct(
                store_product_id=str(
                    data.get("Stockcode") or data.get("stockcode") or ""
                ),
                name=data.get("Name")
                or data.get("name")
                or data.get("DisplayName")
                or "",
                current_price=float(data.get("Price") or data.get("price") or 0),
                brand=data.get("Brand") or data.get("brand"),
                category=data.get("Category") or data.get("category"),
                unit_size=data.get("PackageSize") or data.get("packageSize"),
                unit_price=float(
                    data.get("CupPrice") or data.get("unitPrice") or 0
                )
                or None,
                unit_price_measure=data.get("CupMeasure")
                or data.get("cupMeasure"),
                image_url=data.get("MediumImageFile") or data.get("imageUrl"),
                product_url=(
                    f"{WOOLWORTHS_BASE}/shop/productdetails/"
                    f"{data.get('Stockcode') or data.get('stockcode')}/"
                    f"{data.get('UrlFriendlyName')}"
                    if (data.get("Stockcode") or data.get("stockcode")) and data.get("UrlFriendlyName")
                    else None
                ),
                is_available=(
                    data.get("IsAvailable", True)
                    and not data.get("IsInStoreOnly", False)
                    and data.get("IsPurchasable", True) is not False
                ),
            )
        except Exception:
            return None


# The singleton instance lives in scrapers.registry to avoid circular imports.
# Legacy code that imports `woolworths_scraper` from this module will break; update
# those call-sites to use `from ..scrapers.registry import woolworths_scraper`.
