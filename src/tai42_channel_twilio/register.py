"""Registration side-effects: the ``"twilio"`` channel and its inbound route.

Loaded when the manifest's ``channel_modules`` lists ``tai42_channel_twilio``.
Importing THIS module registers the channel on ``tai42_app.channels`` and (via
the ``inbound`` import) the unauthenticated webhook route; importing the package
``__init__`` alone does NOT (library use).

The Twilio number's inbound webhook is configured OUT-OF-BAND (console/REST):
point "A message comes in" at ``{public base URL}/api/channels/twilio/inbound``
(HTTP POST). The plugin never mutates Twilio account configuration.
"""

from tai42_contract.app import tai42_app

import tai42_channel_twilio.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_twilio.channel import TwilioChannel

tai42_app.channels.register("twilio", TwilioChannel())
