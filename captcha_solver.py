import asyncio
from dataclasses import dataclass
from typing import Dict, Optional

from playwright.async_api import Page


@dataclass
class CaptchaSolveResult:
    success: bool
    token: str = ""
    provider: str = ""
    error: str = ""
    manual_required: bool = True


class CaptchaSolver:
    """CAPTCHA helper with manual fallback and token cache."""

    def __init__(self, provider: str = "", api_key: str = "", manual_timeout_seconds: int = 180):
        self.provider = provider or ""
        self.api_key = api_key or ""
        self.manual_timeout_seconds = max(30, manual_timeout_seconds)
        self._token_cache: Dict[str, str] = {}

    def _cache_key(self, site_key: str, page_url: str) -> str:
        return f"{site_key}|{page_url}"

    def get_cached_token(self, site_key: str, page_url: str) -> str:
        return self._token_cache.get(self._cache_key(site_key, page_url), "")

    def cache_token(self, site_key: str, page_url: str, token: str) -> None:
        if token:
            self._token_cache[self._cache_key(site_key, page_url)] = token

    async def solve_recaptcha_v2(self, site_key: str, page_url: str) -> CaptchaSolveResult:
        cached = self.get_cached_token(site_key, page_url)
        if cached:
            return CaptchaSolveResult(success=True, token=cached, provider="cache", manual_required=False)
        return CaptchaSolveResult(success=False, provider=self.provider, error="Automated CAPTCHA bypass is not enabled in this build.")

    async def solve_hcaptcha(self, site_key: str, page_url: str) -> CaptchaSolveResult:
        return await self.solve_recaptcha_v2(site_key, page_url)

    async def solve_image_captcha(self, image_data: str) -> CaptchaSolveResult:
        _ = image_data
        return CaptchaSolveResult(success=False, provider=self.provider, error="Automated CAPTCHA bypass is not enabled in this build.")

    async def has_captcha(self, page: Page) -> bool:
        selectors = [
            "iframe[src*='captcha']",
            "iframe[src*='recaptcha']",
            "div.g-recaptcha",
            "div.h-captcha",
        ]
        for selector in selectors:
            try:
                if await page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    async def wait_for_manual_solve(self, page: Page) -> bool:
        print("[CAPTCHA] Solve CAPTCHA manually in browser, then press ENTER...")
        await asyncio.to_thread(input)

        deadline = asyncio.get_event_loop().time() + self.manual_timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            if not await self.has_captcha(page):
                return True
            await asyncio.sleep(2)
        return False