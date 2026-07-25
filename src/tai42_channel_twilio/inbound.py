"""Inbound Twilio webhook — where the human's reply enters the system.

``POST /api/channels/twilio/inbound`` is unauthenticated (Twilio cannot send the
platform api key); auth is the ``X-Twilio-Signature`` HMAC, validated fail-closed
before any byte of the body is trusted. Two correctness rules:

* The signed URL must be the EXACT public URL Twilio called, reconstructed from
  ``X-Forwarded-Proto``/``X-Forwarded-Host`` behind a TLS-terminating proxy. A
  spoofed header only changes the URL the HMAC is computed over, so it can only
  make validation FAIL — forging a PASS still needs the auth token.
* The signed params must be the RAW form pairs including duplicates
  (``parse_qsl`` over the raw body); collapsing to a dict drops duplicate keys
  and breaks the signature.

Twilio's scheme carries no timestamp, so the ``MessageSid`` dedupe is the replay
guard. A reply matching a pending question is forwarded as ``{"answer": <Body
verbatim, outer whitespace stripped>}``; on a correlation miss the message enters
the conversation bridge instead. The signed status door reuses the same auth.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from urllib.parse import parse_qsl

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.conversations import DeliveryReceipt
from tai42_kit.clients.impl.http import HttpxClient

from tai42_channel_twilio.correlation import (
    PendingQuestion,
    already_seen,
    mark_seen,
    pop_pending,
    restore_pending,
)
from tai42_channel_twilio.settings import require_secret, twilio_settings

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Twilio-Signature"
_SHA1_DIGEST_LEN = hashlib.sha1().digest_size  # 20 bytes; 28 chars in base64
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024

# Twilio MessageStatus → the terminal receipt to record; anything else (queued,
# sent, sending, ...) is a non-terminal no-op.
_DELIVERY_RECEIPTS = {
    "failed": DeliveryReceipt.FAILED,
    "undelivered": DeliveryReceipt.FAILED,
    "delivered": DeliveryReceipt.DELIVERED,
}


class SignatureRejectedError(Exception):
    """The request failed Twilio signature authentication (mapped to 401)."""


class PayloadTooLargeError(Exception):
    """The inbound body exceeded the unauthenticated door's byte cap (mapped to 413)."""


class AnswerForwardError(Exception):
    """The interactions callback door did not accept the forwarded answer."""


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Read the body counting ACTUAL bytes, never a client ``Content-Length``.
    Raises ``PayloadTooLargeError`` past ``cap`` before any signature work."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise PayloadTooLargeError("request body exceeds the configured cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _reconstruct_public_url(request: Request) -> str:
    """The exact public URL Twilio called, as seen from outside the proxy.

    Scheme/host from ``X-Forwarded-Proto``/``X-Forwarded-Host`` when the trusted
    proxy set them (first value of a list), else the request's own; path and raw
    query from the request unchanged.
    """
    proto_header = request.headers.get("x-forwarded-proto")
    proto = proto_header.split(",")[0].strip() if proto_header else request.url.scheme
    host_header = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if not host_header:
        raise SignatureRejectedError("request carries no Host header — cannot reconstruct the signed URL")
    host = host_header.split(",")[0].strip()
    url = f"{proto}://{host}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return url


def _validate_signature(
    auth_token: str, public_url: str, form_pairs: list[tuple[str, str]], provided: str | None
) -> None:
    """Validate ``X-Twilio-Signature`` or raise ``SignatureRejectedError``.

    Twilio's algorithm: append each POST param's name+value (params sorted by
    name, no delimiters) to the full URL, HMAC-SHA1 with the auth token, base64.
    Compared constant-time over the decoded digest bytes.
    """
    if provided is None:
        raise SignatureRejectedError(f"missing {_SIGNATURE_HEADER} header")
    payload = public_url + "".join(name + value for name, value in sorted(form_pairs))
    expected = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    try:
        provided_digest = base64.b64decode(provided, validate=True)
    except ValueError as exc:
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} is not valid base64") from exc
    if len(provided_digest) != _SHA1_DIGEST_LEN:
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} is not a SHA-1 digest")
    if not hmac.compare_digest(provided_digest, expected):
        raise SignatureRejectedError(f"{_SIGNATURE_HEADER} mismatch")


