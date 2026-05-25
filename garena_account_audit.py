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
    """Manages proxy rotation with round-robin and intelligent health checks.
    
    Features:
    - Round-robin proxy selection from pool
    - Marks proxies as dead after 3 consecutive failures
    - Auto-recovery: resets dead proxies if all fail
    - Thread-safe with async locks
    - Tracks success/failure counts per proxy
    """
    
    def __init__(self, proxy_list: List[str]):
        self.proxies = proxy_list
        self.current_index = 0
        self.dead_proxies = set()
        self.lock = asyncio.Lock()
        self.failed_count = {proxy: 0 for proxy in proxy_list}
        self.success_count = {proxy: 0 for proxy in proxy_list}
    
    async def get_next_proxy(self) -> Optional[str]:
        """Get next healthy proxy in round-robin fashion.
        
        Returns:
            Next proxy from healthy pool or None if all dead.
        """
        if not self.proxies:
            return None
        
        async with self.lock:
            # Filter out dead proxies
            healthy = [p for p in self.proxies if p not in self.dead_proxies]
            
            if not healthy:
                # All proxies are dead, reset and try again
                print("[PROXY] All proxies dead, resetting recovery mode...")
                self.dead_proxies.clear()
                healthy = self.proxies
            
            if not healthy:
                return None
            
            # Round-robin selection to distribute load
            proxy = healthy[self.current_index % len(healthy)]
            self.current_index += 1
            return proxy
    
    async def mark_success(self, proxy: str) -> None:
        """Mark a proxy as successfully used - resets failure count."""
        async with self.lock:
            self.success_count[proxy] = self.success_count.get(proxy, 0) + 1
            # Clear failures on success
            self.failed_count[proxy] = 0
            # Remove from dead list if previously marked
            self.dead_proxies.discard(proxy)
    
    async def mark_dead(self, proxy: str) -> None:
        """Mark a proxy as dead after 3 failures (timeout/connection error)."""
        async with self.lock:
            self.failed_count[proxy] = self.failed_count.get(proxy, 0) + 1
            failures = self.failed_count[proxy]
            
            # Mark as dead after 3 consecutive failures
            if failures >= 3:
                self.dead_proxies.add(proxy)
                print(f"[PROXY] Marked as dead (3 failures): {proxy}")
            else:
                print(f"[PROXY] Failure {failures}/3: {proxy} (will retry)")
    
    async def get_stats(self) -> dict:
        """Get comprehensive proxy statistics."""
        async with self.lock:
            return {
                "total": len(self.proxies),
                "dead": len(self.dead_proxies),
                "healthy": len(self.proxies) - len(self.dead_proxies),
                "success_count": dict(self.success_count),
                "failed_count": dict(self.failed_count),
            }


async def read_proxy_list(path: str) -> List[str]:
    """Read proxy list from file (one proxy per line).
    
    Supports formats:
    - http://host:port
    - socks5://host:port
    - Lines starting with # are comments
    """
    proxies = []
    try:
        async with aiofiles.open(path, "r", encoding="utf-8", errors="ignore") as f:
            async for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    # Validate proxy format
                    if "://" in line:
                        proxies.append(line)
                    else:
                        # Assume http if no scheme specified
                        proxies.append(f"http://{line}")
        print(f"[PROXY] Loaded {len(proxies)} proxies from {path}")
    except FileNotFoundError:
        print(f"[WARN] Proxy file not found: {path}. Running without proxies.")
    return proxies


class RateLimiter:
    """Adaptive rate limiter with dynamic delay adjustment.
    
    Features:
    - Fixed base delay between requests
    - Increase delay on rate limiting detection
    - Exponential backoff for retries
    """
    
    def __init__(self, delay: float):
        self.delay = delay
        self.lock = asyncio.Lock()
        self.last_run = 0.0
        self.dynamic_delay = delay

    async def wait(self) -> None:
        """Wait for configured delay since last run."""
        async with self.lock:
            elapsed = time.time() - self.last_run
            if elapsed < self.dynamic_delay:
                await asyncio.sleep(self.dynamic_delay - elapsed)
            self.last_run = time.time()
    
    async def increase_delay(self, factor: float = 1.5) -> None:
        """Increase delay (max 60s) when rate limiting is detected."""
        async with self.lock:
            old_delay = self.dynamic_delay
            self.dynamic_delay = min(self.dynamic_delay * factor, 60.0)
            print(f"[RATE] Increased delay {old_delay:.1f}s → {self.dynamic_delay:.1f}s")
    
    async def reset_delay(self) -> None:
        """Reset delay to original base value."""
        async with self.lock:
            self.dynamic_delay = self.delay


