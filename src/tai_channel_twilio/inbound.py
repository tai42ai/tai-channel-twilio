"""Inbound Twilio webhook — where the human's reply enters the system.

``POST /api/channels/twilio/inbound`` is registered unauthenticated (Twilio cannot
send the platform api key); authentication is Twilio's ``X-Twilio-Signature``
instead: ``base64(HMAC-SHA1(auth_token, public_url + concat(sorted form pairs)))``,
validated fail-closed before one byte of the body is trusted.

Two rules make the validation correct in real deployments:

* The URL in the HMAC must be the EXACT public URL Twilio called. Behind a
  TLS-terminating proxy the app sees an internal URL, so the public one is
  reconstructed from ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` (set by the
  deployment's own trusted proxy). The trust direction is safe: a spoofed
  forwarded header changes the URL the HMAC is computed over, so it can only
  make validation FAIL — forging a PASS still requires the auth token.
* The params in the HMAC must be the RAW form pairs, duplicates included
  (``parse_qsl`` over the raw body). Collapsing the form into a dict drops
  duplicate keys, which both breaks the signature and hides data.

Twilio's scheme carries no timestamp, so a captured request validates forever:
the ``MessageSid`` dedupe in the correlation store is the replay guard.

The answer is forwarded to the callback_url as the JSON object
``{"answer": <value>}`` where the value is the message ``Body`` verbatim (outer
whitespace stripped) — correlation lives entirely out-of-band in the number pair,
so the human's text needs no code, tag, or prefix, and none is ever stripped
because none is ever added. The door validates the value against the question's
stored answer_format and records the typed result.
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
from tai_contract.app import tai_app
from tai_kit.clients.impl.http import HttpxClient

from tai_channel_twilio.correlation import (
    PendingQuestion,
    already_seen,
    mark_seen,
    pop_pending,
    restore_pending,
)
from tai_channel_twilio.settings import require_secret, twilio_settings

logger = logging.getLogger(__name__)

_SIGNATURE_HEADER = "X-Twilio-Signature"
_SHA1_DIGEST_LEN = hashlib.sha1().digest_size  # 20 bytes; 28 chars in base64
# An inbound SMS webhook body is a few KiB; an unauthenticated door still
# bounds what it reads into memory — loud 413, never a truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024


class SignatureRejectedError(Exception):
    """The request failed Twilio signature authentication (mapped to 401)."""


class PayloadTooLargeError(Exception):
    """The inbound body exceeded the unauthenticated door's byte cap (mapped to 413)."""


class AnswerForwardError(Exception):
    """The interactions callback door did not accept the forwarded answer."""


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Read the body counting ACTUAL bytes, never a client ``Content-Length``.

    Raises ``PayloadTooLargeError`` past ``cap`` BEFORE any signature work —
    loud, never truncated.
    """
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

    Scheme from ``X-Forwarded-Proto`` and host from ``X-Forwarded-Host`` when the
    trusted fronting proxy set them (first value when the proxy chain appended a
    list), else the request's own scheme/Host. Path and raw query come from the
    request unchanged.
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
    """Validate ``X-Twilio-Signature``, or raise ``SignatureRejectedError``.

    Twilio's documented algorithm: append each POST param's name+value (no
    delimiters) to the full URL, params sorted by name; HMAC-SHA1 the result
    with the auth token; base64-encode; compare. The compare is constant-time
    over the decoded digest bytes.
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


async def _forward_answer(callback_url: str, answer: str) -> httpx.Response:
    """POST the answer to the interaction's ticketed callback door.

    The body is the JSON object ``{"answer": <value>}`` — the door validates
    the value against the question's stored answer_format (text→str,
    select∈options; confirm and external asks are Tier-1 links and never
    reach this forward) and records the typed result; a bare non-object body
    would be rejected 400. Returns the door's response; the caller applies
    the status policy (2xx answered, 404 terminal, 400 rejected-keep,
    anything else restore-and-raise).
    """
    async with tai_app.clients.client_ctx(HttpxClient, timeout=twilio_settings().http_timeout_seconds) as client:
        return await client.post(callback_url, json={"answer": answer})


