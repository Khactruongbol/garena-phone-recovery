# Garena Phone Recovery Tool

**Công cụ tự động khôi phục số điện thoại Garena thông qua 3 phases hoàn chỉnh.**

## 🎯 **3 Phases Hoạt Động**

### **Phase 1: Garena Login & Extract Last 4 Digits**
- Đăng nhập vào Garena SSO
- Xử lý CAPTCHA/OTP (manual)
- Trích xuất 4 chữ số cuối của số điện thoại
- Output: `****XXXX`

### **Phase 2: napthe.vn API & Extract First 3 Digits**
- Đăng nhập vào napthe.vn
- Gọi API `/api/auth/get_user_info/multi`
- Trích xuất 3 chữ số đầu
- Output: `XXX*****`

### **Phase 3: Recovery Brute-Force & Complete Phone**
- Brute-force 3 chữ số giữa (000-999)
- Test 1000 combinations: `XXX000XXXX → XXX999XXXX`
- Detect success/rate limit/lock
- Output: `XXXXXXXXXX` (10 chữ số)

## 📦 **Cài Đặt**

### **1. Clone Repository**
```bash
git clone https://github.com/Khactruongbol/garena-phone-recovery.git
cd garena-phone-recovery
```

### **2. Tạo Virtual Environment**
```bash
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# hoặc
venv\Scripts\activate  # Windows
```

### **3. Cài Đặt Dependencies**
```bash
pip install -r requirements.txt
```

**Hoặc cài thủ công:**
```bash
pip install playwright aiofiles
playwright install chromium
```

## 📝 **Chuẩn Bị Input Files**

### **1. File Accounts (`accounts.txt`)**
```
username1:password1
username2:password2
username3:password3
```

### **2. File Proxies (`proxies.txt`)**
```
160.250.166.25:10298
160.250.166.16:10723
160.250.166.28:10575
160.250.166.29:10326
160.250.166.36:10247
```

## 🚀 **Cách Chạy**

### **Basic - Không Proxy**
```bash
python garena_phone_recovery_v2.py \
  -i accounts.txt \
  -o results \
  --napthe-user your_napthe_username \
  --napthe-pass your_napthe_password \
  --delay 25 \
  --concurrency 1
```

### **Với Rotating Proxy**
```bash
python garena_phone_recovery_v2.py \
  -i accounts.txt \
  -o results \
  --napthe-user your_napthe_username \
  --napthe-pass your_napthe_password \
  --proxy-list proxies.txt \
  --delay 25 \
  --phase-delay 5 \
  --phase3-delay 3 \
  --concurrency 1
```

### **Full Configuration**
```bash
python garena_phone_recovery_v2.py \
  -i accounts.txt \
  -o garena_recovery_result \
  --napthe-user napthe_user \
  --napthe-pass napthe_pass \
  --proxy-list proxies.txt \
  --concurrency 1 \
  --delay 25 \
  --phase-delay 5 \
  --phase3-delay 3 \
  --timeout 45000
```

## 📊 **Arguments**

```
-i, --input                Input file (username:password)
-o, --output               Output prefix (default: garena_recovery_result)
--napthe-user              napthe.vn username (REQUIRED)
--napthe-pass              napthe.vn password (REQUIRED)
--proxy-list               Rotating proxy list file (optional)
--concurrency              Số workers (1-2 recommended, default: 1)
--delay                    Delay giữa accounts (seconds, default: 25)
--phase-delay              Delay giữa phases (seconds, default: 5)
--phase3-delay             Delay giữa recovery attempts (seconds, default: 3)
--timeout                  Request timeout (ms, default: 45000)
```

## 📤 **Output Files**

### **garena_recovery_result.json**
```json
{
  "results": [
    {
      "username": "user1",
      "status": "success",
      "timestamp": "2025-05-25T10:30:00Z",
      "proxy_used": "http://160.250.166.25:10298",
      "phase1": {
        "login_status": "success",
        "last_4_digits": "1234",
        "masked_phone": "***1234"
      },
      "phase2": {
        "api_status": "success",
        "first_3_digits": "098"
      },
      "phase3": {
        "recovery_status": "success",
        "complete_phone": "0981231234",
        "attempts": 123
      }
    }
  ],
  "summary": {
    "total": 1,
    "success": 1,
    "failed": 0,
    "manual_required": 0
  }
}
```

