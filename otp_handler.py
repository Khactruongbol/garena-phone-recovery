import os
import re
from typing import Optional


class OTPHandler:
    """OTP extraction helper from trusted local sources."""

    def __init__(self, otp_email: str = ""):
        self.otp_email = otp_email or ""

    def validate_otp_format(self, otp: str) -> bool:
        return bool(re.fullmatch(r"\d{4,8}", otp or ""))

    def _extract_from_text(self, text: str) -> str:
        if not text:
            return ""
        matches = re.findall(r"(?<!\d)(\d{4,8})(?!\d)", text)
        for candidate in matches:
            if self.validate_otp_format(candidate):
                return candidate
        return ""

    async def extract_from_email_api(self, email: Optional[str] = None) -> str:
        _ = email or self.otp_email
        direct = os.getenv("GARENA_OTP_CODE", "").strip()
        if self.validate_otp_format(direct):
            return direct

        email_text = os.getenv("GARENA_OTP_EMAIL_TEXT", "")
        return self._extract_from_text(email_text)

    async def extract_from_sms_gateway(self) -> str:
        direct = os.getenv("GARENA_SMS_OTP", "").strip()
        if self.validate_otp_format(direct):
            return direct

        sms_text = os.getenv("GARENA_SMS_OTP_TEXT", "")
        return self._extract_from_text(sms_text)
