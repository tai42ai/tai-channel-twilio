"""Registration side-effects: the ``"twilio"`` channel and its inbound route.

Loaded when the manifest's ``channel_modules`` lists ``tai_channel_twilio``:
the runtime imports every module under the package, and importing THIS one
registers the channel on ``tai_app.channels`` and — via the ``inbound`` import —
the unauthenticated webhook route on ``tai_app.http``. Importing the package
``__init__`` alone does NOT register (library use).

The Twilio phone number's inbound webhook is configured OUT-OF-BAND (Twilio
console or REST API): point the number's "A message comes in" URL at
``{public base URL}/api/channels/twilio/inbound`` (HTTP POST). The plugin never
mutates Twilio account configuration at startup.
"""

from tai_contract.app import tai_app

import tai_channel_twilio.inbound  # noqa: F401  (route registration side-effect)
from tai_channel_twilio.channel import TwilioChannel

tai_app.channels.register("twilio", TwilioChannel())