async def _authenticated_form_pairs(request: Request) -> list[tuple[str, str]]:
    """Bounded-read and Twilio-signature-validate the request; return the RAW form
    pairs (duplicates kept). Nothing in the body is trusted until the signature
    validates. Raises ``ValueError`` (auth token unset → logged 500),
    ``PayloadTooLargeError`` (→ 413), or ``SignatureRejectedError`` (→ 401)."""
    auth_token = require_secret(twilio_settings().auth_token, "CHANNEL_TWILIO_AUTH_TOKEN")
    raw = await _read_bounded_body(request, _MAX_BODY_BYTES)
    try:
        body_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Twilio signs UTF-8 bodies; undecodable bytes can't carry a valid signature.
        raise SignatureRejectedError("body is not valid UTF-8") from exc
    form_pairs = parse_qsl(body_text, keep_blank_values=True)
    public_url = _reconstruct_public_url(request)
    _validate_signature(auth_token, public_url, form_pairs, request.headers.get(_SIGNATURE_HEADER))
    return form_pairs


def _auth_error_response(exc: ValueError | PayloadTooLargeError | SignatureRejectedError, route: str) -> Response:
    """Map an ``_authenticated_form_pairs`` failure to its response: 413 oversize,
    401 bad signature, 500 for an unset auth token (operator misconfig, never a
    401 that reads like an ordinary bad signature)."""
    if isinstance(exc, PayloadTooLargeError):
        return PlainTextResponse("payload too large", status_code=413)
    if isinstance(exc, SignatureRejectedError):
        logger.warning("rejected Twilio %s: %s", route, exc)
        return PlainTextResponse("signature verification failed", status_code=401)
    logger.error("twilio %s: CHANNEL_TWILIO_AUTH_TOKEN is unset or empty; failing closed", route)
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


async def _forward_answer(callback_url: str, answer: str) -> httpx.Response:
    """POST ``{"answer": <value>}`` to the interaction's callback door and return
    its response; the caller applies the status policy."""
    async with tai42_app.clients.client_ctx(HttpxClient, timeout=twilio_settings().http_timeout_seconds) as client:
        return await client.post(callback_url, json={"answer": answer})


