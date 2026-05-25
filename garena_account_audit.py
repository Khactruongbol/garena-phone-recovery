import argparse
import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import aiofiles
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

LOGIN_URL = "https://sso.garena.com/universal/login?app_id=10100&redirect_uri=https%3A%2F%2Faccount.garena.com%2F&locale=vi-VN"
ACCOUNT_URL = "https://account.garena.com/"

SELECTORS = {
    "username": "input[name='username'], input[type='text']",
    "password": "input[name='password'], input[type='password']",
    "login_button": "button[type='submit'], button:has-text('Đăng nhập')",
    "captcha": "iframe[src*='captcha'], text=CAPTCHA, text=captcha",
    "otp": "text=OTP, text=mã xác minh, text=xác minh",
    "phone_section": "text=Số điện thoại, text=Phone",
    "masked_phone": "text=/\\*+\\d{2,4}|\\d{2,3}\\*+\\d{2,4}/",
}


@dataclass
class AuditResult:
    username: str
    status: str
    last_4_digits: str = ""
    masked_phone: str = ""
    error: str = ""
    timestamp: str = ""


class RateLimiter:
    def __init__(self, delay: float):
        self.delay = delay
        self.lock = asyncio.Lock()
        self.last_run = 0.0

    async def wait(self) -> None:
        async with self.lock:
            elapsed = time.time() - self.last_run
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self.last_run = time.time()


async def read_accounts(path: str) -> List[Tuple[str, str]]:
    accounts: List[Tuple[str, str]] = []
    async with aiofiles.open(path, "r", encoding="utf-8", errors="ignore") as f:
        async for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            username, password = line.split(":", 1)
            username = username.strip()
            password = password.strip()
            if username and password:
                accounts.append((username, password))
    return accounts


async def is_visible(page, selector: str, timeout: int = 1500) -> bool:
    try:
        locator = page.locator(selector).first
        await locator.wait_for(timeout=timeout)
        return await locator.is_visible()
    except Exception:
        return False


def extract_last_4(masked_phone: str) -> str:
    digits = re.findall(r"\d", masked_phone or "")
    if len(digits) >= 4:
        return "".join(digits[-4:])
    return ""


async def fetch_masked_phone(page) -> str:
    candidates = [
        SELECTORS["masked_phone"],
        "text=/\\*+\\d{4}/",
        "text=/\\d{3}\\*+\\d{4}/",
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            await loc.wait_for(timeout=2000)
            text = (await loc.inner_text()).strip()
            if text:
                return " ".join(text.split())
        except Exception:
            continue
    return ""


async def audit_account(
    browser,
    username: str,
    password: str,
    retries: int,
    timeout: int,
    proxy: Optional[str],
) -> AuditResult:
    result = AuditResult(username=username, status="failed", timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    for attempt in range(1, retries + 1):
        context_kwargs = {"locale": "vi-VN", "viewport": {"width": 1280, "height": 800}}
        if proxy:
            context_kwargs["proxy"] = {"server": proxy}

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)

            if await is_visible(page, SELECTORS["captcha"]):
                result.status = "manual_required"
                result.error = "CAPTCHA detected. Please complete verification manually."
                return result

            await page.fill(SELECTORS["username"], username)
            await page.fill(SELECTORS["password"], password)
            await page.click(SELECTORS["login_button"])
            await page.wait_for_load_state("networkidle", timeout=timeout)

            if await is_visible(page, SELECTORS["otp"]):
                result.status = "manual_required"
                result.error = "OTP verification required."
                return result

            if await is_visible(page, SELECTORS["captcha"]):
                result.status = "manual_required"
                result.error = "CAPTCHA detected after login."
                return result

            if "sso.garena.com" in page.url.lower() and "login" in page.url.lower():
                result.status = "failed"
                result.error = "Invalid credentials or login blocked."
                return result

            await page.goto(ACCOUNT_URL, wait_until="networkidle", timeout=timeout)
            masked_phone = await fetch_masked_phone(page)
            result.masked_phone = masked_phone
            result.last_4_digits = extract_last_4(masked_phone)

            if result.last_4_digits:
                result.status = "success"
            else:
                result.status = "failed"
                result.error = "Could not extract masked phone digits from account page."
            return result

        except PlaywrightTimeoutError:
            result.error = f"Timeout on attempt {attempt}."
        except Exception as e:
            result.error = f"{type(e).__name__}: {e}"
        finally:
            await context.close()

        await asyncio.sleep(2)

    return result


async def save_outputs(results: List[AuditResult], output_prefix: str) -> None:
    json_path = Path(f"{output_prefix}.json")
    txt_path = Path(f"{output_prefix}.txt")

    async with aiofiles.open(json_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))

    async with aiofiles.open(txt_path, "w", encoding="utf-8") as f:
        for r in results:
            await f.write(
                f"{r.username}|{r.status}|{r.last_4_digits}|{r.masked_phone}|{r.error}\n"
            )

    print(f"[OK] Saved: {json_path}")
    print(f"[OK] Saved: {txt_path}")


async def worker(name: str, queue: asyncio.Queue, browser, args, limiter: RateLimiter, results: List[AuditResult], lock: asyncio.Lock) -> None:
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return

        username, password = item
        await limiter.wait()
        print(f"[{name}] Checking: {username}")

        result = await audit_account(
            browser=browser,
            username=username,
            password=password,
            retries=args.retries,
            timeout=args.timeout,
            proxy=args.proxy,
        )

        async with lock:
            results.append(result)

        print(f"[{name}] {username}: {result.status} ({result.error or 'ok'})")
        queue.task_done()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Garena account masked-phone audit tool for account owners. "
            "This tool does not perform brute-force recovery."
        )
    )
    parser.add_argument("-i", "--input", required=True, help="Input file with username:password lines")
    parser.add_argument("-o", "--output", default="garena_audit_result", help="Output file prefix")
    parser.add_argument("--concurrency", type=int, default=1, help="Workers (recommended 1-2)")
    parser.add_argument("--delay", type=float, default=15.0, help="Delay between account attempts (seconds)")
    parser.add_argument("--retries", type=int, default=2, help="Retry attempts per account")
    parser.add_argument("--proxy", default=None, help="Optional proxy, e.g. http://host:port")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--timeout", type=int, default=45000, help="Request timeout in milliseconds")
    args = parser.parse_args()

    if args.concurrency > 2:
        print("[WARN] Limiting concurrency to 2 to reduce lock/rate-limit risk.")
        args.concurrency = 2

    accounts = await read_accounts(args.input)
    if not accounts:
        print("[ERR] No valid accounts found in input file.")
        return

    queue: asyncio.Queue = asyncio.Queue()
    for item in accounts:
        await queue.put(item)

    results: List[AuditResult] = []
    lock = asyncio.Lock()
    limiter = RateLimiter(args.delay)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless)
        tasks = [
            asyncio.create_task(worker(f"W{i + 1}", queue, browser, args, limiter, results, lock))
            for i in range(args.concurrency)
        ]

        for _ in tasks:
            await queue.put(None)

        await queue.join()

        for task in tasks:
            await task

        await browser.close()

    await save_outputs(results, args.output)


if __name__ == "__main__":
    asyncio.run(main())
