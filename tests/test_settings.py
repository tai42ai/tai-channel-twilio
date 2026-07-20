"""Settings: env round-trips, secret hygiene, and the fail-closed require helpers."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError
from tai_contract.channels import ChannelDeliveryError
from tai_kit.settings import reset_all_settings

from tai_channel_twilio.settings import (
    TwilioRedisSettings,
    TwilioSettings,
    require_delivery_secret,
    require_delivery_setting,
    require_secret,
    twilio_redis_settings,
    twilio_settings,
)


def test_env_round_trip(twilio_env):
    settings = TwilioSettings()
    assert settings.account_sid == "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    assert settings.auth_token is not None
    assert settings.auth_token.get_secret_value() == "testtoken"
    # "From" arrives via the full-name alias, not the env_prefix.
    assert settings.from_number == "+15550000001"
    assert settings.default_recipient == "+15550000002"
    assert settings.allowed_recipients == ["+15550000003", "whatsapp:+15550000004"]


def test_secrets_never_surface_in_repr_or_dump(twilio_env):
    settings = TwilioSettings()
    for exposed in (repr(settings), str(settings.model_dump())):
        assert "testtoken" not in exposed


def test_no_env_constructs_cleanly_all_none(no_twilio_env):
    # A library import never demands configuration: every credential/target
    # field defaults None.
    settings = TwilioSettings()
    assert settings.account_sid is None
    assert settings.auth_token is None
    assert settings.from_number is None
    assert settings.allowed_recipients == []
    assert settings.default_recipient is None


def test_allowed_recipients_comma_string_splits_strips_and_drops_empties(no_twilio_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_TWILIO_ALLOWED_RECIPIENTS", " +15550000003 ,, whatsapp:+15550000004 , ")
    reset_all_settings()
    assert TwilioSettings().allowed_recipients == ["+15550000003", "whatsapp:+15550000004"]


def test_allowed_recipients_json_list_env(no_twilio_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_TWILIO_ALLOWED_RECIPIENTS", '["+15550000003", "whatsapp:+15550000004"]')
    reset_all_settings()
    assert TwilioSettings().allowed_recipients == ["+15550000003", "whatsapp:+15550000004"]


def test_allowed_recipients_json_list_strips_and_drops_empties(no_twilio_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_TWILIO_ALLOWED_RECIPIENTS", '[" +15550000003 ", "", " whatsapp:+15550000004 "]')
    reset_all_settings()
    assert TwilioSettings().allowed_recipients == ["+15550000003", "whatsapp:+15550000004"]


def test_allowed_recipients_malformed_json_raises(no_twilio_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_TWILIO_ALLOWED_RECIPIENTS", "[not json")
    reset_all_settings()
    with pytest.raises(ValidationError, match="allowed_recipients"):
        TwilioSettings()


def test_allowed_recipients_json_non_string_items_raise(no_twilio_env, monkeypatch):
    monkeypatch.setenv("CHANNEL_TWILIO_ALLOWED_RECIPIENTS", '[["nested"]]')
    reset_all_settings()
    with pytest.raises(ValidationError, match="allowed_recipients"):
        TwilioSettings()


def test_allowed_recipients_list_passes_through(no_twilio_env):
    settings = TwilioSettings(allowed_recipients=["+15550000003"])
    assert settings.allowed_recipients == ["+15550000003"]


def test_allowed_recipients_other_type_raises(no_twilio_env):
    with pytest.raises(ValidationError, match="comma-separated string or a list"):
        TwilioSettings(allowed_recipients=123)  # type: ignore[arg-type]


def test_require_delivery_setting_raises_delivery_error_naming_env_var():
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_DEFAULT_RECIPIENT"):
        require_delivery_setting(None, "CHANNEL_TWILIO_DEFAULT_RECIPIENT")


def test_require_delivery_setting_empty_raises_delivery_error():
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_FROM"):
        require_delivery_setting("", "CHANNEL_TWILIO_FROM")


def test_require_delivery_setting_passes_value_through():
    assert require_delivery_setting("+15550000002", "CHANNEL_TWILIO_DEFAULT_RECIPIENT") == "+15550000002"


def test_require_delivery_secret_unset_raises_delivery_error_naming_env_var():
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_AUTH_TOKEN"):
        require_delivery_secret(None, "CHANNEL_TWILIO_AUTH_TOKEN")


def test_require_delivery_secret_empty_raises_delivery_error():
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_AUTH_TOKEN"):
        require_delivery_secret(SecretStr(""), "CHANNEL_TWILIO_AUTH_TOKEN")


def test_require_delivery_secret_returns_plaintext():
    assert require_delivery_secret(SecretStr("tok"), "CHANNEL_TWILIO_AUTH_TOKEN") == "tok"


def test_require_secret_unset_raises_naming_env_var():
    # The inbound webhook path keeps ValueError — its handler maps it to a
    # logged 500, never a delivery failure.
    with pytest.raises(ValueError, match="set CHANNEL_TWILIO_AUTH_TOKEN"):
        require_secret(None, "CHANNEL_TWILIO_AUTH_TOKEN")


def test_require_secret_empty_fails_closed():
    with pytest.raises(ValueError, match="set CHANNEL_TWILIO_AUTH_TOKEN"):
        require_secret(SecretStr(""), "CHANNEL_TWILIO_AUTH_TOKEN")


def test_require_secret_returns_plaintext():
    assert require_secret(SecretStr("tok"), "CHANNEL_TWILIO_AUTH_TOKEN") == "tok"


def test_empty_env_secret_is_treated_as_missing(no_twilio_env, monkeypatch: pytest.MonkeyPatch):
    # env_ignore_empty: an empty CHANNEL_TWILIO_AUTH_TOKEN= leaves the field
    # None, so require_secret raises through the same fail-closed helper.
    monkeypatch.setenv("CHANNEL_TWILIO_AUTH_TOKEN", "")
    reset_all_settings()
    settings = TwilioSettings()
    assert settings.auth_token is None
    with pytest.raises(ValueError, match="CHANNEL_TWILIO_AUTH_TOKEN"):
        require_secret(settings.auth_token, "CHANNEL_TWILIO_AUTH_TOKEN")


def test_api_base_url_default_and_override(no_twilio_env, monkeypatch: pytest.MonkeyPatch):
    # Production default is Twilio's REST API origin; an operator (or the e2e
    # harness pointing at a stub) may override it.
    assert TwilioSettings().api_base_url == "https://api.twilio.com/2010-04-01"
    monkeypatch.setenv("CHANNEL_TWILIO_API_BASE_URL", "http://127.0.0.1:9098")
    reset_all_settings()
    assert TwilioSettings().api_base_url == "http://127.0.0.1:9098"


def test_http_timeout_default_and_override(no_twilio_env, monkeypatch: pytest.MonkeyPatch):
    assert TwilioSettings().http_timeout_seconds == 30
    monkeypatch.setenv("CHANNEL_TWILIO_HTTP_TIMEOUT_SECONDS", "7.5")
    reset_all_settings()
    assert TwilioSettings().http_timeout_seconds == 7.5


def test_http_timeout_rejects_non_positive(no_twilio_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TWILIO_HTTP_TIMEOUT_SECONDS", "0")
    reset_all_settings()
    with pytest.raises(ValidationError, match="http_timeout_seconds"):
        TwilioSettings()


def test_dedupe_ttl_rejects_non_positive(no_twilio_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TWILIO_DEDUPE_TTL", "0")
    reset_all_settings()
    with pytest.raises(ValidationError, match="dedupe_ttl"):
        TwilioSettings()


def test_redis_settings_read_url_and_client_kwargs(twilio_env):
    settings = TwilioRedisSettings()
    assert settings.redis_url == "redis://test/0"
    assert settings.client_kwargs()["url"] == "redis://test/0"
    # decode_responses default True: the correlation getters return str.
    assert settings.client_kwargs()["decode_responses"] is True


def test_cached_accessors_reset_with_settings_cache(twilio_env, monkeypatch: pytest.MonkeyPatch):
    assert twilio_settings() is twilio_settings()
    assert twilio_redis_settings() is twilio_redis_settings()
    before = twilio_settings()
    monkeypatch.setenv("CHANNEL_TWILIO_DEFAULT_RECIPIENT", "+15559999999")
    reset_all_settings()
    after = twilio_settings()
    assert after is not before
    assert after.default_recipient == "+15559999999"