@tai42_app.http.custom_route(
    "/api/channels/twilio/inbound",
    methods=["POST"],
    summary="Twilio inbound message webhook",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def twilio_inbound(request: Request) -> Response:
    """Receive a Twilio inbound message and resolve the pair's pending question,
    or route it into the conversation bridge.

    Order is load-bearing: bounded body read (413) → signature (nothing trusted
    before it) → MessageSid dedupe → atomic pending claim → forward. A correlated
    reply forwards to the callback door; a correlation MISS goes to the bridge.
    Forward status policy: 2xx answered; 404 terminal (drop correlation + ack);
    400 rejected (restore so the human can re-reply); anything else restores and
    raises so Twilio's retry re-resolves — the answer is never silently lost.
    """
    try:
        form_pairs = await _authenticated_form_pairs(request)
    except (ValueError, PayloadTooLargeError, SignatureRejectedError) as exc:
        return _auth_error_response(exc, "inbound")

    # Collapse to single values only now, after the signature validated the raw pairs.
    form = dict(form_pairs)
    message_sid = form.get("MessageSid")
    if not message_sid:
        return PlainTextResponse("missing MessageSid", status_code=400)

    if await already_seen(message_sid):
        return Response(status_code=204)

    # Inbound direction: To = the deployment's Twilio number, From = the human.
    pending = await pop_pending(twilio_number=form.get("To", ""), human_number=form.get("From", ""))
    if pending is None:
        # No pending question (unrelated text or expired) — route to the bridge.
        return await _bridge_inbound(form, message_sid)

    answer = form.get("Body", "").strip()
    try:
        forwarded = await _forward_answer(pending.callback_url, answer)
    except Exception:
        # Transport failure — restore so the webhook retry or the human's next
        # message still resolves it.
        await _restore(form, pending)
        raise

    if forwarded.status_code // 100 == 2:
        await mark_seen(message_sid)
        return Response(status_code=204)
    if forwarded.status_code == 404:
        # Terminal: the ticket is gone; retrying can't succeed, so stay dropped
        # and ack (no redelivery storm on a dead ticket).
        logger.warning("callback door returned terminal HTTP 404 for %s; dropping the correlation", message_sid)
        await mark_seen(message_sid)
        return Response(status_code=204)
    if forwarded.status_code == 400:
        # The door rejected THIS answer; keep the correlation so the human can
        # re-reply, and ack (the same body would be rejected again).
        logger.warning(
            "callback door rejected the answer for %s (400); correlation kept so the human can re-reply",
            message_sid,
        )
        await _restore(form, pending)
        await mark_seen(message_sid)
        return Response(status_code=204)
    # 401/413/5xx ambient failure — keep the correlation and fail loudly; Twilio's
    # retry re-pops the restored question and forwards again.
    await _restore(form, pending)
    raise AnswerForwardError(
        f"interactions callback rejected the answer: HTTP {forwarded.status_code}: {forwarded.text[:500]}"
    )


async def _restore(form: dict[str, str], pending: PendingQuestion) -> None:
    await restore_pending(twilio_number=form.get("To", ""), human_number=form.get("From", ""), question=pending)


async def _bridge_inbound(form: dict[str, str], message_sid: str) -> Response:
    """Route an uncorrelated inbound message into the conversation bridge.

    ``our_identity`` = To, ``client_address`` = From (verbatim). A message with no
    route bound is logged and success-acked (the provider must not retry-storm a
    permanently-unrouted address); a retryable overflow or infrastructure failure
    propagates as a 5xx so Twilio redelivers rather than silently dropping it.
    """
    try:
        await tai42_app.conversations.accept(
            channel="twilio",
            our_identity=form.get("To", ""),
            client_address=form.get("From", ""),
            text=form.get("Body", ""),
            provider_message_id=message_sid,
        )
    except LookupError as exc:
        logger.warning("unrouted Twilio inbound %s dropped: %s", message_sid, exc)
    await mark_seen(message_sid)
    return Response(status_code=204)


@tai42_app.http.custom_route(
    "/api/channels/twilio/status",
    methods=["POST"],
    summary="Twilio delivery status webhook",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def twilio_status(request: Request) -> Response:
    """Ingest a Twilio-signed delivery-status callback for a bridge outbound message.

    ``failed``/``undelivered`` → record FAILED; ``delivered`` → DELIVERED; every
    intermediate status (queued/sent/sending/...) is a benign no-op. A status for a
    ``MessageSid`` the bridge does not track is acked, never a 5xx — the provider
    must not retry a message we do not own.
    """
    try:
        form_pairs = await _authenticated_form_pairs(request)
    except (ValueError, PayloadTooLargeError, SignatureRejectedError) as exc:
        return _auth_error_response(exc, "status")

    form = dict(form_pairs)
    message_sid = form.get("MessageSid")
    if not message_sid:
        return PlainTextResponse("missing MessageSid", status_code=400)
    status = form.get("MessageStatus")
    if not status:
        return PlainTextResponse("missing MessageStatus", status_code=400)

    receipt = _DELIVERY_RECEIPTS.get(status)
    if receipt is None:
        return Response(status_code=204)

    try:
        await tai42_app.conversations.record_delivery_status("twilio", message_sid, receipt)
    except LookupError as exc:
        logger.info("twilio status for untracked message %s ignored: %s", message_sid, exc)
    return Response(status_code=204)
