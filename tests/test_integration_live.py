"""Live integration tests against the real Twilio Messages API (outbound only).

Run with ``pytest -m integration``. Credentials and numbers are read purely from
the operator's ``CHANNEL_TWILIO_ACCOUNT_SID`` / ``_AUTH_TOKEN`` / ``_FROM`` /
``_DEFAULT_RECIPIENT`` environment; the suite skips cleanly when any of them is
unset (CI has none). With Twilio's magic TEST credentials,
``CHANNEL_TWILIO_FROM=+15005550006`` is the always-valid test sender and no SMS
is actually delivered.

Only the outbound send is live: the correlation store stays the in-memory fake
(the inbound webhook cannot be driven without a public URL), so ``deliver()``
here proves the real Twilio accept end-to-end while writing no external state.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from tai_kit.clients.impl.http import HttpxClient
from tai_kit.clients.impl.redis import RedisClient
from tai_kit.settings import reset_all_settings

from tai_channel_twilio import TwilioChannel
from tai_channel_twilio.client import send_message
from tai_channel_twilio.settings import twilio_settings
from tests.conftest import FakeRedis, make_delivery

_ENV_KEYS = (
    "CHANNEL_TWILIO_ACCOUNT_SID",
    "CHANNEL_TWILIO_AUTH_TOKEN",
    "CHANNEL_TWILIO_FROM",
    "CHANNEL_TWILIO_DEFAULT_RECIPIENT",
)


def _missing_creds() -> bool:
    return not all(os.environ.get(key) for key in _ENV_KEYS)


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_missing_creds(), reason="CHANNEL_TWILIO_* not set"),
]


class _LiveHttpx:
    """One-shot real ``httpx.AsyncClient`` per call — the live stand-in for the pooled client."""

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
            return await client.post(url, **kwargs)


@pytest.fixture(autouse=True)
def live_clients(stub_app):
    # The operator's real env must win over any cached test settings, and the
    # send path must reach the real API (the correlation store stays in-memory).
    reset_all_settings()
    stub_app.clients.by_class[HttpxClient] = _LiveHttpx()
    stub_app.clients.by_class[RedisClient] = FakeRedis()
    yield
    reset_all_settings()


async def test_send_message_returns_a_message_sid():
    settings = twilio_settings()
    assert settings.default_recipient is not None
    assert settings.from_number is not None

    sid = await send_message(
        to=settings.default_recipient,
        from_number=settings.from_number,
        body="tai-channel-twilio live smoke: send_message",
    )

    assert sid.startswith(("SM", "MM"))


async def test_send_to_magic_invalid_number_raises_loudly():
    from tai_contract.channels import ChannelDeliveryError

    settings = twilio_settings()
    assert settings.from_number is not None

    # +15005550001 is Twilio's magic invalid number under test credentials; under
    # live credentials it is an invalid 'To' — either way the send is rejected
    # with a Twilio error code and surfaces as the loud failure path.
    with pytest.raises(ChannelDeliveryError, match="code="):
        await send_message(
            to="+15005550001",
            from_number=settings.from_number,
            body="tai-channel-twilio live smoke: invalid recipient",
        )


async def test_full_deliver_reaches_the_configured_human(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TWILIO_REDIS_URL", "redis://in-memory-fake/0")
    reset_all_settings()

    await TwilioChannel().deliver(
        make_delivery(question="tai-channel-twilio live smoke: full deliver — reply is not expected.")
    )
