"""Authenticated webhook ingress and service lifecycle."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import signal
from collections.abc import Awaitable, Callable
from typing import NoReturn

from aiohttp import web

from .config import Config
from .events import InvalidEvent, normalize
from .irc import IRCBot
from .relay import Relay

SIGNATURE = re.compile(r"sha256=[0-9a-fA-F]{64}\Z", re.ASCII)
EVENT = re.compile(r"[A-Za-z0-9_]{1,64}\Z", re.ASCII)
DELIVERY = re.compile(r"[!-~]{1,128}\Z", re.ASCII)


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError("Invalid JSON constant")


def create_app(config: Config, relay: Relay) -> web.Application:
    inflight = 0

    @web.middleware
    async def admission(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        nonlocal inflight
        if inflight >= config.http.max_inflight:
            return web.Response(status=503)
        inflight += 1
        try:
            return await handler(request)
        except web.HTTPException as error:
            headers = (
                {"Allow": error.headers["Allow"]} if "Allow" in error.headers else {}
            )
            return web.Response(status=error.status, headers=headers)
        except Exception:
            logging.error("Webhook processing failed")
            return web.Response(status=500)
        finally:
            inflight -= 1

    async def github(request: web.Request) -> web.Response:
        hook_key = request.match_info["hook_key"]
        hook = config.hooks.get(hook_key)
        if hook is None:
            raise web.HTTPNotFound()
        if request.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise web.HTTPUnsupportedMediaType()
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        body = await request.read()
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = (
            "sha256="
            + hmac.new(hook.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        )
        if not SIGNATURE.fullmatch(signature) or not hmac.compare_digest(
            expected, signature.lower()
        ):
            raise web.HTTPForbidden()
        event = request.headers.get("X-GitHub-Event", "")
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if not EVENT.fullmatch(event) or not DELIVERY.fullmatch(delivery):
            raise web.HTTPBadRequest()
        try:
            payload = json.loads(body, parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise web.HTTPBadRequest() from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest()
        if event == "ping":
            return web.Response(status=200)
        repository = payload.get("repository")
        repo_id = repository.get("id") if isinstance(repository, dict) else None
        if type(repo_id) is not int or repo_id <= 0:
            raise web.HTTPBadRequest()
        route = hook.repositories.get(repo_id)
        if route is None:
            raise web.HTTPForbidden()
        try:
            announcement = normalize(event, payload) if event in route.events else None
        except InvalidEvent:
            raise web.HTTPBadRequest() from None
        if relay.duplicate(hook_key, delivery):
            return web.Response(status=204)
        count = relay.offer(announcement, route.channels) if announcement else 0
        return web.Response(status=202 if count else 204)

    app = web.Application(
        client_max_size=config.http.max_body_bytes,
        middlewares=[admission],
        handler_args={"auto_decompress": False},
    )
    app.router.add_post("/webhooks/github/{hook_key}", github)
    return app


async def serve(config: Config, *, stop: asyncio.Event | None = None) -> None:
    relay = Relay(config.relay)
    bot = IRCBot(config.irc, relay)
    relay.sender = bot.send_announcement
    runner = web.AppRunner(create_app(config, relay), access_log=None)
    tasks: list[asyncio.Task[None] | asyncio.Task[bool]] = []
    stop = stop if stop is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = []
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
            signals.append(sig)
        await runner.setup()
        await web.TCPSite(runner, config.http.host, config.http.port).start()
        tasks = [asyncio.create_task(relay.run()), asyncio.create_task(bot.run())]
        stopping = asyncio.create_task(stop.wait())
        tasks.append(stopping)
        logging.info("umarell started")
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if stopping not in done:
            raise RuntimeError("Service worker stopped unexpectedly")
    finally:
        relay.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        for sig in signals:
            loop.remove_signal_handler(sig)
