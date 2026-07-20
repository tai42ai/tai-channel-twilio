"""Twilio channel settings.

A ``TaiBaseSettings`` subclass reading the ``CHANNEL_TWILIO_`` env group, exposed
through accessors cached by ``tai_kit.settings.settings_cache`` (so a live-reload
soft restart drops the singletons with every other settings group).

Recipient policy is operator-owned deployment configuration:
``CHANNEL_TWILIO_ALLOWED_RECIPIENTS`` is the whitelist a caller-requested
destination must be on, and ``CHANNEL_TWILIO_DEFAULT_RECIPIENT`` is the
destination used when the caller requests none. The auth token is a
``SecretStr`` so it never surfaces in a repr, log line, traceback, or
model_dump; the plaintext is read only at the two seams that need it
(HTTP Basic auth, signature HMAC key).
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict
from tai_contract.channels import ChannelDeliveryError
from tai_kit.clients import RedisConnectionSettings
from tai_kit.settings import TaiBaseSettings, settings_cache


class TwilioSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHANNEL_TWILIO_")

    account_sid: str | None = None
    auth_token: SecretStr | None = None
    # "From" is Twilio's wire name; the env var carries it verbatim via a
    # full-name alias (an alias bypasses the env_prefix). For WhatsApp the
    # value carries the "whatsapp:" prefix (e.g. "whatsapp:+15551234567") and
    # flows through unchanged — send, correlation keys, and inbound matching
    # all use the prefixed strings.
    from_number: str | None = Field(default=None, validation_alias="CHANNEL_TWILIO_FROM")
    # Operator whitelist of destination "To" numbers (E.164, or "whatsapp:"-
    # prefixed) a caller-requested recipient must be on; an unlisted request is
    # refused. NoDecode hands the raw env string to the validator below, which
    # accepts a comma-separated string or a JSON list.
    allowed_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Destination "To" number used when the caller requests no recipient.
    # Operator-set, so it is trusted without an allowlist check.
    default_recipient: str | None = None
    # Twilio REST API origin. Overridable so a stub server can stand in for
    # Twilio (the e2e harness records against a local endpoint); production
    # never changes it. The Messages endpoint is addressed as
    # ``{api_base_url}/Accounts/{AccountSid}/Messages.json``.
    api_base_url: str = "https://api.twilio.com/2010-04-01"
    # Timeout for both the outbound Twilio send and the loopback answer forward.
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    # How long a handled MessageSid stays remembered. Twilio's signature scheme
    # carries no timestamp, so a captured request validates forever — this
    # dedupe window is the replay guard (48h default; a replay older than the
    # window finds no pending question and is dropped as uncorrelated anyway).
    dedupe_ttl: int = Field(default=172_800, gt=0)

    @field_validator("allowed_recipients", mode="before")
    @classmethod
    def _parse_allowed_recipients(cls, value: object) -> object:
        """A list passes through; a string parses as a JSON list when it starts
        with ``[``, else splits on commas; both string branches normalize the
        same way (items stripped, empties dropped); anything else is rejected
        loudly.
        """
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                # JSON text opening with "[" parses to a list or raises loudly;
                # a non-string item is rejected loudly rather than stripped.
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
    """The configured value an outbound send needs, raising
    ``ChannelDeliveryError`` when unset or empty.

    A config read on the ``deliver``/``notify`` path fails as a delivery
    failure: the channel contract promises ``ChannelDeliveryError`` for every
    reason a send cannot happen, operator misconfiguration included. Checked at
    use time, before any network work, so a missing variable surfaces as this
    message (naming only the env var) rather than a deep Twilio 401.
    """
    if not value:
        raise ChannelDeliveryError(f"Twilio channel is not configured: set {env_name}.")
    return value


def require_delivery_secret(value: SecretStr | None, env_name: str) -> str:
    """The plaintext secret an outbound send needs, raising
    ``ChannelDeliveryError`` when unset or empty.

    Same delivery-failure contract as ``require_delivery_setting``; the
    message names only the env var, never the secret value.
    """
    secret = value.get_secret_value() if value is not None else ""
    if not secret:
        raise ChannelDeliveryError(f"Twilio channel is not configured: set {env_name}.")
    return secret


def require_secret(value: SecretStr | None, env_name: str) -> str:
    """The plaintext secret the inbound webhook's signature check needs,
    raising ``ValueError`` when unset or empty (the webhook maps it to a
    logged 500).

    An empty auth token would still key the signature HMAC — a forgeable
    signature anyone can compute — so empty fails CLOSED with a loud error,
    never a soft "signature mismatch". The message names only the env var,
    never the secret value.
    """
    secret = value.get_secret_value() if value is not None else ""
    if not secret:
        raise ValueError(f"Twilio channel is not configured: set {env_name}.")
    return secret
