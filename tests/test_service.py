"""Service ownership of HTTP resources and background task termination."""

import asyncio
from dataclasses import replace

import pytest
from aiohttp import ClientSession, web

from umarell import web as service
from umarell.config import Config, HTTPConfig, IRCConfig, RelayConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "relay", "irc"])
@pytest.mark.parametrize("host", ["::", "0.0.0.0", "::1", "127.0.0.1"])
async def test_service_cleans_up_on_stop_or_worker_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str | None, host: str
) -> None:
    config = Config(
        HTTPConfig(host=host, port=8080),
        IRCConfig("unused.example"),
        RelayConfig(),
        {},
    )
    stop = asyncio.Event()
    release_failure = asyncio.Event()
    started = {name: asyncio.Event() for name in ("relay", "irc", "http")}
    finished = set()
    worker_tasks = set()
    relays = []
    sites = []

    class FakeRelay:
        def __init__(self, config):
            self.closed = False
            relays.append(self)

        async def run(self):
            worker_tasks.add(asyncio.current_task())
            started["relay"].set()
            try:
                if failure == "relay":
                    await release_failure.wait()
                    raise RuntimeError("private worker detail")
                await asyncio.Event().wait()
            finally:
                finished.add("relay")

        def close(self):
            self.closed = True

    class FakeIRC:
        def __init__(self, *args):
            pass

        async def send_announcement(self, item):
            pass

        async def run(self):
            worker_tasks.add(asyncio.current_task())
            started["irc"].set()
            try:
                if failure == "irc":
                    await release_failure.wait()
                    raise RuntimeError("private worker detail")
                await asyncio.Event().wait()
            finally:
                finished.add("irc")

    class LocalSite(web.TCPSite):
        def __init__(self, runner, host, port):
            assert host == config.http.host
            assert port == config.http.port
            super().__init__(runner, host, 0)
            sites.append(self)

        async def start(self):
            await super().start()
            started["http"].set()

    monkeypatch.setattr(service, "Relay", FakeRelay)
    monkeypatch.setattr(service, "IRCBot", FakeIRC)
    monkeypatch.setattr(service.web, "TCPSite", LocalSite)
    task = asyncio.create_task(service.serve(config, stop=stop), name="test-service")
    address = None
    try:
        for ready in started.values():
            await asyncio.wait_for(ready.wait(), 2)
        address = sites[0]._server.sockets[0].getsockname()
        assert address[0] == host
        client_host = "::1" if ":" in host else "127.0.0.1"
        url_host = f"[{client_host}]" if ":" in client_host else client_host
        async with ClientSession() as client:
            async with client.get(f"http://{url_host}:{address[1]}/") as response:
                assert response.status == 404
        if failure:
            release_failure.set()
            with pytest.raises(RuntimeError, match="private worker detail"):
                await asyncio.wait_for(task, 2)
        else:
            stop.set()
            await asyncio.wait_for(task, 2)
        assert relays[0].closed
        assert finished == {"relay", "irc"}
        assert all(worker.done() for worker in worker_tasks)
        assert not sites[0]._server.is_serving()
        with pytest.raises(OSError):
            await asyncio.open_connection(client_host, address[1])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_service_bind_failure_closes_relay(monkeypatch):
    instances = []

    class FakeRelay:
        def __init__(self, config):
            self.closed = False
            instances.append(self)

        def close(self):
            self.closed = True

    class FakeIRC:
        def __init__(self, *args):
            pass

        async def send_announcement(self, item):
            pass

    monkeypatch.setattr(service, "Relay", FakeRelay)
    monkeypatch.setattr(service, "IRCBot", FakeIRC)
    listener = await asyncio.start_server(
        lambda reader, writer: writer.close(), "127.0.0.1", 0
    )
    try:
        port = listener.sockets[0].getsockname()[1]
        config = Config(
            replace(HTTPConfig(), host="127.0.0.1", port=port),
            IRCConfig("unused.example"),
            RelayConfig(),
            {},
        )
        with pytest.raises(OSError):
            await service.serve(config, stop=asyncio.Event())
        assert instances[0].closed
    finally:
        listener.close()
        await listener.wait_closed()
