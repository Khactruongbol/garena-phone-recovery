import random
from typing import Any


class RetryManager:
    """Utility for retry and backoff behavior."""

    def __init__(self, max_retries: int = 3, base_delay: float = 2.0, max_delay: float = 120.0, jitter: float = 0.2):
        self.max_retries = max(0, max_retries)
        self.base_delay = max(0.1, base_delay)
        self.max_delay = max(1.0, max_delay)
        self.jitter = max(0.0, jitter)

    def exponential_backoff(self, attempt: int) -> float:
        raw_delay = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        if self.jitter == 0:
            return raw_delay
        jitter_value = random.uniform(0, raw_delay * self.jitter)
        return min(self.max_delay, raw_delay + jitter_value)

    def is_rate_limited(self, response: Any) -> bool:
        if isinstance(response, int):
            return response == 429
        text = str(response).lower()
        markers = ["429", "too many", "rate limit", "slow down", "retry later"]
        return any(marker in text for marker in markers)

    def is_temporary_error(self, error: str) -> bool:
        if not error:
            return False
        text = error.lower()
        markers = [
            "timeout",
            "tempor",
            "network",
            "connection",
            "dns",
            "429",
            "too many",
            "service unavailable",
            "gateway",
            "try again",
        ]
        return any(marker in text for marker in markers)