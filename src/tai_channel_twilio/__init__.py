"""Twilio SMS/WhatsApp channel plugin for the TAI ecosystem.

A ``tai_contract.channels.Channel`` that delivers ``ask_user`` questions to a
human's phone over the Twilio Messages API and bridges the reply back to the
interaction callback door through its own verified webhook route. The runtime
discovers it through the manifest's ``channel_modules`` — it imports every
module under the package, and ``tai_channel_twilio.register`` fires the
registrations as a side-effect (the ``"twilio"`` channel name, the inbound
route). Importing this ``__init__`` alone does NOT register (library use).
"""

from tai_channel_twilio.channel import TwilioChannel
from tai_channel_twilio.settings import TwilioSettings, twilio_settings

__all__ = ["TwilioChannel", "TwilioSettings", "twilio_settings"]
