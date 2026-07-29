"""Twilio channel settings.

A ``TaiBaseSettings`` subclass reading the ``CHANNEL_TWILIO_`` env group through
accessors cached by ``tai42_kit.settings.settings_cache`` (dropped on a soft
restart). ``CHANNEL_TWILIO_ALLOWED_RECIPIENTS`` whitelists caller-requested
destinations; ``CHANNEL_TWILIO_DEFAULT_RECIPIENT`` is used when none is
requested. The auth token is a ``SecretStr`` (never in a repr/log/traceback);
the plaintext is read only at the HTTP Basic auth and signature-HMAC seams.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class TwilioSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHANNEL_TWILIO_")

    account_sid: str | None = None
    auth_token: SecretStr | None = None
    # "From" is Twilio's wire name; the full-name alias bypasses env_prefix. A
    # "whatsapp:"-prefixed value flows through unchanged everywhere.
    from_number: str | None = Field(default=None, validation_alias="CHANNEL_TWILIO_FROM")
    # Operator whitelist of "To" numbers a caller-requested recipient must be on.
    # NoDecode hands the raw env string to the validator (comma-separated or JSON).
    allowed_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # "To" number used when the caller requests none; trusted without an allowlist check.
    default_recipient: str | None = None
    # Twilio REST API origin. Overridable so a stub can stand in (e2e); production
    # never changes it.
    api_base_url: str = "https://api.twilio.com/2010-04-01"
    # Timeout for both the outbound Twilio send and the loopback answer forward.
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    # How long a handled MessageSid stays remembered (replay guard; 48h default).
    dedupe_ttl: int = Field(default=172_800, gt=0)

    @field_validator("allowed_recipients", mode="before")
    @classmethod
    def _parse_allowed_recipients(cls, value: object) -> object:
        """A list passes through; a string parses as JSON when it starts with
        ``[`` else splits on commas (items stripped, empties dropped); anything
        else is rejected loudly."""
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                items = json.loads(text)
                for item in items:
                    if not isinstance(item, str):
                        raise ValueError(f"allowed_recipients JSON list items must be strings, got {item!r}")
                return [item for item in (part.strip() for part in items) if item]
            return [item for item in (part.strip() for part in text.split(",")) if item]
        raise ValueError("allowed_recipients must be a comma-separated string or a list")


@settings_cache
def twilio_settings() -> TwilioSettings:
    return TwilioSettings()


class TwilioRedisSettings(RedisConnectionSettings):
    """Connection for the plugin-owned correlation store (``CHANNEL_TWILIO_REDIS_URL`` etc.)."""

    model_config = SettingsConfigDict(env_prefix="CHANNEL_TWILIO_")


@settings_cache
def twilio_redis_settings() -> TwilioRedisSettings:
    return TwilioRedisSettings()


def require_delivery_setting(value: str | None, env_name: str) -> str:
    """The configured value an outbound send needs; raises ``ChannelDeliveryError``
    (naming only the env var) when unset. Checked before any network work."""
    if not value:
        raise ChannelDeliveryError(f"Twilio channel is not configured: set {env_name}.")
    return value


def require_delivery_secret(value: SecretStr | None, env_name: str) -> str:
    """The plaintext secret an outbound send needs; raises ``ChannelDeliveryError``
    (naming only the env var, never the value) when unset."""
    secret = value.get_secret_value() if value is not None else ""
    if not secret:
        raise ChannelDeliveryError(f"Twilio channel is not configured: set {env_name}.")
    return secret


def require_secret(value: SecretStr | None, env_name: str) -> str:
    """The plaintext secret the inbound signature check needs; raises ``ValueError``
    (webhook maps it to a logged 500) when unset. An empty token would key a
    forgeable HMAC, so empty fails CLOSED, never a soft mismatch. Names only the
    env var.
    """
    secret = value.get_secret_value() if value is not None else ""
    if not secret:
        raise ValueError(f"Twilio channel is not configured: set {env_name}.")
    return secret
