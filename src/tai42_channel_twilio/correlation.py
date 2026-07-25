"""Pending-question correlation store (plugin-owned Redis).

SMS has no threading, so the only correlation key is the number pair
(<Twilio number>, <human number>). AT MOST ONE pending question per pair — an
atomic ``SET NX`` reservation; a second concurrent question is rejected loudly
with ``PendingQuestionExistsError``. The value holds the ``callback_url`` and
deadline; the key TTL is the remaining answer budget, so an expired question
cannot capture a later reply.

``pop_pending`` claims with ``GETDEL`` (atomic — one of two concurrent webhooks
wins). ``restore_pending`` puts a popped question back with its remaining TTL
after a failed forward, itself a ``SET NX`` so it never overwrites a NEW
reservation that took the pair in the gap (refused with a loud log instead).

A handled-``MessageSid`` set is the replay guard (Twilio's signature scheme
carries no timestamp).
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
    """Atomically claim-and-remove the pending question; ``None`` when there is
    none (``GETDEL`` — a concurrent duplicate webhook gets ``None``)."""
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
    """Put a popped question back with its remaining TTL after a failed forward.

    A ``SET NX``: if a NEW question reserved the pair in the gap, the restore is
    refused with a loud log rather than misrouting the new question's reply. A
    question past its deadline is not restored.
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
