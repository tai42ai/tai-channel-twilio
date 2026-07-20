"""Pending-question correlation store (plugin-owned Redis).

SMS has no threading: the only native correlation key is the number pair
(<Twilio number>, <human number>). This store therefore holds AT MOST ONE pending
question per pair — an atomic ``SET NX`` reservation. A second concurrent question
to the same pair is rejected loudly with ``PendingQuestionExistsError`` (never
queued, never silently replacing the first). The value carries the ticketed
``callback_url`` and the question deadline; the key's TTL is exactly the remaining
answer budget, so an expired question can never capture a later, unrelated reply.

``pop_pending`` claims with ``GETDEL`` — atomic, so of two concurrent webhook
deliveries only one obtains the callback_url. ``restore_pending`` puts a popped
question back (with its remaining TTL) when forwarding the answer failed, so the
webhook retry — or the human's next message — can still resolve it. The restore
is itself a ``SET NX``: if a NEW question has already reserved the pair in the
gap, the restore is refused with a loud error log instead of overwriting —
never a blind overwrite that would misroute the new question's reply.

The store also remembers handled ``MessageSid`` values: Twilio's signature scheme
has no timestamp, so this dedupe set is the replay guard.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_twilio.settings import TwilioRedisSettings, twilio_redis_settings, twilio_settings

logger = logging.getLogger(__name__)


class PendingQuestionExistsError(ChannelDeliveryError):
    """A question is already pending for this number pair (one at a time)."""


@dataclass(frozen=True)
class PendingQuestion:
    callback_url: str
    timeout_at: datetime


def _pending_key(twilio_number: str, human_number: str) -> str:
    return f"channel:twilio:pending:{twilio_number}:{human_number}"


def _seen_key(message_sid: str) -> str:
    return f"channel:twilio:seen:{message_sid}"


def _redis_settings() -> TwilioRedisSettings:
    """The correlation-store connection, raising a clear config error when unset."""
    settings = twilio_redis_settings()
    if not settings.redis_url:
        raise ValueError("Twilio channel correlation store is not configured: set CHANNEL_TWILIO_REDIS_URL.")
    return settings


def _remaining_seconds(timeout_at: datetime) -> int:
    """Whole seconds until the deadline, raising when it already passed."""
    remaining = math.ceil((timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        raise ChannelDeliveryError(f"question deadline {timeout_at.isoformat()} has already passed")
    return remaining


async def reserve_pending(twilio_number: str, human_number: str, callback_url: str, timeout_at: datetime) -> None:
    """Atomically reserve the pair for one question, or raise ``PendingQuestionExistsError``."""
    value = json.dumps({"callback_url": callback_url, "timeout_at": timeout_at.isoformat()})
    ttl = _remaining_seconds(timeout_at)
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        stored = await redis.set(_pending_key(twilio_number, human_number), value, nx=True, ex=ttl)
    if not stored:
        raise PendingQuestionExistsError(
            f"a question is already pending for the pair ({twilio_number}, {human_number}); "
            "one pending question per number pair — answer or let it time out first"
        )


async def release_pending(twilio_number: str, human_number: str) -> None:
    """Drop the reservation (the send failed — the human never received the question)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.delete(_pending_key(twilio_number, human_number))


async def pop_pending(twilio_number: str, human_number: str) -> PendingQuestion | None:
    """Atomically claim-and-remove the pending question; ``None`` when there is none.

    ``GETDEL`` guarantees a concurrent duplicate webhook delivery gets ``None``.
    """
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        raw = await redis.getdel(_pending_key(twilio_number, human_number))
    if raw is None:
        return None
    data = json.loads(raw)
    return PendingQuestion(
        callback_url=data["callback_url"],
        timeout_at=datetime.fromisoformat(data["timeout_at"]),
    )


async def restore_pending(twilio_number: str, human_number: str, question: PendingQuestion) -> None:
    """Put a popped question back with its remaining TTL (after a failed answer forward).

    The restore is a ``SET NX``: if a NEW question has already reserved the
    pair in the gap since the pop, overwriting it would misroute the new
    question's reply to the old callback_url — so the restore is refused with
    a LOUD error log instead, and the old ask resolves by its own timeout (the
    door's single-use claim keeps a late duplicate forward harmless). A
    question already past its deadline is not restored — the waiting ask has
    timed out on its own side, and a key with a non-positive TTL is invalid.
    """
    remaining = math.ceil((question.timeout_at - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        return
    value = json.dumps({"callback_url": question.callback_url, "timeout_at": question.timeout_at.isoformat()})
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        stored = await redis.set(_pending_key(twilio_number, human_number), value, nx=True, ex=remaining)
    if not stored:
        logger.error(
            "could not restore the pending question for (%s, %s): a new question has since "
            "reserved the pair; the old ask will resolve by its own timeout",
            twilio_number,
            human_number,
        )


async def already_seen(message_sid: str) -> bool:
    """Whether this ``MessageSid`` was already handled (webhook retry or replay)."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        return bool(await redis.exists(_seen_key(message_sid)))


async def mark_seen(message_sid: str) -> None:
    """Remember a handled ``MessageSid`` for the configured dedupe window."""
    async with tai42_app.clients.client_ctx(RedisClient, _redis_settings()) as redis:
        await redis.set(_seen_key(message_sid), "1", ex=twilio_settings().dedupe_ttl)
