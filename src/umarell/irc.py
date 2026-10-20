"""ircrobots integration using the library's native I/O loops."""

import asyncio
import contextlib
import logging
import ssl

from ircrobots.bot import Bot
from ircrobots.interface import ITCPReader, ITCPTransport, ITCPWriter
from ircrobots.ircv3 import CAP_SASL
from ircrobots.params import ConnectionParams, SASLParams, SASLUserPass
from ircrobots.sasl import SASLContext, SASLResult, SASLUnknownMechanismError
from ircrobots.security import TLS
from ircrobots.server import Server
from ircrobots.transport import TCPReader, TCPWriter
from ircstates.emit import Emit
from ircstates.server import ServerDisconnectedException
from irctokens.line import Line, build

from .config import IRCConfig
from .relay import Pending, Relay

LOG = logging.getLogger(__name__)
SETUP_TIMEOUT = 90
QUIT_TIMEOUT = 2


class ConnectionReader(TCPReader):
    """Own transport-read helpers so connection teardown can await them."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        super().__init__(reader)
        self.tasks: set[asyncio.Task[object]] = set()

    def _finished(self, task: asyncio.Task[object]) -> None:
        self.tasks.discard(task)
        # The native parent still observes errors through task.result(). This
        # also retrieves errors if its parent is cancelled during teardown.
        if not task.cancelled():
            task.exception()

    async def read(self, byte_count: int) -> bytes:
        task = asyncio.current_task()
        assert task is not None
        if task not in self.tasks:
            self.tasks.add(task)
            task.add_done_callback(self._finished)
        return await super().read(byte_count)

    async def close(self) -> None:
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class VerifiedTransport(ITCPTransport):
    def __init__(self, context: ssl.SSLContext) -> None:
        self.context = context
        self.reader: ConnectionReader | None = None

    async def connect(
        self,
        hostname: str,
        port: int,
        tls: TLS | None,
        bindhost: str | None = None,
    ) -> tuple[ITCPReader, ITCPWriter]:
        reader, writer = await asyncio.open_connection(
            hostname,
            port,
            ssl=self.context,
            server_hostname=hostname,
            local_addr=(bindhost, 0) if bindhost else None,
        )
        self.reader = ConnectionReader(reader)
        return self.reader, TCPWriter(writer)


class RelayServer(Server):
    def __init__(self, bot: "IRCBot", name: str) -> None:
        super().__init__(bot, name)
        self.owner = bot
        self.relay = bot.relay
        self.config = bot.config
        self.registration = asyncio.Event()
        self.setup_complete = False
        self.connection_ended = asyncio.Event()

    def set_throttle(self, rate: int, time: float) -> None:
        pass

    @property
    def connection_writer(self) -> ITCPWriter | None:
        """Upstream creates _writer on connect and clears it on disconnect."""
        return getattr(self, "_writer", None)

    async def sasl_auth(self, params: SASLParams) -> bool:
        if not isinstance(params, SASLUserPass):
            raise TypeError("Expected configured PLAIN SASL parameters")
        if not self.cap_agreed(CAP_SASL):
            raise ConnectionError("Required IRC authentication unavailable")
        try:
            self.sasl_state = await SASLContext(self).plain(
                params.username, params.password
            )
        except SASLUnknownMechanismError:
            raise ConnectionError("Required IRC authentication unavailable") from None
        if self.sasl_state != SASLResult.SUCCESS:
            raise ConnectionError("Required IRC authentication failed")
        return True

    async def _on_read(self, line: Line, emit: Emit | None) -> None:
        # Resuming an old session conflicts with the no-catch-up policy.
        self.available_caps.pop("draft/resume-0.5", None)
        await super()._on_read(line, emit)

    def _configured_channel(self, name: str) -> str | None:
        return next(
            (c for c in self.config.channels if self.casefold_equals(c, name)), None
        )

    async def line_read(self, line: Line) -> None:
        if self.owner.current is not self:
            return
        if line.command == "001":
            if self.config.sasl and self.sasl_state != SASLResult.SUCCESS:
                raise ConnectionError("Required IRC authentication unavailable")
            # A session owns one setup task; repeated welcomes cannot rerun it.
            self.registration.set()
        elif line.command in {"JOIN", "PART"} and line.source and line.params:
            if self.casefold_equals(line.hostmask.nickname, self.nickname):
                channel = self._configured_channel(line.params[0])
                if channel:
                    if line.command == "JOIN" and self.setup_complete:
                        self.relay.channel_ready(channel)
                    elif line.command == "PART":
                        self.relay.channel_lost(channel)
        elif line.command == "KICK" and len(line.params) >= 2:
            if self.casefold_equals(line.params[1], self.nickname):
                channel = self._configured_channel(line.params[0])
                if channel:
                    self.relay.channel_lost(channel)
        elif line.command == "ERROR":
            raise ConnectionError("IRC server closed connection")

    async def _setup_connection(self) -> None:
        await self.registration.wait()
        # This task is separate from the reader: delays must not hold up PONGs,
        # service responses, or disconnect detection.
        async with asyncio.timeout(SETUP_TIMEOUT):
            for command in self.config.on_connect:
                await asyncio.shield(
                    self.send(build(command.command, list(command.params)))
                )
                if command.delay_after:
                    await asyncio.sleep(command.delay_after)
            self.setup_complete = True
            for channel in self.config.channels:
                await asyncio.shield(self.send(build("JOIN", [channel])))
        # Successful setup must not trigger FIRST_COMPLETED session teardown.
        await asyncio.Future()

    async def announce(self, item: Pending) -> None:
        if self.owner.current is not self or not self.relay.valid(item):
            return
        line = build("PRIVMSG", [item.channel, item.text])
        future = asyncio.ensure_future(self.send(line))
        ended = asyncio.create_task(self.connection_ended.wait())
        try:
            done, _ = await asyncio.wait(
                (future, ended), return_when=asyncio.FIRST_COMPLETED
            )
            if future in done:
                future.result()
        finally:
            # Leave the library's send future untouched, including when it has
            # already been dequeued into a native batch. Disconnect releases
            # this application waiter even if that future never completes.
            ended.cancel()
            await asyncio.gather(ended, return_exceptions=True)


class IRCBot(Bot):
    def __init__(self, config: IRCConfig, relay: Relay) -> None:
        super().__init__()
        self.config = config
        self.relay = relay
        self.current: RelayServer | None = None
        context = ssl.create_default_context()
        if config.ca_file:
            context.load_verify_locations(cafile=config.ca_file)
        self.transport = VerifiedTransport(context)

    def create_server(self, name: str) -> RelayServer:
        return RelayServer(self, name)

    async def send_announcement(self, item: Pending) -> None:
        server = self.current
        if server and self.relay.valid(item):
            await server.announce(item)

    def parameters(self) -> ConnectionParams:
        sasl = self.config.sasl
        return ConnectionParams(
            self.config.nickname,
            self.config.host,
            self.config.port,
            username=self.config.username,
            realname=self.config.realname,
            password=self.config.password,
            sasl=SASLUserPass(sasl.username, sasl.password) if sasl else None,
            autojoin=[],
        )

    async def _watch_registration(self, server: RelayServer) -> None:
        async with asyncio.timeout(60):
            await server.registration.wait()
        # Successful registration disarms the watchdog, not the connection.
        await asyncio.Future()

    async def _session(self, server: RelayServer) -> None:
        async with asyncio.timeout(30):
            await server.connect(self.transport, self.parameters())
        tasks = [
            asyncio.create_task(server._read_lines()),
            asyncio.create_task(server._send_lines()),
            asyncio.create_task(self._watch_registration(server)),
            asyncio.create_task(server._setup_connection()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # Inspect every completion: don't hide a programming failure behind
            # a simultaneous recoverable disconnect.
            errors = [t.exception() for t in done if not t.cancelled()]
            for error in errors:
                if error and not isinstance(
                    error, (OSError, TimeoutError, ServerDisconnectedException)
                ):
                    raise error
            for error in errors:
                if error:
                    raise error
            raise ConnectionError("IRC worker ended unexpectedly")
        finally:
            self.relay.disconnect()
            server.connection_ended.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # ircrobots 0.7.2 leaves its Event.wait helper pending when the
            # parent reader is cancelled. Wake it, and await transport readers,
            # without replacing either library I/O loop.
            server._wait_for.set()
            if self.transport.reader is not None:
                await self.transport.reader.close()
            await asyncio.sleep(0)

    async def run(self) -> None:
        delay = 1
        while not self.relay.closed:
            self.relay.begin_connection()
            server = self.create_server("team")
            self.current = server
            try:
                await self._session(server)
            except (OSError, TimeoutError, ServerDisconnectedException):
                LOG.warning(
                    "IRC connection unavailable; pending announcements discarded"
                )
            finally:
                self.relay.disconnect()
                self.current = None
                server.connection_ended.set()
                if server.connection_writer is not None:
                    try:
                        # Session workers have stopped, so no native sender can
                        # race this final write. Its queue is deliberately not
                        # flushed: pending announcements must stay discarded.
                        with contextlib.suppress(OSError, TimeoutError):
                            async with asyncio.timeout(QUIT_TIMEOUT):
                                server.connection_writer.write(
                                    b"QUIT :Connection closing\r\n"
                                )
                                await server.connection_writer.drain()
                    finally:
                        with contextlib.suppress(OSError, TimeoutError):
                            async with asyncio.timeout(5):
                                await server.disconnect()
            if server.registration.is_set():
                delay = 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
