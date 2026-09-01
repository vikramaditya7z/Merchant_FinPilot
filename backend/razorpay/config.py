"""Razorpay integration configuration.

PROJECT_RULES 10.9 / ARCHITECTURE.md §12.

Loads Razorpay credentials and options strictly from environment variables.
Never hardcodes credentials or exposes secrets to external consumers.
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RazorpayConfig:
    """Immutable configuration container for Razorpay API and Webhook integration."""

    key_id: Optional[str] = None
    key_secret: Optional[str] = None
    webhook_secret: Optional[str] = None
    api_base_url: str = "https://api.razorpay.com/v1"
    timeout_seconds: float = 10.0

    @property
    def is_configured(self) -> bool:
        """Return True if both API key ID and Secret are provided."""
        return bool(self.key_id and self.key_id.strip() and self.key_secret and self.key_secret.strip())

    @property
    def is_webhook_configured(self) -> bool:
        """Return True if Webhook Secret is configured."""
        return bool(self.webhook_secret and self.webhook_secret.strip())

    @classmethod
    def from_env(cls) -> "RazorpayConfig":
        """Load configuration from environment variables."""
        key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip() or None
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip() or None
        webhook_secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip() or None
        base_url = os.environ.get("RAZORPAY_API_BASE_URL", "https://api.razorpay.com/v1").strip()
        timeout_str = os.environ.get("RAZORPAY_TIMEOUT_SECONDS", "10.0").strip()

        try:
            timeout_seconds = float(timeout_str)
        except ValueError:
            timeout_seconds = 10.0

        return cls(
            key_id=key_id,
            key_secret=key_secret,
            webhook_secret=webhook_secret,
            api_base_url=base_url,
            timeout_seconds=timeout_seconds,
        )

    def __repr__(self) -> str:
        masked_secret = "***" if self.key_secret else None
        masked_wh = "***" if self.webhook_secret else None
        return (
            f"RazorpayConfig(key_id={self.key_id!r}, key_secret={masked_secret!r}, "
            f"webhook_secret={masked_wh!r}, api_base_url={self.api_base_url!r}, "
            f"timeout_seconds={self.timeout_seconds})"
        )

    def __str__(self) -> str:
        return self.__repr__()