### **garena_recovery_result.txt (Pipe-delimited)**
```
username|status|last_4|first_3|complete_phone|attempts|error
user1|success|1234|098|0981231234|123|
user2|failed|5678|123||0|API error: No phone data
```

## ⚙️ **Configuration Recommendations**

### **An toàn nhất (Single Account)**
```bash
python garena_phone_recovery_v2.py \
  -i accounts.txt \
  -o results \
  --napthe-user napthe_user \
  --napthe-pass napthe_pass \
  --delay 30 \
  --phase-delay 10 \
  --phase3-delay 5 \
  --concurrency 1
```

### **Với Proxy Rotation**
```bash
python garena_phone_recovery_v2.py \
  -i accounts.txt \
  -o results \
  --napthe-user napthe_user \
  --napthe-pass napthe_pass \
  --proxy-list proxies.txt \
  --delay 25 \
  --phase-delay 5 \
  --phase3-delay 3 \
  --concurrency 1
```

## 🔐 **Safety Guidelines**

✅ **Chỉ sử dụng với tài khoản của bạn**
✅ **Tôn trọng rate limits - thêm delays hợp lý**
✅ **Không sử dụng cho unauthorized access**
✅ **Theo dõi account lockouts**
✅ **Implement cooldown giữa các batches**
✅ **Log tất cả attempts để audit**

## ❌ **Lỗi Thường Gặp**

### **"No accounts found"**
- Kiểm tra file `accounts.txt` tồn tại
- Format: `username:password` (mỗi dòng một account)

### **"CAPTCHA detected"**
- Tool sẽ tạm dừng
- Solve CAPTCHA trong browser
- Nhấn ENTER để tiếp tục

### **"OTP required"**
- Nhập OTP vào browser khi được prompt
- Nhấn ENTER để tiếp tục

### **"Rate limited"**
- Tool sẽ tự động tăng delays
- Hoặc dừng nếu bị block quá lâu

### **Proxy errors**
- Kiểm tra proxy format: `host:port` (không có `http://`)
- Proxy có thể đã die
- Tool sẽ auto mark dead proxy sau 3 lần fail

## 📋 **Workflow Example**

```
$ python garena_phone_recovery_v2.py -i accounts.txt -o results --napthe-user user --napthe-pass pass --proxy-list proxies.txt --concurrency 1

[OK] Loaded 5 accounts
[PROXY] Loaded 10 rotating proxies from proxies.txt
[PROXY] Ready to rotate 10 proxies (host:port format)
[START] 5 accounts | 1 workers

[W1] ═══════════════════════════════════
[W1] Account: user1
[W1] ═══════════════════════════════════
  [Phase 1] Garena login...
  [Phase 1] ✓ Last 4 digits: 1234
  [Phase 2] napthe.vn login...
  [Phase 2] ✓ First 3 digits: 098
  [Phase 3] Brute-force recovery (1000 attempts)...
  [Phase 3] Pattern: 098XXX1234
  [Phase 3] ✓ Found: 0981231234 (attempt 123)
[W1] ✓ Result: success | Phone: 0981231234

[FINAL] ✓ 1 | ✗ 0 | ⚠ 0
[OK] Saved: results.json
[OK] Saved: results.txt
```

## 🔗 **Links**

- 📁 Repository: https://github.com/Khactruongbol/garena-phone-recovery
- 📝 Main Script: `garena_phone_recovery_v2.py`
- 🔐 Proxy List: `proxies.txt`
- 📋 Accounts: `accounts.txt` (local only)

---

**Tạo bởi:** Nguyễn Khắc Trường
**Phiên bản:** v2.0
**Cập nhật:** 2025-05-25
