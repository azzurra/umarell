"""Exercise signed ingress over actual loopback HTTP without external services."""

import asyncio
import gzip
import hashlib
import hmac
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp.test_utils import TestClient, TestServer

from umarell.config import (
    EVENTS,
    Config,
    HookConfig,
    HTTPConfig,
    IRCConfig,
    RelayConfig,
    RepositoryConfig,
)
from umarell.relay import Relay
from umarell.web import create_app

SECRET = "test webhook secret"


def payload():
    return {
        "repository": {"id": 7, "full_name": "team/project"},
        "action": "opened",
        "sender": {"login": "alice"},
        "pull_request": {
            "number": 3,
            "title": "Caffè 修正",
            "draft": False,
            "html_url": "https://github.com/team/project/pull/3",
        },
    }


def headers(body, event="pull_request", delivery="delivery-1"):
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
    }


@asynccontextmanager
async def ingress(
    *,
    events=EVENTS,
    channels=("#one",),
    capacity=100,
    online=True,
    max_body=1048576,
    max_inflight=32,
):
    config = Config(
        HTTPConfig(max_body_bytes=max_body, max_inflight=max_inflight),
        IRCConfig("irc.example", channels=channels),
        RelayConfig(capacity=capacity),
        {"test": HookConfig(SECRET, {7: RepositoryConfig(7, channels, events)})},
    )
    relay = Relay(config.relay)
    if online:
        relay.begin_connection()
        for channel in channels:
            relay.channel_ready(channel)
    async with TestClient(TestServer(create_app(config, relay))) as client:
        yield client, relay


async def post(
    client,
    data=None,
    *,
    raw=None,
    event="pull_request",
    delivery="delivery-1",
    override=None,
    path="/webhooks/github/test",
):
    body = (
        raw
        if raw is not None
        else json.dumps(
            payload() if data is None else data, ensure_ascii=False
        ).encode()
    )
    request_headers = headers(body, event, delivery)
    if override:
        for key, value in override.items():
            if value is None:
                request_headers.pop(key, None)
            else:
                request_headers[key] = value
    async with client.post(path, data=body, headers=request_headers) as response:
        return response.status, await response.read()


@pytest.mark.asyncio
async def test_signed_unicode_enqueue_and_duplicate():
    async with ingress() as (client, relay):
        assert await post(client) == (202, b"")
        assert len(relay.pending) == 1
        assert "Caffè 修正" in relay.pending[0].text
        assert await post(client) == (204, b"")
        assert len(relay.pending) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signature", [None, "", "sha1=" + "0" * 64, "sha256=" + "0" * 64, "sha256=oops"]
)
async def test_bad_signature(signature):
    async with ingress() as (client, relay):
        status, _ = await post(client, override={"X-Hub-Signature-256": signature})
        assert status == 403
        assert not relay.pending and not relay.seen


@pytest.mark.asyncio
async def test_signature_is_over_exact_raw_bytes():
    body = json.dumps(payload(), ensure_ascii=False).encode()
    async with ingress() as (client, relay):
        status, _ = await post(
            client,
            raw=body + b" ",
            override={"X-Hub-Signature-256": headers(body)["X-Hub-Signature-256"]},
        )
        assert status == 403
        assert not relay.pending


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b"null",
        b"true",
        b"1",
        b'"text"',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"\xff",
    ],
)
async def test_invalid_json(raw):
    async with ingress() as (client, relay):
        status, body = await post(client, raw=raw)
        assert status == 400
        assert raw not in body or len(raw) <= 1
        assert not relay.pending and not relay.seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository,status",
    [
        (None, 400),
        ({}, 400),
        ({"id": True}, 400),
        ({"id": "7"}, 400),
        ({"id": 0}, 400),
        ({"id": 8}, 403),
    ],
)
async def test_repository_authorization(repository, status):
    data = payload()
    data["repository"] = repository
    async with ingress() as (client, relay):
        assert (await post(client, data))[0] == status
        assert not relay.pending and not relay.seen


@pytest.mark.asyncio
async def test_unknown_hook():
    async with ingress() as (client, relay):
        assert (await post(client, path="/webhooks/github/missing"))[0] == 404
        assert not relay.pending


