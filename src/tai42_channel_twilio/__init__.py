"""Twilio SMS/WhatsApp channel plugin.

A ``tai42_contract.channels.Channel`` that delivers ``ask_user`` questions over
the Twilio Messages API and bridges replies back through its own webhook route.
Importing this package does NOT register anything (library use); the runtime
imports ``tai42_channel_twilio.register`` to register the ``"twilio"`` channel
and its inbound route.
"""

from tai42_channel_twilio.channel import TwilioChannel
from tai42_channel_twilio.settings import TwilioSettings, twilio_settings

__all__ = ["TwilioChannel", "TwilioSettings", "twilio_settings"]
