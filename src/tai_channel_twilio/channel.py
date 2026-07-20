"""The Twilio channel — ``deliver`` pushes one question to the human's phone,
``notify`` pushes one fire-and-forget message with no reply expected.

The correlation reservation happens BEFORE the outbound send: by the time Twilio
accepts the message the store already knows the pair's ``callback_url``, so an
inbound reply racing the send response can never arrive before the store can
match it. A failed send releases the reservation and raises — the ask helper
prunes the interaction loudly; nothing dangles.

``answer_format`` ``"confirm"`` and ``"external"`` are Tier-1: the SMS carries
the callback_url as a plain tappable link and NO correlation is stored — the
human answers in the browser via the callback door (a confirm tap records the
bool; an external ask completes its flow there), so no SMS reply is expected or
matched. Confirm MUST take this path: the door accepts only the bool recorded
from the link tap for a confirm ask, so a typed SMS reply could never resolve
it. Tier-2 (typed reply over the correlation store) is ``"text"`` and
``"select"`` only.

WhatsApp: when the resolved pair carries the ``whatsapp:`` prefix, the send is
a freeform ``Body``, which Twilio only accepts inside the 24-hour customer-service
window opened by the human's last inbound message; outside the window Twilio
rejects the send with error 63016, which surfaces here as a loud
``ChannelDeliveryError``. Approved-template (``ContentSid``) sends are not
implemented — SMS is the default path.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from tai_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification

from tai_channel_twilio.client import send_message
from tai_channel_twilio.correlation import release_pending, reserve_pending
from tai_channel_twilio.settings import TwilioSettings, require_delivery_setting, twilio_settings

# Tier-1 answer formats resolve via the callback link, not an SMS reply.
_TIER1_FORMATS = frozenset({"confirm", "external"})


def _render_question(delivery: ChannelDelivery) -> str:
    """The SMS body for a Tier-2 typed-reply ask (``text`` or ``select``):
    the question, plus numbered options for a select ask.

    The reply's text becomes the value inside the ``{"answer": …}`` envelope
    forwarded to the interactions callback door; format validation stays
    server-side at that door, so the rendering only has to be readable.
    """
    lines = [delivery.question]
    if delivery.options:
        lines.extend(f"{index}. {option}" for index, option in enumerate(delivery.options, start=1))
        lines.append("Reply with the text of one option.")
    return "\n".join(lines)


def _render_link(delivery: ChannelDelivery) -> str:
    """The SMS body for a Tier-1 ask (``confirm`` or ``external``): the
    question plus the callback link.

    Plain URLs are tappable in SMS clients; the human answers in the browser
    through the callback door, so the reply path (correlation store + inbound
    webhook) is not involved.
    """
    return f"{delivery.question}\n\nAnswer here: {delivery.callback_url}"


def _resolve_target(settings: TwilioSettings, requested: str | None) -> str:
    """The destination "To" number for an outbound send.

    A caller-requested recipient must be on the operator's
    ``CHANNEL_TWILIO_ALLOWED_RECIPIENTS`` whitelist — an unlisted value is
    refused loudly before any network work (fail closed). No requested
    recipient means the operator-set ``CHANNEL_TWILIO_DEFAULT_RECIPIENT``,
    which is trusted without an allowlist check; neither set raises
    ``ChannelDeliveryError`` naming the env var — on the send path a missing
    setting is a delivery failure like any other.
    """
    if requested is None:
        return require_delivery_setting(settings.default_recipient, "CHANNEL_TWILIO_DEFAULT_RECIPIENT")
    if requested not in set(settings.allowed_recipients):
        raise ChannelDeliveryError(
            f"recipient {requested!r} is not on CHANNEL_TWILIO_ALLOWED_RECIPIENTS; refusing to send"
        )
    return requested


class TwilioChannel:
    """Satisfies the ``tai_contract.channels.Channel`` protocol."""

    async def deliver(self, delivery: ChannelDelivery) -> None:
        """Resolve the destination "To" number, then push the question to it.

        A caller-requested ``delivery.recipient`` must be on the operator's
        ``CHANNEL_TWILIO_ALLOWED_RECIPIENTS`` whitelist — an unlisted value is
        refused loudly before any reservation or network work (fail closed).
        No requested recipient means the operator-set
        ``CHANNEL_TWILIO_DEFAULT_RECIPIENT``, which is trusted without an
        allowlist check; neither set raises ``ChannelDeliveryError`` — every
        failure on this path, operator misconfiguration included, surfaces as
        that one type. An ask whose deadline has already passed is refused
        loudly for every answer format before any reservation or send.
        """
        settings = twilio_settings()
        from_number = require_delivery_setting(settings.from_number, "CHANNEL_TWILIO_FROM")
        target = _resolve_target(settings, delivery.recipient)

        # Never send a question whose answer budget is already spent — for
        # every format: a Tier-1 link would be dead on arrival, and a Tier-2
        # typed reply could no longer be forwarded. Checked before any
        # reservation or HTTP work.
        if math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds()) <= 0:
            raise ChannelDeliveryError(
                f"interaction {delivery.interaction_id} already timed out "
                f"(timeout_at={delivery.timeout_at.isoformat()}); nothing was sent"
            )

        if delivery.answer_format in _TIER1_FORMATS:
            # Tier-1: the human taps the link and answers via the callback
            # door directly (a confirm ask records its bool from the tap) —
            # no inbound reply exists to correlate, so no reservation is
            # written and the pair stays free.
            await send_message(to=target, from_number=from_number, body=_render_link(delivery))
            return

        # Reserve first: the reply can only exist after the send, but the send
        # response can lose the race against a fast reply's webhook — the store
        # must already hold the callback_url when that happens. This also
        # enforces one-pending-per-pair before any network cost.
        await reserve_pending(
            twilio_number=from_number,
            human_number=target,
            callback_url=delivery.callback_url,
            timeout_at=delivery.timeout_at,
        )
        try:
            await send_message(to=target, from_number=from_number, body=_render_question(delivery))
        except Exception:
            # The human never received the question — the reservation must not
            # block the pair until its TTL runs out.
            await release_pending(twilio_number=from_number, human_number=target)
            raise

    async def notify(self, notification: ChannelNotification) -> None:
        """Send one fire-and-forget message; raise ``ChannelDeliveryError`` on
        any failure.

        No reply is expected, so nothing touches the correlation store and no
        deadline applies — the message is the notification's text, sent as-is.
        A plain return means Twilio ACCEPTED the message, not that a human saw
        it; there is exactly one send attempt, no retry. The destination
        resolves through the same allowlist policy as ``deliver``: a requested
        recipient must be on ``CHANNEL_TWILIO_ALLOWED_RECIPIENTS`` (unlisted is
        refused loudly), and no recipient means the operator-set
        ``CHANNEL_TWILIO_DEFAULT_RECIPIENT`` — missing send configuration
        (default recipient, FROM number, credentials) raises
        ``ChannelDeliveryError`` like any other delivery failure.

        WhatsApp: a ``whatsapp:``-prefixed target receives a freeform ``Body``,
        which Twilio only accepts inside the 24-hour customer-service window
        opened by the human's last inbound message; outside the window Twilio
        rejects the send with error 63016, which surfaces here as a loud
        ``ChannelDeliveryError``. Approved-template (``ContentSid``) sends are
        not implemented.
        """
        settings = twilio_settings()
        from_number = require_delivery_setting(settings.from_number, "CHANNEL_TWILIO_FROM")
        target = _resolve_target(settings, notification.recipient)
        await send_message(to=target, from_number=from_number, body=notification.message)
