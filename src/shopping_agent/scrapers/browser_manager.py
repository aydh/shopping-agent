import json
import logging
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from ..config import settings
from ..models.product import Store

logger = logging.getLogger(__name__)

_LOGIN_URLS = {
    Store.COLES: "https://www.coles.com.au/customer/login",
    Store.WOOLWORTHS: "https://www.woolworths.com.au/shop/securelogin",
}

_PROFILE_CHECK_URLS = {
    Store.COLES: "https://www.coles.com.au",
    Store.WOOLWORTHS: "https://www.woolworths.com.au",
}


class BrowserManager:
    """Manages a shared Playwright browser with per-store contexts and cookie persistence."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._contexts: dict[Store, BrowserContext] = {}

    async def _ensure_browser(self) -> Browser:
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        return self._browser

    def _cookie_path(self, store: Store) -> Path:
        settings.ensure_dirs()
        return settings.cookies_dir / f"{store.value}.json"

    async def _load_cookies(self, store: Store, context: BrowserContext) -> None:
        path = self._cookie_path(store)
        if path.exists():
            try:
                cookies = json.loads(path.read_text())
                await context.add_cookies(cookies)
                logger.info("Loaded %d cookies for %s", len(cookies), store.value)
            except Exception:
                logger.warning("Failed to load cookies for %s", store.value, exc_info=True)

    async def _save_cookies(self, store: Store, context: BrowserContext) -> None:
        path = self._cookie_path(store)
        try:
            cookies = await context.cookies()
            path.write_text(json.dumps(cookies, indent=2))
            logger.info("Saved %d cookies for %s", len(cookies), store.value)
        except Exception:
            logger.warning("Failed to save cookies for %s", store.value, exc_info=True)

    async def get_context(self, store: Store) -> BrowserContext:
        if store not in self._contexts:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            await self._load_cookies(store, context)
            self._contexts[store] = context
        return self._contexts[store]

    async def get_page(self, store: Store):
        context = await self.get_context(store)
        page = await context.new_page()
        return page

    async def is_authenticated(self, store: Store) -> bool:
        """Check if we have saved cookies for a store."""
        path = self._cookie_path(store)
        if not path.exists():
            return False
        try:
            cookies = json.loads(path.read_text())
            return len(cookies) > 0
        except Exception:
            return False

    async def login_interactive(self, store: Store) -> bool:
        """Open a visible browser for user to log in manually."""
        if self._playwright is None:
            self._playwright = await async_playwright().start()

        # Launch a visible (headed) browser for login
        headed_browser = await self._playwright.chromium.launch(headless=False)
        context = await headed_browser.new_context(
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        login_url = _LOGIN_URLS[store]
        await page.goto(login_url)

        logger.info("Waiting for user to log in to %s...", store.value)

        # Wait for user to complete login by detecting navigation away from login page
        # or detection of a session cookie
        try:
            # Wait up to 5 minutes for user to complete login
            await page.wait_for_url(
                lambda url: "login" not in url.lower() and "signin" not in url.lower(),
                timeout=300_000,
            )
            # Give a moment for cookies to settle
            await page.wait_for_timeout(2000)
        except Exception:
            logger.warning("Login timed out or was cancelled for %s", store.value)
            await headed_browser.close()
            return False

        # Save cookies
        cookies = await context.cookies()
        self._cookie_path(store).write_text(json.dumps(cookies, indent=2))
        logger.info("Login successful for %s, saved %d cookies", store.value, len(cookies))

        await headed_browser.close()

        # Reset the headless context so it picks up new cookies
        if store in self._contexts:
            await self._contexts[store].close()
            del self._contexts[store]

        return True

    async def logout(self, store: Store) -> None:
        path = self._cookie_path(store)
        if path.exists():
            path.unlink()
        if store in self._contexts:
            await self._contexts[store].close()
            del self._contexts[store]

    async def save_all_cookies(self) -> None:
        for store, context in self._contexts.items():
            await self._save_cookies(store, context)

    async def close(self) -> None:
        await self.save_all_cookies()
        for context in self._contexts.values():
            await context.close()
        self._contexts.clear()
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None


# Singleton
browser_manager = BrowserManager()
