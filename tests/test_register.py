"""Registration: importing the register module fires the channel + route side-effects;
importing the bare package registers nothing (library-safe)."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

from tai42_contract.channels import Channel

import tai42_channel_twilio
from tai42_channel_twilio import TwilioChannel


def test_importing_register_registers_channel_and_route(stub_app):
    sys.modules.pop("tai42_channel_twilio.register", None)
    sys.modules.pop("tai42_channel_twilio.inbound", None)
    stub_app.channels.registered.clear()
    stub_app.http.routes.clear()

    importlib.import_module("tai42_channel_twilio.register")

    assert list(stub_app.channels.registered) == ["twilio"]
    assert isinstance(stub_app.channels.registered["twilio"], TwilioChannel)
    paths = {route.path for route in stub_app.http.routes}
    assert paths == {"/api/channels/twilio/inbound", "/api/channels/twilio/status"}
    assert len(stub_app.http.routes) == 2
    assert all(route.methods == ["POST"] and route.authed is False for route in stub_app.http.routes)


def test_bare_package_import_does_not_register():
    # `import tai42_channel_twilio` (library use) must not touch the app handle;
    # only the register module carries the side-effect. Checked in a clean
    # subprocess (no stub app bound, no CHANNEL_TWILIO_* env) so the in-process
    # module cache cannot mask it.
    code = "import sys; import tai42_channel_twilio; assert 'tai42_channel_twilio.register' not in sys.modules"
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHANNEL_TWILIO_")}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_twilio_channel_satisfies_the_channel_protocol():
    assert isinstance(TwilioChannel(), Channel)


def test_package_exports():
    assert tai42_channel_twilio.__all__ == ["TwilioChannel", "TwilioSettings", "twilio_settings"]
    for name in tai42_channel_twilio.__all__:
        assert getattr(tai42_channel_twilio, name) is not None
