"""The inbound webhook handler — signature validation, dedupe, correlation, forward policy."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx
import pytest
from starlette.responses import Response

import tai42_channel_twilio.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_twilio.correlation import reserve_pending
from tai42_channel_twilio.inbound import AnswerForwardError
from tests.conftest import (
    FakeHttpx,
    FakeRedis,
    build_request,
    compute_signature,
    make_delivery,
    response,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("twilio_env")

_PATH = "/api/channels/twilio/inbound"
_TWILIO = "+15550000001"
_HUMAN = "+15550000002"
_CALLBACK = "https://app.example/api/interactions/callback/ticket-1"
_SEEN_KEY = "channel:twilio:seen:SM777"


def _pairs(**overrides: str) -> list[tuple[str, str]]:
    form = {"MessageSid": "SM777", "To": _TWILIO, "From": _HUMAN, "Body": "yes please"}
    form.update(overrides)
    return list(form.items())


@pytest.fixture
def handler(stub_app) -> Callable[..., Awaitable[Response]]:
    routes = [route for route in stub_app.http.routes if route.path == _PATH]
    assert len(routes) == 1
    route = routes[0]
    assert route.methods == ["POST"]
    assert route.authed is False
    return route.handler


async def _seed_pending(callback_url: str = _CALLBACK) -> None:
    delivery = make_delivery(callback_url=callback_url)
    await reserve_pending(_TWILIO, _HUMAN, delivery.callback_url, delivery.timeout_at)


async def _pending_intact(fake_redis: FakeRedis) -> bool:
    return f"channel:twilio:pending:{_TWILIO}:{_HUMAN}" in fake_redis.store


# --- Happy path ---------------------------------------------------------------


async def test_valid_signature_resolves_pending(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert len(fake_httpx.calls) == 1
    assert fake_httpx.calls[0]["url"] == _CALLBACK
    assert fake_httpx.calls[0]["json"] == {"answer": "yes please"}
    assert not await _pending_intact(fake_redis)  # consumed
    assert _SEEN_KEY in fake_redis.store  # sid marked seen


async def test_answer_is_body_verbatim_minus_outer_whitespace(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    await handler(signed_request(_pairs(Body=" yes please \n")))

    assert fake_httpx.calls[0]["json"] == {"answer": "yes please"}


async def test_public_url_reconstructed_from_forwarded_headers(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The ASGI scope says http://app-internal:8000, the forwarded headers say
    # https://public.example — the signature over the PUBLIC url must validate.
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(_pairs(), proto="https", host="public.example"))

    assert result.status_code == 204


async def test_duplicate_form_keys_validate(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A dict-collapsing implementation would drop one MediaUrl0 pair and 401 here.
    await _seed_pending()
    fake_httpx.responses.append(response(200))
    pairs = [*_pairs(), ("MediaUrl0", "https://a.example/1"), ("MediaUrl0", "https://b.example/2")]

    result = await handler(signed_request(pairs))

    assert result.status_code == 204


async def test_query_string_is_part_of_the_signed_url(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(_pairs(), query="x=1"))
    assert result.status_code == 204

    # The same request signed WITHOUT the query must be rejected.
    await _seed_pending()
    rejected = await handler(signed_request(_pairs(), query="x=1", sign_url=f"https://public.example{_PATH}"))
    assert rejected.status_code == 401


async def test_fallback_to_request_scheme_and_host_without_proxy_headers(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # No X-Forwarded-* headers: the signed URL is the request's own scheme + Host.
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    def strip_forwarded(headers: dict[str, str]) -> None:
        del headers["x-forwarded-proto"]
        del headers["x-forwarded-host"]

    result = await handler(
        signed_request(_pairs(), sign_url=f"http://app-internal:8000{_PATH}", tamper=strip_forwarded)
    )

    assert result.status_code == 204


# --- Fail-closed branches -----------------------------------------------------


async def _assert_rejected(handler, request, fake_redis: FakeRedis, fake_httpx: FakeHttpx, status: int = 401):
    result = await handler(request)
    assert result.status_code == status
    assert not fake_httpx.calls  # zero forwards
    assert await _pending_intact(fake_redis)  # pending NOT consumed


async def test_missing_signature_header_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    await _assert_rejected(handler, signed_request(_pairs(), omit_signature=True), fake_redis, fake_httpx)


async def test_wrong_token_signature_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    await _assert_rejected(handler, signed_request(_pairs(), token="other-token"), fake_redis, fake_httpx)


async def test_url_mismatch_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    request = signed_request(_pairs(), sign_url=f"https://evil.example{_PATH}")
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_non_base64_signature_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    request = signed_request(_pairs(), signature="!!! not base64 !!!")
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_wrong_digest_length_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    request = signed_request(_pairs(), signature="c2hvcnQ=")  # base64("short") — 5 bytes, not 20
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_empty_auth_token_fails_closed_with_config_error(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    await _seed_pending()
    monkeypatch.setenv("CHANNEL_TWILIO_AUTH_TOKEN", "")
    reset_all_settings()

    # Operator misconfiguration is a logged, constant 500 — never a 401 that
    # reads like an ordinary bad signature, never any processing.
    result = await handler(signed_request(_pairs()))

    assert result.status_code == 500
    assert json.loads(result.body) == {"error": "channel misconfigured"}
    assert not fake_httpx.calls
    assert await _pending_intact(fake_redis)


async def test_tampered_body_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    # Sign one body, deliver another.
    signature = compute_signature("testtoken", f"https://public.example{_PATH}", _pairs())
    request = signed_request(_pairs(Body="no way"), signature=signature)
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_missing_host_header_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()

    def strip_hosts(headers: dict[str, str]) -> None:
        del headers["x-forwarded-host"]
        del headers["host"]

    await _assert_rejected(handler, signed_request(_pairs(), tamper=strip_hosts), fake_redis, fake_httpx)


async def test_oversized_body_413_before_any_signature_work(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    # No X-Twilio-Signature header at all, yet the response is 413 (not 401):
    # the bounded read runs BEFORE any HMAC work.
    request = build_request(
        body=b"",
        chunks=[b"x" * (512 * 1024), b"y" * (512 * 1024), b"z"],
        headers={"host": "app-internal:8000", "content-type": "application/x-www-form-urlencoded"},
    )
    await _assert_rejected(handler, request, fake_redis, fake_httpx, status=413)


async def test_non_utf8_body_is_clean_401(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    await _seed_pending()
    request = build_request(
        body=b"\xff\xfe",
        headers={
            "host": "app-internal:8000",
            "content-type": "application/x-www-form-urlencoded",
            "x-twilio-signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        },
    )

    with caplog.at_level("WARNING"):
        await _assert_rejected(handler, request, fake_redis, fake_httpx)

    assert any("not valid UTF-8" in record.message for record in caplog.records)


# --- Post-signature branches ---------------------------------------------------


async def test_missing_message_sid_is_400(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    pairs = [pair for pair in _pairs() if pair[0] != "MessageSid"]
    await _assert_rejected(handler, signed_request(pairs), fake_redis, fake_httpx, status=400)


async def test_message_sid_dedupe_skips_second_delivery(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    first = await handler(signed_request(_pairs()))
    second = await handler(signed_request(_pairs()))

    assert first.status_code == 204
    assert second.status_code == 204
    assert len(fake_httpx.calls) == 1  # no second forward


async def test_uncorrelated_inbound_dropped_with_warning(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert not fake_httpx.calls
    assert any("uncorrelated" in record.message for record in caplog.records)
    assert _SEEN_KEY in fake_redis.store  # replay of the same sid dedupes


async def test_door_5xx_restores_pending_and_raises_so_twilio_retries(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_pending()
    fake_httpx.responses.append(response(500, text="oops"))

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(_pairs()))

    assert await _pending_intact(fake_redis)  # restored
    assert _SEEN_KEY not in fake_redis.store  # NOT marked seen — the retry must not dedupe away

    # Twilio's webhook retry (same request) pops the restored question and lands it.
    fake_httpx.responses.append(response(200))
    retry = await handler(signed_request(_pairs()))
    assert retry.status_code == 204
    assert len(fake_httpx.calls) == 2
    assert not await _pending_intact(fake_redis)


async def test_door_404_is_terminal_drop(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    await _seed_pending()
    fake_httpx.responses.append(response(404))

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert not await _pending_intact(fake_redis)  # correlation stays dropped
    assert _SEEN_KEY in fake_redis.store
    assert any("terminal HTTP 404" in record.message for record in caplog.records)

    # Redelivery dedupes — no retry storm on a dead ticket.
    redelivery = await handler(signed_request(_pairs()))
    assert redelivery.status_code == 204
    assert len(fake_httpx.calls) == 1


async def test_door_400_keeps_correlation_for_a_re_reply(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    await _seed_pending()
    fake_httpx.responses.append(response(400))

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert await _pending_intact(fake_redis)  # restored — the human can reply again
    assert _SEEN_KEY in fake_redis.store
    assert any("rejected the answer" in record.message for record in caplog.records)

    # A NEW reply from the pair (new sid) pops it and forwards.
    fake_httpx.responses.append(response(200))
    retry = await handler(signed_request(_pairs(MessageSid="SM778", Body="option two")))
    assert retry.status_code == 204
    assert fake_httpx.calls[-1]["json"] == {"answer": "option two"}
    assert not await _pending_intact(fake_redis)


async def test_forward_transport_failure_restores_and_propagates(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(httpx.ConnectError("door unreachable"))

    with pytest.raises(httpx.ConnectError):
        await handler(signed_request(_pairs()))

    assert await _pending_intact(fake_redis)  # restored
    assert _SEEN_KEY not in fake_redis.store  # not marked seen
