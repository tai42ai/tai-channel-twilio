"""Bind a light stub app before the plugin is imported.

``tai_channel_twilio.register`` calls ``tai_app.channels.register`` and
``tai_channel_twilio.inbound`` calls ``tai_app.http.custom_route`` at import time,
and both send paths reach their clients via ``tai_app.clients.client_ctx``.
Binding a stub app here — at collection time, before any test module imports the
plugin — satisfies all three without standing up the real runtime.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from starlette.requests import Request
from tai_contract.app import tai_app
from tai_contract.channels import ChannelDelivery
from tai_kit.clients.impl.http import HttpxClient
from tai_kit.clients.impl.redis import RedisClient
from tai_kit.settings import reset_all_settings


class _ClientCtx:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubClients:
    def __init__(self) -> None:
        self.by_class: dict[type, Any] = {}
        self.ctx_kwargs: list[dict[str, Any]] = []

    def client_ctx(self, client_cls: type, settings: Any = None, **kwargs: Any) -> _ClientCtx:
        self.ctx_kwargs.append({"client_cls": client_cls, "settings": settings, **kwargs})
        if client_cls not in self.by_class:
            raise RuntimeError(f"test must set a stub client for {client_cls.__name__}")
        return _ClientCtx(self.by_class[client_cls])


class _StubChannels:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def register(self, name: str, channel: Any) -> None:
        if name in self.registered:
            raise ValueError(f"channel {name!r} already registered")
        self.registered[name] = channel


class _StubHttp:
    def __init__(self) -> None:
        self.routes: list[SimpleNamespace] = []

    def custom_route(
        self,
        path: str,
        methods: list[str],
        name: str | None = None,
        include_in_schema: bool = True,
        *,
        summary: str,
        tags: list[str],
        response_model: Any,
        request_model: Any = None,
        authed: bool = True,
    ) -> Callable[[Any], Any]:
        def decorator(fn: Any) -> Any:
            self.routes.append(
                SimpleNamespace(path=path, methods=methods, authed=authed, summary=summary, tags=tags, handler=fn)
            )
            return fn

        return decorator


class _StubApp:
    def __init__(self) -> None:
        self.channels = _StubChannels()
        self.clients = _StubClients()
        self.http = _StubHttp()


_stub_app = _StubApp()
tai_app.bind(_stub_app)


class FakeHttpx:
    """Records every ``post`` call and replays queued responses (or raises queued exceptions)."""

    def __init__(self, events: list[tuple[str, str]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: list[httpx.Response | Exception] = []
        self.events = events if events is not None else []

    async def post(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        json: Any = None,
        auth: tuple[str, str] | None = None,
    ) -> httpx.Response:
        self.events.append(("http_post", url))
        self.calls.append({"url": url, "data": data, "json": json, "auth": auth})
        if not self.responses:
            raise AssertionError("FakeHttpx: no queued response for post")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeRedis:
    """In-memory stand-in for the async redis commands the correlation store uses."""

    def __init__(self, events: list[tuple[str, str]] | None = None) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.events = events if events is not None else []

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self.events.append(("redis_set", key))
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def getdel(self, key: str) -> str | None:
        self.ttls.pop(key, None)
        return self.store.pop(key, None)

    async def delete(self, key: str) -> int:
        self.ttls.pop(key, None)
        return 0 if self.store.pop(key, None) is None else 1

    async def exists(self, key: str) -> int:
        return 1 if key in self.store else 0


@pytest.fixture
def stub_app() -> Iterator[_StubApp]:
    yield _stub_app
    _stub_app.clients.by_class.clear()
    _stub_app.clients.ctx_kwargs.clear()


_ENV_VARS = (
    "CHANNEL_TWILIO_ACCOUNT_SID",
    "CHANNEL_TWILIO_AUTH_TOKEN",
    "CHANNEL_TWILIO_FROM",
    "CHANNEL_TWILIO_ALLOWED_RECIPIENTS",
    "CHANNEL_TWILIO_DEFAULT_RECIPIENT",
    "CHANNEL_TWILIO_API_BASE_URL",
    "CHANNEL_TWILIO_REDIS_URL",
    "CHANNEL_TWILIO_HTTP_TIMEOUT_SECONDS",
    "CHANNEL_TWILIO_DEDUPE_TTL",
)


@pytest.fixture
def twilio_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CHANNEL_TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("CHANNEL_TWILIO_AUTH_TOKEN", "testtoken")
    monkeypatch.setenv("CHANNEL_TWILIO_FROM", "+15550000001")
    # The default recipient is deliberately NOT on the allowlist: the allowlist
    # gates only caller-requested recipients, never the operator's default.
    monkeypatch.setenv("CHANNEL_TWILIO_DEFAULT_RECIPIENT", "+15550000002")
    monkeypatch.setenv("CHANNEL_TWILIO_ALLOWED_RECIPIENTS", "+15550000003,whatsapp:+15550000004")
    monkeypatch.setenv("CHANNEL_TWILIO_REDIS_URL", "redis://test/0")
    # The cached settings singletons must never leak a previous test's env.
    reset_all_settings()
    yield
    reset_all_settings()


@pytest.fixture
def no_twilio_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ``CHANNEL_TWILIO_*`` env at all, settings caches reset before and
    after — the unconfigured/fail-closed branches."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reset_all_settings()
    try:
        yield
    finally:
        reset_all_settings()


@pytest.fixture
def fake_httpx(stub_app: _StubApp) -> FakeHttpx:
    fake = FakeHttpx()
    stub_app.clients.by_class[HttpxClient] = fake
    return fake


@pytest.fixture
def fake_redis(stub_app: _StubApp, fake_httpx: FakeHttpx) -> FakeRedis:
    # Share one event log with the HTTP fake so ordering across the two
    # backends (reserve-before-send) is assertable.
    fake = FakeRedis(events=fake_httpx.events)
    stub_app.clients.by_class[RedisClient] = fake
    return fake


def make_delivery(**overrides: Any) -> ChannelDelivery:
    """A valid ``ChannelDelivery`` with a tz-aware deadline a few minutes ahead."""
    fields: dict[str, Any] = {
        "interaction_id": "int-1",
        "question": "Deploy to prod?",
        "answer_format": "text",
        "options": None,
        "callback_url": "https://app.example/api/interactions/callback/ticket-1",
        "timeout_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    fields.update(overrides)
    return ChannelDelivery(**fields)


def compute_signature(token: str, url: str, pairs: list[tuple[str, str]]) -> str:
    """Twilio's documented signature: base64(HMAC-SHA1(token, url + sorted name+value pairs))."""
    payload = url + "".join(name + value for name, value in sorted(pairs))
    return base64.b64encode(hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()).decode(
        "ascii"
    )