@pytest.mark.asyncio
async def test_ping_has_no_repository_and_no_dedupe():
    async with ingress() as (client, relay):
        assert await post(client, {}, event="ping") == (200, b"")
        assert not relay.seen and not relay.pending
        assert (
            await post(client, {}, event="ping", override={"X-Hub-Signature-256": None})
        )[0] == 403


@pytest.mark.asyncio
async def test_filtered_actions_are_cached_and_malformed_events_are_not():
    async with ingress() as (client, relay):
        data = {"repository": {"id": 7}, "action": "edited"}
        assert await post(client, data) == (204, b"")
        assert await post(client) == (204, b"")
        assert not relay.pending
        broken = payload()
        broken["pull_request"]["draft"] = "false"
        assert (await post(client, broken, delivery="fresh"))[0] == 400
        assert (await post(client, delivery="fresh"))[0] == 202


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event,data",
    [
        ("push", {"repository": {"id": 7}}),
        (
            "pull_request_review",
            {
                "repository": {"id": 7},
                "action": "submitted",
                "review": {"state": "commented"},
            },
        ),
    ],
)
async def test_unknown_events_and_comments(event, data):
    async with ingress() as (client, relay):
        assert await post(client, data, event=event) == (204, b"")
        assert len(relay.seen) == 1 and not relay.pending


@pytest.mark.asyncio
async def test_disabled_category_skips_subject_validation():
    async with ingress(events=frozenset()) as (client, relay):
        assert await post(client, {"repository": {"id": 7}}) == (204, b"")
        assert len(relay.seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,value",
    [
        ("X-GitHub-Event", None),
        ("X-GitHub-Event", ""),
        ("X-GitHub-Event", "x" * 65),
        ("X-GitHub-Delivery", None),
        ("X-GitHub-Delivery", ""),
        ("X-GitHub-Delivery", "has space"),
        ("X-GitHub-Delivery", "x" * 129),
    ],
)
async def test_invalid_headers(key, value):
    async with ingress() as (client, relay):
        assert (await post(client, override={key: value}))[0] == 400
        assert not relay.seen


@pytest.mark.asyncio
async def test_media_encoding_and_body_limit():
    async with ingress(max_body=1024) as (client, relay):
        assert (await post(client, raw=b"x" * 1025))[0] == 413
        assert (await post(client, override={"Content-Type": "text/plain"}))[0] == 415
        zipped = gzip.compress(json.dumps(payload()).encode())
        assert (await post(client, raw=zipped, override={"Content-Encoding": "gzip"}))[
            0
        ] == 415
        assert (await post(client, override={"Content-Encoding": "br"}))[0] == 415
        assert (await post(client, override={"Content-Encoding": "identity"}))[0] == 202
        assert len(relay.pending) == 1


@pytest.mark.asyncio
async def test_offline_is_cached():
    async with ingress(online=False) as (client, relay):
        assert await post(client) == (204, b"")
        relay.begin_connection()
        relay.channel_ready("#one")
        assert await post(client) == (204, b"")
        assert not relay.pending


@pytest.mark.asyncio
async def test_partial_fanout_and_overflow():
    async with ingress(channels=("#one", "#two"), capacity=1) as (client, relay):
        assert await post(client) == (202, b"")
        assert [item.channel for item in relay.pending] == ["#one"]
        assert await post(client, delivery="overflow") == (204, b"")
        relay.pending.clear()
        assert await post(client, delivery="overflow") == (204, b"")
        relay.channel_lost("#one")
        assert await post(client, delivery="new") == (202, b"")
        assert [item.channel for item in relay.pending] == ["#two"]


@pytest.mark.asyncio
async def test_inflight_limit_with_partial_body():
    async with ingress(max_inflight=1) as (client, relay):
        reader, writer = await asyncio.open_connection(
            client.server.host, client.server.port
        )
        try:
            writer.write(
                b"POST /webhooks/github/test HTTP/1.1\r\n"
                b"Host: localhost\r\nContent-Type: application/json\r\n"
                b"Content-Length: 100\r\n\r\n{"
            )
            await writer.drain()
            # The first response must stay pending while its body is incomplete.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(reader.read(1), timeout=0.05)
            assert (await post(client))[0] == 503
            assert not relay.pending
        finally:
            writer.close()
            await writer.wait_closed()
