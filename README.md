# garena-phone-recovery

A **legitimate account-owner audit** utility that logs into Garena and extracts the masked phone display plus the visible last 4 digits from the account page.

## Safety scope

This repository intentionally **does not** include brute-force recovery or bypass behavior.

## Requirements

- Python 3.10+
- `playwright`
- `aiofiles`
- Install browser:
  - `playwright install chromium`

## Usage

```bash
python garena_account_audit.py \
  -i accounts.txt \
  -o garena_audit_result \
  --concurrency 1 \
  --delay 15 \
  --retries 2 \
  --headless
```

`accounts.txt` format:

```text
username1:password1
username2:password2
```

## Output

- `garena_audit_result.json`
- `garena_audit_result.txt`

JSON example fields:

- `username`
- `status` (`success`, `failed`, `manual_required`)
- `last_4_digits`
- `masked_phone`
- `error`
- `timestamp`
