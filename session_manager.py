import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import aiofiles
from playwright.async_api import BrowserContext


class SessionManager:
    """Persist and restore cookies between runs."""

    def __init__(self, base_dir: str = ".sessions"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _session_file(self, username: str) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", username)
        return self.base_dir / f"{safe_name}.cookies.json"

    async def save_cookies_to_file(self, cookies: Dict[str, str], username: str) -> None:
        if not cookies:
            return
        file_path = self._session_file(username)
        payload = {
            "username": username,
            "saved_at": datetime.utcnow().isoformat() + "Z",
            "cookies": [{"name": key, "value": value, "domain": ".garena.com", "path": "/"} for key, value in cookies.items()],
        }
        async with aiofiles.open(file_path, "w", encoding="utf-8") as handle:
            await handle.write(json.dumps(payload, ensure_ascii=False, indent=2))

    async def load_cookies_from_file(self, username: str) -> List[Dict[str, str]]:
        file_path = self._session_file(username)
        if not file_path.exists():
            return []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as handle:
                raw = await handle.read()
            data = json.loads(raw)
            cookies = data.get("cookies", [])
            if isinstance(cookies, list):
                return cookies
        except Exception:
            return []
        return []

    async def apply_cookies(self, context: BrowserContext, username: str) -> None:
        cookies = await self.load_cookies_from_file(username)
        if cookies:
            try:
                await context.add_cookies(cookies)
            except Exception:
                pass

    def validate_session_token(self, cookies: Dict[str, str]) -> bool:
        if not cookies:
            return False
        return any(name.lower() in {"session", "sessionid", "token", "access_token"} for name in cookies)