async def read_accounts(path: str) -> List[Tuple[str, str]]:
    """Read accounts from file (format: username:password, one per line)."""
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
    """Check if element matching selector is visible."""
    try:
        locator = page.locator(selector).first
        await locator.wait_for(timeout=timeout)
        return await locator.is_visible()
    except Exception:
        return False


def extract_last_4(masked_phone: str) -> str:
    """Extract last 4 digits from masked phone number.
    
    Example: "***1234" -> "1234"
    """
    digits = re.findall(r"\d", masked_phone or "")
    if len(digits) >= 4:
        return "".join(digits[-4:])
    return ""


def is_still_on_login_page(url: str) -> bool:
    """Check if current URL is Garena login page."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return hostname == "sso.garena.com" and "/login" in path


async def fetch_masked_phone(page) -> str:
    """Fetch masked phone number from account page.
    
    Tries multiple selectors for robustness against UI changes.
    """
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
    limiter: RateLimiter,
) -> AuditResult:
    """Audit a single account: login, extract masked phone, handle errors.
    
    Flow:
    1. Get next healthy proxy from rotator (if available)
    2. Create browser context with proxy
    3. Navigate to Garena login
    4. Detect and handle CAPTCHA/OTP (pause for manual)
    5. Fill credentials and login
    6. Navigate to account page
    7. Extract masked phone digits
    8. On error: mark proxy dead, retry with backoff
    """
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
            print(f"  [Attempt {attempt}] {f'Proxy: {proxy}' if proxy else 'No proxy'}")
            
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)

            if await is_visible(page, SELECTORS["captcha"]):
                result.status = "manual_required"
                result.error = "CAPTCHA detected. Please complete verification manually."
                await context.close()
                if proxy_rotator and proxy:
                    await proxy_rotator.mark_dead(proxy)
                return result

            await page.fill(SELECTORS["username"], username)
            await page.fill(SELECTORS["password"], password)
            await page.click(SELECTORS["login_button"])
            await page.wait_for_load_state("networkidle", timeout=timeout)

            if await is_visible(page, SELECTORS["otp"]):
                result.status = "manual_required"
                result.error = "OTP verification required."
                await context.close()
                if proxy_rotator and proxy:
                    await proxy_rotator.mark_success(proxy)
                return result

            if await is_visible(page, SELECTORS["captcha"]):
                result.status = "manual_required"
                result.error = "CAPTCHA detected after login."
                await context.close()
                if proxy_rotator and proxy:
                    await proxy_rotator.mark_dead(proxy)
                return result

            if is_still_on_login_page(page.url):
                result.status = "failed"
                result.error = "Invalid credentials or login blocked."
                await context.close()
                if proxy_rotator and proxy:
                    await proxy_rotator.mark_dead(proxy)
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
            if proxy_rotator and proxy:
                await proxy_rotator.mark_success(proxy)
            return result

        except PlaywrightTimeoutError as e:
            result.error = f"Timeout on attempt {attempt}."
            if proxy_rotator and proxy:
                await proxy_rotator.mark_dead(proxy)
            # Increase rate limiter on timeout (rate limiting detected)
            await limiter.increase_delay()
        except Exception as e:
            error_msg = str(e).lower()
            
            # Detect different error types for intelligent handling
            if "proxy" in error_msg or "connection refused" in error_msg or "econnrefused" in error_msg:
                result.error = f"Proxy error: {type(e).__name__}"
                if proxy_rotator and proxy:
                    await proxy_rotator.mark_dead(proxy)
            elif "429" in error_msg or "rate" in error_msg:
                result.error = f"Rate limited: {type(e).__name__}"
                await limiter.increase_delay()
            else:
                result.error = f"{type(e).__name__}: {str(e)[:100]}"
        finally:
            await context.close()

        # Exponential backoff between retries: 2^attempt seconds (capped at 30s)
        backoff = min(2 ** attempt, 30)
        print(f"  [Backoff] Waiting {backoff}s before retry...")
        await asyncio.sleep(backoff)

    return result


async def save_outputs(results: List[AuditResult], output_prefix: str, proxy_stats: Optional[dict] = None) -> None:
    """Save results to JSON and TXT files with optional proxy stats."""
    json_path = Path(f"{output_prefix}.json")
    txt_path = Path(f"{output_prefix}.txt")

    async with aiofiles.open(json_path, "w", encoding="utf-8") as f:
        output_data = {
            "results": [asdict(r) for r in results],
            "proxy_stats": proxy_stats,
            "summary": {
                "total": len(results),
                "success": len([r for r in results if r.status == "success"]),
                "failed": len([r for r in results if r.status == "failed"]),
                "manual_required": len([r for r in results if r.status == "manual_required"]),
            }
        }
        await f.write(json.dumps(output_data, ensure_ascii=False, indent=2))

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
    """Worker task to process accounts from queue.
    
    Each worker:
    - Waits for rate limiter
    - Audits account
    - Appends result to thread-safe list
    """
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
            limiter=limiter,
        )

        async with lock:
            results.append(result)

        proxy_info = f" (Proxy: {result.proxy_used})" if result.proxy_used else ""
        print(f"[{name}] {username}: {result.status} ({result.error or 'ok'}){proxy_info}")
        queue.task_done()


async def main() -> None:
    """Main entry point with argument parsing and orchestration."""
    parser = argparse.ArgumentParser(
        description=(
            "Garena account audit tool with intelligent proxy rotation. "
            "Extracts masked phone numbers from account pages. "
            "Non-bypass, account-owner only."
        )
    )
    parser.add_argument("-i", "--input", required=True, help="Input file with username:password lines")
    parser.add_argument("-o", "--output", default="garena_audit_result", help="Output file prefix")
    parser.add_argument("--proxy-list", default=None, help="Proxy list file (one proxy per line)")
    parser.add_argument("--single-proxy", default=None, help="Single proxy (overrides proxy-list)")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrent workers (recommended 1-2)")
    parser.add_argument("--delay", type=float, default=15.0, help="Base delay between accounts (seconds)")
    parser.add_argument("--retries", type=int, default=2, help="Retry attempts per account")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--timeout", type=int, default=45000, help="Request timeout (milliseconds)")
    args = parser.parse_args()

    if args.concurrency > 2:
        print("[WARN] Limiting concurrency to 2 to reduce lock/rate-limit risk.")
        args.concurrency = 2

    accounts = await read_accounts(args.input)
    if not accounts:
        print("[ERR] No valid accounts found in input file.")
        return

    print(f"[OK] Loaded {len(accounts)} accounts")

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

    print(f"[START] Processing {len(accounts)} accounts with {args.concurrency} workers")
    print(f"[CONFIG] Delay: {args.delay}s | Retries: {args.retries} | Timeout: {args.timeout}ms")

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

    # Get final proxy stats
    proxy_stats = None
    if proxy_rotator:
        proxy_stats = await proxy_rotator.get_stats()
        print(f"\n[PROXY STATS]")
        print(f"  Total: {proxy_stats['total']} | Healthy: {proxy_stats['healthy']} | Dead: {proxy_stats['dead']}")

    await save_outputs(results, args.output, proxy_stats)
    
    # Print summary
    success = len([r for r in results if r.status == "success"])
    failed = len([r for r in results if r.status == "failed"])
    manual = len([r for r in results if r.status == "manual_required"])
    print(f"\n[SUMMARY] Success: {success} | Failed: {failed} | Manual Required: {manual}")


if __name__ == "__main__":
    asyncio.run(main())