@tai_app.http.custom_route(
    "/api/channels/twilio/inbound",
    methods=["POST"],
    summary="Twilio inbound message webhook",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def twilio_inbound(request: Request) -> Response:
    """Receive a Twilio inbound message and resolve the pair's pending question.

    Order is load-bearing: bounded body read first (413 before any HMAC work),
    signature second (nothing is trusted before it), MessageSid dedupe third
    (replay guard), atomic pending claim fourth, forward fifth. The forward's
    status policy: 2xx answered; 404 terminal (drop correlation + ack, the
    ticket is gone); 400 rejected (restore so the human can re-reply); anything
    else restores the pending question and raises, so Twilio's webhook retry
    still resolves the ask — the answer is never silently lost.
    """
    # Empty/missing auth token = operator misconfiguration -> logged 500 (fail
    # CLOSED), never a 401 that reads like an ordinary bad signature.
    try:
        auth_token = require_secret(twilio_settings().auth_token, "CHANNEL_TWILIO_AUTH_TOKEN")
    except ValueError:
        logger.error("twilio inbound: CHANNEL_TWILIO_AUTH_TOKEN is unset or empty; failing closed")
        return JSONResponse({"error": "channel misconfigured"}, status_code=500)

    try:
        raw = await _read_bounded_body(request, _MAX_BODY_BYTES)
    except PayloadTooLargeError:
        return PlainTextResponse("payload too large", status_code=413)
    try:
        body_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Twilio signs UTF-8 form bodies; undecodable bytes can never carry a
        # valid signature — the same clean reject, never an unhandled 500.
        logger.warning("rejected Twilio inbound: body is not valid UTF-8")
        return PlainTextResponse("signature verification failed", status_code=401)
    form_pairs = parse_qsl(body_text, keep_blank_values=True)
    try:
        public_url = _reconstruct_public_url(request)
        _validate_signature(auth_token, public_url, form_pairs, request.headers.get(_SIGNATURE_HEADER))
    except SignatureRejectedError as exc:
        logger.warning("rejected Twilio inbound: %s", exc)
        return PlainTextResponse("signature verification failed", status_code=401)

    # Single-value access is safe only now, AFTER the signature validated the
    # full raw pair list (duplicates included).
    form = dict(form_pairs)
    message_sid = form.get("MessageSid")
    if not message_sid:
        return PlainTextResponse("missing MessageSid", status_code=400)

    if await already_seen(message_sid):
        return Response(status_code=204)

    # Inbound direction: To = the deployment's Twilio number, From = the human.
    pending = await pop_pending(twilio_number=form.get("To", ""), human_number=form.get("From", ""))
    if pending is None:
        # No pending question for this pair — an unrelated text, or the question
        # expired. Twilio's webhook contract forbids 5xx-ing ordinary traffic,
        # so this is a logged drop; a waiting ask (if any) fails loudly by its
        # own timeout.
        logger.warning("uncorrelated Twilio inbound %s dropped (no pending question)", message_sid)
        await mark_seen(message_sid)
        return Response(status_code=204)

    answer = form.get("Body", "").strip()
    try:
        forwarded = await _forward_answer(pending.callback_url, answer)
    except Exception:
        # Transport failure — restore so the webhook retry (the raise -> 5xx
        # -> Twilio redelivers) or the human's next message still resolves it.
        await _restore(form, pending)
        raise

    if forwarded.status_code // 100 == 2:
        await mark_seen(message_sid)
        return Response(status_code=204)
    if forwarded.status_code == 404:
        # Terminal: the ticket is gone (the ask timed out, was pruned, or the
        # single-use claim already fired). Retrying the same forward can never
        # succeed, so the correlation stays dropped (already popped) and the
        # delivery is acked — no redelivery storm on a dead ticket.
        logger.warning("callback door returned terminal HTTP 404 for %s; dropping the correlation", message_sid)
        await mark_seen(message_sid)
        return Response(status_code=204)
    if forwarded.status_code == 400:
        # The door rejected THIS answer (e.g. a select reply not in options).
        # Keep the correlation so the human can reply again; redelivering the
        # same body can never help, so the delivery is acked.
        logger.warning(
            "callback door rejected the answer for %s (400); correlation kept so the human can re-reply",
            message_sid,
        )
        await _restore(form, pending)
        await mark_seen(message_sid)
        return Response(status_code=204)
    # 401/413/5xx: ambient failure — keep the correlation and fail the delivery
    # loudly; Twilio's webhook retry re-pops the restored question and forwards
    # again. Loud, convergent, no loss.
    await _restore(form, pending)
    raise AnswerForwardError(
        f"interactions callback rejected the answer: HTTP {forwarded.status_code}: {forwarded.text[:500]}"
    )


async def _restore(form: dict[str, str], pending: PendingQuestion) -> None:
    await restore_pending(twilio_number=form.get("To", ""), human_number=form.get("From", ""), question=pending)
