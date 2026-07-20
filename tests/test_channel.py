"""``TwilioChannel.deliver`` and ``TwilioChannel.notify`` — outbound form
payload, tier split, reservation ordering, and the correlation-free notify path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tai_contract.channels import ChannelDeliveryError, ChannelNotification

from tai_channel_twilio.channel import TwilioChannel
from tai_channel_twilio.correlation import PendingQuestionExistsError
from tests.conftest import FakeHttpx, FakeRedis, make_delivery, response

pytestmark = pytest.mark.usefixtures("twilio_env")

_ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
_MESSAGES_URL = f"https://api.twilio.com/2010-04-01/Accounts/{_ACCOUNT_SID}/Messages.json"


def _accepted() -> httpx.Response:
    return response(201, json={"sid": "SM123"})


async def test_deliver_sends_exact_outbound_form(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await TwilioChannel().deliver(make_delivery())

    assert len(fake_httpx.calls) == 1
    call = fake_httpx.calls[0]
    assert call["url"] == _MESSAGES_URL
    assert call["data"] == {"To": "+15550000002", "From": "+15550000001", "Body": "Deploy to prod?"}
    # HTTP Basic auth pair — httpx encodes it as Authorization: Basic base64(sid:token).
    assert call["auth"] == (_ACCOUNT_SID, "testtoken")


async def test_messages_url_derives_from_api_base_url(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    # An overridden CHANNEL_TWILIO_API_BASE_URL (a stub origin in e2e) is where
    # the Messages endpoint is addressed — the send URL derives from the setting.
    from tai_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_TWILIO_API_BASE_URL", "http://127.0.0.1:9098")
    reset_all_settings()
    fake_httpx.responses.append(_accepted())

    await TwilioChannel().deliver(make_delivery())

    assert fake_httpx.calls[0]["url"] == f"http://127.0.0.1:9098/Accounts/{_ACCOUNT_SID}/Messages.json"


async def test_select_rendering_numbers_options(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await TwilioChannel().deliver(
        make_delivery(answer_format="select", options=["staging", "production"], question="Which env?")
    )

    body = fake_httpx.calls[0]["data"]["Body"]
    assert body == "Which env?\n1. staging\n2. production\nReply with the text of one option."


async def test_reserve_precedes_send(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await TwilioChannel().deliver(make_delivery())

    kinds = [kind for kind, _ in fake_redis.events]
    assert kinds.index("redis_set") < kinds.index("http_post")


async def test_second_concurrent_question_rejected_loudly(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())
    await TwilioChannel().deliver(make_delivery())

    with pytest.raises(PendingQuestionExistsError) as excinfo:
        await TwilioChannel().deliver(make_delivery(interaction_id="int-2"))

    assert isinstance(excinfo.value, ChannelDeliveryError)
    assert len(fake_httpx.calls) == 1  # no second send happened


async def test_send_rejection_raises_and_releases_reservation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(400, json={"code": 21211, "message": "Invalid 'To' number"}))

    with pytest.raises(ChannelDeliveryError, match=r"HTTP 400.*21211"):
        await TwilioChannel().deliver(make_delivery())

    # The pair was released — a follow-up deliver succeeds.
    fake_httpx.responses.append(_accepted())
    await TwilioChannel().deliver(make_delivery())


async def test_send_rejection_detail_bounded_for_non_json_body(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(502, text="<html>" + "x" * 2000 + "</html>"))

    with pytest.raises(ChannelDeliveryError, match="HTTP 502") as excinfo:
        await TwilioChannel().deliver(make_delivery())

    assert len(str(excinfo.value)) < 600  # detail capped at 500 chars


async def test_transport_failure_raises_and_releases_reservation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(httpx.ConnectError("boom"))

    with pytest.raises(ChannelDeliveryError, match="transport") as excinfo:
        await TwilioChannel().deliver(make_delivery())

    assert isinstance(excinfo.value.__cause__, httpx.ConnectError)
    fake_httpx.responses.append(_accepted())
    await TwilioChannel().deliver(make_delivery())


async def test_no_recipient_sends_to_default(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await TwilioChannel().deliver(make_delivery())

    assert fake_httpx.calls[0]["data"]["To"] == "+15550000002"  # the operator default


async def test_allowlisted_recipient_sends_and_correlates_to_it(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await TwilioChannel().deliver(make_delivery(recipient="+15550000003"))

    assert fake_httpx.calls[0]["data"]["To"] == "+15550000003"
    # The reservation is keyed per recipient — the pair uses the actual target.
    assert list(fake_redis.store) == ["channel:twilio:pending:+15550000001:+15550000003"]

    # A different pair stays free: the default-recipient ask still succeeds.
    fake_httpx.responses.append(_accepted())
    await TwilioChannel().deliver(make_delivery(interaction_id="int-2"))
    assert fake_httpx.calls[1]["data"]["To"] == "+15550000002"


async def test_unlisted_recipient_rejected_and_nothing_sent(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_TWILIO_ALLOWED_RECIPIENTS"):
        await TwilioChannel().deliver(make_delivery(recipient="+15559999999"))

    assert not fake_httpx.calls  # nothing sent
    assert not fake_redis.store  # nothing reserved
    assert not [event for event in fake_redis.events if event[0] == "redis_set"]


async def test_json_allowlist_padded_entry_matches_unpadded_recipient(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_TWILIO_ALLOWED_RECIPIENTS", '[" +15550000003 ", ""]')
    reset_all_settings()

    fake_httpx.responses.append(_accepted())
    await TwilioChannel().deliver(make_delivery(recipient="+15550000003"))

    assert fake_httpx.calls[0]["data"]["To"] == "+15550000003"


async def test_empty_allowlist_rejects_any_requested_recipient(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_ALLOWED_RECIPIENTS")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_TWILIO_ALLOWED_RECIPIENTS"):
        await TwilioChannel().deliver(make_delivery(recipient="+15550000003"))

    assert not fake_httpx.calls
    assert not fake_redis.store


async def test_no_recipient_and_no_default_raises_delivery_error_before_any_work(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_DEFAULT_RECIPIENT")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_DEFAULT_RECIPIENT"):
        await TwilioChannel().deliver(make_delivery())

    assert not fake_redis.store  # nothing reserved
    assert not fake_httpx.calls  # nothing sent


async def test_deliver_missing_from_number_raises_delivery_error_before_any_work(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_FROM")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_FROM"):
        await TwilioChannel().deliver(make_delivery())

    assert not fake_httpx.calls  # nothing sent
    assert not fake_redis.store  # nothing reserved
    assert not fake_redis.events  # no HTTP call and no Redis write at all


async def test_deliver_missing_account_sid_raises_delivery_error_and_releases_reservation(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_ACCOUNT_SID")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_ACCOUNT_SID"):
        await TwilioChannel().deliver(make_delivery())

    assert not fake_httpx.calls  # checked before any network work — nothing sent
    assert not fake_redis.store  # the Tier-2 reservation was released, the pair is free


async def test_deliver_missing_auth_token_raises_delivery_error_and_releases_reservation(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_AUTH_TOKEN")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_AUTH_TOKEN"):
        await TwilioChannel().deliver(make_delivery())

    assert not fake_httpx.calls  # nothing sent
    assert not fake_redis.store  # the reservation was released


async def test_past_deadline_raises_before_any_send(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="already timed out"):
        await TwilioChannel().deliver(make_delivery(timeout_at=datetime.now(UTC) - timedelta(seconds=1)))

    assert not fake_redis.store
    assert not fake_httpx.calls


@pytest.mark.parametrize("answer_format", ["confirm", "external"])
async def test_tier1_past_deadline_raises_before_any_send(
    answer_format: str, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    with pytest.raises(ChannelDeliveryError, match="already timed out"):
        await TwilioChannel().deliver(
            make_delivery(answer_format=answer_format, timeout_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert not fake_httpx.calls  # the link would be dead on arrival — nothing sent
    assert not fake_redis.store
    assert not fake_redis.events  # no HTTP call and no reservation attempt at all


async def test_accepted_response_without_sid_raises(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(response(201, json={"status": "queued"}))

    with pytest.raises(ChannelDeliveryError, match="no MessageSid"):
        await TwilioChannel().deliver(make_delivery())


@pytest.mark.parametrize("answer_format", ["confirm", "external"])
async def test_tier1_link_send_skips_reservation(answer_format: str, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    delivery = make_delivery(answer_format=answer_format)
    await TwilioChannel().deliver(delivery)

    assert len(fake_httpx.calls) == 1
    body = fake_httpx.calls[0]["data"]["Body"]
    assert delivery.question in body
    assert delivery.callback_url in body  # plain tappable link
    assert not fake_redis.store  # no reservation, no correlation
    assert not [event for event in fake_redis.events if event[0] == "redis_set"]

    # The pair was never consumed: a follow-up typed-reply ask still succeeds.
    fake_httpx.responses.append(_accepted())
    await TwilioChannel().deliver(make_delivery(answer_format="text"))


async def test_notify_sends_plain_body_without_correlation(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await TwilioChannel().notify(ChannelNotification(message="Deploy finished."))

    assert len(fake_httpx.calls) == 1
    call = fake_httpx.calls[0]
    assert call["url"] == _MESSAGES_URL
    # No recipient requested — the operator default is the destination.
    assert call["data"] == {"To": "+15550000002", "From": "+15550000001", "Body": "Deploy finished."}
    assert call["auth"] == (_ACCOUNT_SID, "testtoken")
    # Fire-and-forget: no reservation, no Redis write of any kind.
    assert not fake_redis.store
    assert not [event for event in fake_redis.events if event[0] == "redis_set"]


async def test_notify_allowlisted_recipient_sends_to_it(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted())

    await TwilioChannel().notify(ChannelNotification(message="Heads up.", recipient="+15550000003"))

    assert fake_httpx.calls[0]["data"]["To"] == "+15550000003"
    assert not fake_redis.store


async def test_notify_unlisted_recipient_rejected_and_nothing_sent(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_TWILIO_ALLOWED_RECIPIENTS"):
        await TwilioChannel().notify(ChannelNotification(message="Heads up.", recipient="+15559999999"))

    assert not fake_httpx.calls  # nothing sent
    assert not fake_redis.store  # nothing reserved
    assert not fake_redis.events  # no HTTP call and no Redis write at all


async def test_notify_no_recipient_and_no_default_raises_delivery_error_before_any_work(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_DEFAULT_RECIPIENT")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_DEFAULT_RECIPIENT"):
        await TwilioChannel().notify(ChannelNotification(message="Heads up."))

    assert not fake_httpx.calls
    assert not fake_redis.store


async def test_notify_missing_from_number_raises_delivery_error_before_any_work(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_FROM")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_FROM"):
        await TwilioChannel().notify(ChannelNotification(message="Heads up."))

    assert not fake_httpx.calls  # nothing sent
    assert not fake_redis.store
    assert not fake_redis.events  # no HTTP call and no Redis write at all


async def test_notify_missing_auth_token_raises_delivery_error_and_nothing_sent(
    fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_AUTH_TOKEN")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TWILIO_AUTH_TOKEN"):
        await TwilioChannel().notify(ChannelNotification(message="Heads up."))

    assert not fake_httpx.calls  # checked before any network work — nothing sent
    assert not fake_redis.store  # notify never touches the correlation store
    assert not fake_redis.events


async def test_notify_twilio_rejection_raises_delivery_error(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The WhatsApp 24-hour-window rejection (error 63016) is one such response:
    # a freeform Body outside the window surfaces as this loud error.
    fake_httpx.responses.append(response(400, json={"code": 63016, "message": "Outside the allowed window"}))

    with pytest.raises(ChannelDeliveryError, match=r"HTTP 400.*63016"):
        await TwilioChannel().notify(ChannelNotification(message="Heads up.", recipient="whatsapp:+15550000004"))

    assert not fake_redis.store  # still nothing reserved on failure
