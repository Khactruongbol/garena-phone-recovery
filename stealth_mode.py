import asyncio
import random
from typing import Dict

from playwright.async_api import Page


STEALTH_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


class StealthMode:
    """Stealth-focused browser tuning helpers."""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    def randomize_user_agent(self) -> str:
        return random.choice(STEALTH_USER_AGENTS)

    async def inject_stealth_script(self, page: Page) -> None:
        if not self.enabled:
            return
        script = """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
            Object.defineProperty(navigator, 'language', { get: () => 'vi-VN' });
            Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN', 'en-US', 'en'] });
        """
        try:
            await page.add_init_script(script)
        except Exception:
            pass

    async def add_realistic_delays(self, min_seconds: float = 0.2, max_seconds: float = 0.7) -> None:
        if not self.enabled:
            return
        await asyncio.sleep(random.uniform(min_seconds, max_seconds))

    def default_headers(self, referer: str = "") -> Dict[str, str]:
        headers = {
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
        }
        if referer:
            headers["Referer"] = referer
        return headers