def build_request(
    path: str = "/api/channels/twilio/inbound",
    *,
    body: bytes,
    headers: dict[str, str],
    query: bytes = b"",
    scheme: str = "http",
    chunks: list[bytes] | None = None,
) -> Request:
    """A Starlette ``Request`` over a minimal ASGI scope with a streamed body.

    ``chunks`` overrides the single-message body stream (for the bounded-read
    test, which needs the body delivered in pieces).
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query,
        "scheme": scheme,
        "server": ("app-internal", 8000),
        "client": ("203.0.113.7", 4711),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
    }
    messages = [{"type": "http.request", "body": part, "more_body": True} for part in (chunks or [body])]
    messages[-1]["more_body"] = False
    queue = list(messages)

    async def receive() -> dict[str, Any]:
        return queue.pop(0)

    return Request(scope, receive)


def signed_request(
    pairs: list[tuple[str, str]],
    token: str = "testtoken",
    *,
    path: str = "/api/channels/twilio/inbound",
    proto: str = "https",
    host: str = "public.example",
    query: str = "",
    sign_url: str | None = None,
    signature: str | None = None,
    omit_signature: bool = False,
    tamper: Callable[[dict[str, str]], None] | None = None,
) -> Request:
    """A validly-signed inbound request as Twilio would send it through a proxy.

    The signature is computed over the PUBLIC url (``proto://host + path [+ ?query]``,
    overridable via ``sign_url``) while the ASGI scope carries the internal
    scheme/host — exactly the deployment shape the handler must reconstruct.
    ``tamper`` mutates the final header dict; ``signature`` substitutes a
    precomputed value; ``omit_signature`` drops the header entirely.
    """
    body = urlencode(pairs).encode("utf-8")
    url = sign_url if sign_url is not None else f"{proto}://{host}{path}" + (f"?{query}" if query else "")
    headers = {
        "host": "app-internal:8000",
        "content-type": "application/x-www-form-urlencoded",
        "x-forwarded-proto": proto,
        "x-forwarded-host": host,
    }
    if not omit_signature:
        headers["x-twilio-signature"] = signature if signature is not None else compute_signature(token, url, pairs)
    if tamper is not None:
        tamper(headers)
    return build_request(path, body=body, headers=headers, query=query.encode("ascii"))


def response(status_code: int, *, json: Any = None, text: str | None = None) -> httpx.Response:
    """A real ``httpx.Response`` (with a request attached) for the fake client queues."""
    kwargs: dict[str, Any] = {"request": httpx.Request("POST", "https://queued.example/")}
    if json is not None:
        kwargs["json"] = json
    if text is not None:
        kwargs["text"] = text
    return httpx.Response(status_code, **kwargs)
