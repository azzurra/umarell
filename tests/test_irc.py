import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import signal
import ssl
import subprocess
import sys
from collections.abc import AsyncGenerator, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import TracebackType

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer
from ircrobots.params import SASLUserPass
from ircrobots.server import Server
from irctokens.line import Line, tokenise

from umarell.config import (
    Config,
    HookConfig,
    HTTPConfig,
    IRCConfig,
    OnConnectCommand,
    RelayConfig,
    RepositoryConfig,
    SASLConfig,
)
from umarell.events import Announcement
from umarell.irc import IRCBot, RelayServer
from umarell.relay import Relay
from umarell.web import create_app


@pytest.mark.parametrize("method", ["_send_lines", "_read_lines"])
def test_server_inherits_library_io_loops(method: str) -> None:
    assert getattr(RelayServer, method) is getattr(Server, method)


def test_registration_does_not_replace_initial_throttle() -> None:
    bot = IRCBot(IRCConfig("irc.invalid"), Relay(RelayConfig()))
    server = bot.create_server("team")
    initial = (server.throttle.rate_limit, server.throttle.period)
    server.set_throttle(4, 2)
    assert (server.throttle.rate_limit, server.throttle.period) == initial


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("irc-tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class IRCFixture:
    def __init__(self, certificate: tuple[Path, Path], sasl: str | None = None) -> None:
        self.certificate = certificate
        self.sasl = sasl
        self.writers: list[asyncio.StreamWriter] = []
        self.handlers: set[asyncio.Task[None]] = set()
        self.messages: asyncio.Queue[Line] = asyncio.Queue()
        self.lines: list[Line] = []
        self.connection_lines: list[list[Line]] = []
        self.connections = 0
        self.auth_payload: bytes | None = None
        self.server: asyncio.Server | None = None

    async def __aenter__(self) -> "IRCFixture":
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*self.certificate)
        self.server = await asyncio.start_server(
            self.handle, "127.0.0.1", 0, ssl=context
        )
        assert self.server.sockets
        self.port: int = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()
        for writer in self.writers:
            writer.close()
        for task in self.handlers:
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)

    async def write(self, writer: asyncio.StreamWriter, text: str) -> None:
        writer.write((text + "\r\n").encode())
        await writer.drain()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.handlers.add(task)
        self.writers.append(writer)
        self.connections += 1
        connection_lines: list[Line] = []
        self.connection_lines.append(connection_lines)
        nickname = "umarell"
        try:
            while data := await reader.readline():
                line = tokenise(data.decode().removesuffix("\r\n"))
                self.lines.append(line)
                connection_lines.append(line)
                if line.command == "NICK":
                    nickname = line.params[0]
                elif line.command == "CAP" and line.params[0] == "LS":
                    caps = (
                        "sasl=EXTERNAL"
                        if self.sasl == "unsupported"
                        else "sasl=PLAIN"
                        if self.sasl
                        else ""
                    )
                    await self.write(writer, f":irc CAP * LS :{caps}")
                elif line.command == "CAP" and line.params[0] == "REQ":
                    await self.write(writer, f":irc CAP * ACK :{line.params[-1]}")
                elif line.command == "AUTHENTICATE":
                    if line.params[0] == "PLAIN":
                        await self.write(writer, "AUTHENTICATE +")
                    else:
                        self.auth_payload = base64.b64decode(line.params[0])
                        code = "903" if self.sasl == "success" else "904"
                        await self.write(writer, f":irc {code} {nickname} :SASL result")
                elif line.command == "CAP" and line.params[0] == "END":
                    await self.write(writer, f":irc 001 {nickname} :Welcome")
                    await self.write(writer, f":irc 376 {nickname} :End MOTD")
                elif line.command == "JOIN":
                    await self.write(
                        writer, f":{nickname}!u@localhost JOIN {line.params[0]}"
                    )
                elif line.command == "WHO":
                    await self.write(
                        writer, f":irc 315 {nickname} {line.params[0]} :End WHO"
                    )
                elif line.command == "MODE" and len(line.params) == 1:
                    await self.write(
                        writer, f":irc 324 {nickname} {line.params[0]} +nt"
                    )
                elif line.command == "PRIVMSG":
                    self.messages.put_nowait(line)
                elif line.command == "PING":
                    await self.write(writer, "PONG :umarell")
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.handlers.discard(task)


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not predicate():  # noqa: ASYNC110 - observe native protocol state
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def running_bot(
    fixture: IRCFixture,
    *,
    sasl: bool = False,
    trusted: bool = True,
    host: str = "localhost",
    on_connect: tuple[OnConnectCommand, ...] = (),
    username: str | None = None,
    realname: str | None = None,
) -> AsyncGenerator[tuple[IRCBot, Relay]]:
    config = IRCConfig(
        host=host,
        port=fixture.port,
        channels=("#team",),
        ca_file=fixture.certificate[0] if trusted else None,
        sasl=SASLConfig("account", "password") if sasl else None,
        on_connect=on_connect,
        username=username,
        realname=realname,
    )
    relay = Relay(RelayConfig(send_interval=0.1))
    bot = IRCBot(config, relay)
    relay.sender = bot.send_announcement
    tasks = [asyncio.create_task(bot.run()), asyncio.create_task(relay.run())]
    try:
        yield bot, relay
    finally:
        relay.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_signed_http_to_real_tls_irc_and_reconnect(
    certificate: tuple[Path, Path],
) -> None:
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture) as (bot, relay):
            await until(lambda: "#team" in relay.ready)
            config = Config(
                HTTPConfig(),
                bot.config,
                relay.config,
                {"team": HookConfig("secret", {1: RepositoryConfig(1, ("#team",))})},
            )
            body = json.dumps(
                {
                    "action": "opened",
                    "repository": {"id": 1, "full_name": "o/r"},
                    "issue": {
                        "number": 1,
                        "title": "An issue",
                        "html_url": "https://github.com/o/r/issues/1",
                    },
                }
            ).encode()
            headers = {
                "Content-Type": "application/json",
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "one",
                "X-Hub-Signature-256": "sha256="
                + hmac.new(b"secret", body, hashlib.sha256).hexdigest(),
            }
            async with TestClient(TestServer(create_app(config, relay))) as client:
                response = await client.post(
                    "/webhooks/github/team", data=body, headers=headers
                )
                assert response.status == 202
                line = await asyncio.wait_for(fixture.messages.get(), 10)
                assert line.params[0] == "#team"
                assert "Issue #1 opened" in line.params[1]
                headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
                assert (
                    await client.post(
                        "/webhooks/github/team", data=body, headers=headers
                    )
                ).status == 403
            # Successful registration must leave the connection alive.
            relay.offer(Announcement("second", "https://github.com/o/r"), ("#team",))
            assert (
                "second"
                in (await asyncio.wait_for(fixture.messages.get(), 10)).params[1]
            )
            await fixture.write(fixture.writers[-1], "PING :check")
            await until(lambda: any(line.command == "PONG" for line in fixture.lines))
            relay.offer(Announcement("stale", "https://github.com/o/r"), ("#team",))
            old = bot.current
            fixture.writers[-1].close()
            await until(lambda: bot.current is not old)
            await until(lambda: fixture.connections >= 2 and "#team" in relay.ready)
            assert not relay.pending
            relay.offer(Announcement("fresh", "https://github.com/o/r"), ("#team",))
            async with asyncio.timeout(10):
                while True:
                    line = await fixture.messages.get()
                    assert "stale" not in line.params[1]
                    if "fresh" in line.params[1]:
                        break


@pytest.mark.parametrize("sasl", ["success", "failure", "unsupported", None])
async def test_required_sasl_gates_join_and_output(
    certificate: tuple[Path, Path], sasl: str | None
) -> None:
    async with IRCFixture(certificate, sasl) as fixture:
        async with running_bot(fixture, sasl=True) as (bot, relay):
            if sasl == "success":
                await until(lambda: "#team" in relay.ready)
                assert fixture.auth_payload == b"account\x00account\x00password"
            else:
                await until(lambda: fixture.connections >= 2)
                assert not relay.ready
                assert not any(line.command == "JOIN" for line in fixture.lines)
                assert fixture.messages.empty()


@pytest.mark.parametrize("trusted,host", [(False, "localhost"), (True, "127.0.0.1")])
async def test_tls_rejects_untrusted_or_wrong_hostname(
    certificate: tuple[Path, Path], trusted: bool, host: str
) -> None:
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture, trusted=trusted, host=host) as (_, relay):
            await asyncio.sleep(0.3)
            assert not relay.ready
            assert not fixture.lines


async def test_stale_announcements_dropped_before_library_handoff(
    certificate: tuple[Path, Path],
) -> None:
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture) as (bot, relay):
            await until(lambda: "#team" in relay.ready)
            server = bot.current
            assert server is not None
            relay.offer(Announcement("stale", "https://github.com/o/r"), ("#team",))
            item = relay.pending.popleft()
            relay.channel_lost("#team")
            relay.channel_ready("#team")
            await server.announce(item)
            assert fixture.messages.empty()
            # An expired popped item is also rejected.
            await server.announce(replace(item, deadline=0))
            assert fixture.messages.empty()


async def test_disconnect_releases_dequeued_announcement_and_relay_resumes(
    certificate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture) as (bot, relay):
            await until(lambda: "#team" in relay.ready)
            server = bot.current
            assert server is not None
            writer = server.connection_writer
            assert writer is not None
            draining = asyncio.Event()

            async def blocked_drain() -> None:
                draining.set()
                await asyncio.Future()

            monkeypatch.setattr(writer, "drain", blocked_drain)
            relay.offer(Announcement("in flight", "https://github.com/o/r"), ("#team",))
            await asyncio.wait_for(draining.wait(), 3)
            fixture.writers[0].close()
            await until(lambda: fixture.connections >= 2 and "#team" in relay.ready)
            relay.offer(Announcement("fresh", "https://github.com/o/r"), ("#team",))
            async with asyncio.timeout(3):
                while True:
                    line = await fixture.messages.get()
                    if "fresh" in line.params[1]:
                        break


async def test_shutdown_leaves_no_native_reader_helpers(
    certificate: tuple[Path, Path],
) -> None:
    before = asyncio.all_tasks()
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture) as (_, relay):
            await until(lambda: "#team" in relay.ready)
    assert not (asyncio.all_tasks() - before)


@pytest.mark.parametrize("exit_reason", ["cancel", "failure"])
@pytest.mark.parametrize("during_setup", [False, True])
async def test_connection_exit_sends_quit_before_eof(
    certificate: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    exit_reason: str,
    during_setup: bool,
) -> None:
    async with IRCFixture(certificate) as fixture:
        relay = Relay(RelayConfig())
        commands = (
            (OnConnectCommand("PRIVMSG", ("NickServ", "setup-marker"), 30),)
            if during_setup
            else ()
        )
        bot = IRCBot(
            IRCConfig(
                "localhost",
                port=fixture.port,
                channels=("#team",),
                ca_file=certificate[0],
                on_connect=commands,
            ),
            relay,
        )
        task = asyncio.create_task(bot.run())
        try:
            await until(
                lambda: (
                    bool(identifications(fixture.lines))
                    if during_setup
                    else "#team" in relay.ready
                )
            )
            if exit_reason == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:

                async def fail(line: Line) -> None:
                    raise RuntimeError("test worker failure")

                assert bot.current is not None
                monkeypatch.setattr(bot.current, "line_read", fail)
                await fixture.write(fixture.writers[0], "NOTICE umarell :trigger")
                with pytest.raises(RuntimeError, match="test worker failure"):
                    await asyncio.wait_for(task, 2)
            await until(lambda: not fixture.handlers)
            quits = [line for line in fixture.lines if line.command == "QUIT"]
            assert len(quits) == 1
            assert fixture.lines[-1] is quits[0]
            if during_setup:
                assert not any(line.command == "JOIN" for line in fixture.lines)
            assert fixture.connections == 1
        finally:
            relay.close()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("failure", ["write", "drain", "timeout"])
async def test_failed_quit_still_closes_connection(
    certificate: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from umarell import irc

    monkeypatch.setattr(irc, "QUIT_TIMEOUT", 0.02)
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture) as (bot, relay):
            await until(lambda: "#team" in relay.ready)
            assert bot.current is not None
            writer = bot.current.connection_writer
            assert writer is not None
            close = writer.close
            closed = asyncio.Event()
            attempted = asyncio.Event()

            def broken_write(data: bytes) -> None:
                attempted.set()
                raise OSError("test write failure")

            async def broken_drain() -> None:
                attempted.set()
                if failure == "timeout":
                    await asyncio.Future()
                raise OSError("test drain failure")

            async def closing() -> None:
                closed.set()
                await close()

            monkeypatch.setattr(writer, "close", closing)
            if failure == "write":
                monkeypatch.setattr(writer, "write", broken_write)
            else:
                monkeypatch.setattr(writer, "drain", broken_drain)
        # Cancellation must finish despite a failed or unresponsive QUIT write.
        assert attempted.is_set()
        assert closed.is_set()
        await until(lambda: not fixture.handlers)
        assert fixture.connections == 1


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_process_signal_sends_quit(
    certificate: tuple[Path, Path],
    tmp_path: Path,
    unused_tcp_port: int,
    sig: signal.Signals,
) -> None:
    async with IRCFixture(certificate) as fixture:
        config = tmp_path / "config.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "http": {"host": "127.0.0.1", "port": unused_tcp_port},
                    "irc": {
                        "host": "localhost",
                        "port": fixture.port,
                        "ca_file": str(certificate[0]),
                        "channels": ["#team"],
                    },
                    "hooks": {
                        "team": {
                            "secret": "test-secret",
                            "repositories": [{"id": 1, "channels": ["#team"]}],
                        }
                    },
                }
            )
        )
        config.chmod(0o600)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "umarell",
            "--config",
            str(config),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await until(lambda: any(line.command == "JOIN" for line in fixture.lines))
            process.send_signal(sig)
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == 0, stderr.decode()
            await until(lambda: not fixture.handlers)
            assert sum(line.command == "QUIT" for line in fixture.lines) == 1
            assert fixture.lines[-1].command == "QUIT"
            assert fixture.connections == 1
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


def identifications(lines: Sequence[Line]) -> list[Line]:
    return [
        line
        for line in lines
        if line.command == "PRIVMSG" and line.params[0] == "NickServ"
    ]


async def test_connect_commands_order_once_per_connection_and_reconnect(
    certificate: tuple[Path, Path],
) -> None:
    commands = (
        OnConnectCommand("PRIVMSG", ("NickServ", "IDENTIFY account private-value")),
        OnConnectCommand("MODE", ("umarell", "+B")),
        OnConnectCommand("LUSERS", ()),
    )
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture, on_connect=commands) as (_, relay):
            await until(lambda: "#team" in relay.ready)
            first = fixture.connection_lines[0]
            assert identifications(first)[0].params == [
                "NickServ",
                "IDENTIFY account private-value",
            ]
            setup_lines = [
                line.command
                for line in first
                if line.command in {"PRIVMSG", "LUSERS", "JOIN"}
                or (line.command == "MODE" and line.params == ["umarell", "+B"])
            ]
            assert setup_lines == ["PRIVMSG", "MODE", "LUSERS", "JOIN"]
            whos = sum(line.command == "WHO" for line in first)
            await fixture.write(
                fixture.writers[0], ":irc 001 umarell :Repeated welcome"
            )
            await until(lambda: sum(line.command == "WHO" for line in first) > whos)
            assert len(identifications(first)) == 1
            assert sum(line.command == "JOIN" for line in first) == 1
            fixture.writers[0].close()
            await until(lambda: fixture.connections >= 2 and "#team" in relay.ready)
            assert len(identifications(fixture.connection_lines[1])) == 1


async def test_connect_delay_keeps_reader_responsive_before_join(
    certificate: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    commands = (OnConnectCommand("PRIVMSG", ("NickServ", "IDENTIFY private-value"), 2),)
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture, on_connect=commands) as (bot, relay):
            await until(lambda: bool(identifications(fixture.lines)))
            written = asyncio.get_running_loop().time()
            assert not relay.ready
            assert bot.current is not None
            assert not bot.current.setup_complete
            await fixture.write(fixture.writers[0], "PING :during-setup")
            await until(
                lambda: any(
                    line.command == "PONG" and line.params == ["during-setup"]
                    for line in fixture.lines
                )
            )
            assert not any(line.command == "JOIN" for line in fixture.lines)

            await until(lambda: "#team" in relay.ready)
            assert asyncio.get_running_loop().time() - written >= 1.9
    assert "private-value" not in caplog.text


def test_registration_identity_is_separate_from_sasl_account() -> None:
    config = IRCConfig(
        "irc.invalid",
        nickname="publicnick",
        username="registration-user",
        realname="GitHub announcements",
        sasl=SASLConfig("authentication-account", "private-password"),
    )
    bot = IRCBot(config, Relay(RelayConfig()))
    params = bot.parameters()
    assert params.nickname == "publicnick"
    assert params.username == "registration-user"
    assert params.realname == "GitHub announcements"
    assert isinstance(params.sasl, SASLUserPass)
    assert params.sasl.username == "authentication-account"
    assert params.sasl.password == "private-password"


@pytest.mark.parametrize(
    "username,realname",
    [
        (None, None),
        ("github-user", None),
        (None, " umarell — café "),
        ("github-user", ":umarell GitHub announcements"),
    ],
)
async def test_registration_identity_on_wire_and_reconnect(
    certificate: tuple[Path, Path], username: str | None, realname: str | None
) -> None:
    expected = [username or "umarell", "0", "*", realname or "umarell"]
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture, username=username, realname=realname) as (
            _,
            relay,
        ):
            await until(lambda: "#team" in relay.ready)
            users = [
                line for line in fixture.connection_lines[0] if line.command == "USER"
            ]
            assert len(users) == 1
            assert users[0].params == expected
            if username is not None and realname is not None:
                fixture.writers[0].close()
                await until(lambda: fixture.connections >= 2 and "#team" in relay.ready)
                users = [
                    line
                    for line in fixture.connection_lines[1]
                    if line.command == "USER"
                ]
                assert len(users) == 1
                assert users[0].params == expected


async def test_disconnect_during_connect_delay_discards_old_sequence(
    certificate: tuple[Path, Path],
) -> None:
    commands = (
        OnConnectCommand("PRIVMSG", ("NickServ", "IDENTIFY private-value"), 3),
        OnConnectCommand("MODE", ("umarell", "+B")),
    )
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture, on_connect=commands) as (_, relay):
            await until(lambda: bool(identifications(fixture.lines)))
            first = fixture.connection_lines[0]
            fixture.writers[0].close()
            await until(
                lambda: (
                    fixture.connections >= 2
                    and bool(identifications(fixture.connection_lines[1]))
                )
            )
            assert not any(line.command in {"MODE", "JOIN"} for line in first)
            assert not relay.ready
            assert len(identifications(fixture.connection_lines[1])) == 1


@pytest.mark.parametrize("sasl", ["success", "failure", None])
async def test_on_connect_respects_required_sasl(
    certificate: tuple[Path, Path], sasl: str | None
) -> None:
    commands = (OnConnectCommand("PRIVMSG", ("NickServ", "setup-marker")),)
    async with IRCFixture(certificate, sasl) as fixture:
        async with running_bot(fixture, sasl=True, on_connect=commands) as (_, relay):
            if sasl == "success":
                await until(lambda: "#team" in relay.ready)
                first = fixture.connection_lines[0]
                auth = [
                    i for i, line in enumerate(first) if line.command == "AUTHENTICATE"
                ]
                command = next(
                    i for i, line in enumerate(first) if line.command == "PRIVMSG"
                )
                assert max(auth) < command
            else:
                await until(lambda: fixture.connections >= 2)
                assert not identifications(fixture.lines)
                assert not relay.ready


async def test_shutdown_cancels_setup_without_tail_or_joins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from umarell.irc import IRCBot

    commands = (
        OnConnectCommand("PRIVMSG", ("NickServ", "IDENTIFY private-value"), 30),
        OnConnectCommand("MODE", ("umarell", "+B")),
    )
    config = IRCConfig("irc.invalid", channels=("#team",), on_connect=commands)
    relay = Relay(RelayConfig())
    bot = IRCBot(config, relay)
    server = bot.create_server("test")
    sent: list[Line] = []
    first = asyncio.Event()

    def send(line: Line) -> asyncio.Future[None]:
        sent.append(line)
        first.set()
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    monkeypatch.setattr(server, "send", send)
    server.registration.set()
    task = asyncio.create_task(server._setup_connection())
    await first.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [line.command for line in sent] == ["PRIVMSG"]
    assert not server.setup_complete


async def test_setup_timeout_reconnects_without_join(
    certificate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from umarell import irc

    monkeypatch.setattr(irc, "SETUP_TIMEOUT", 0.2)
    commands = (OnConnectCommand("PRIVMSG", ("NickServ", "IDENTIFY value"), 2),)
    async with IRCFixture(certificate) as fixture:
        async with running_bot(fixture, on_connect=commands) as (_, relay):
            await until(lambda: fixture.connections >= 2)
            assert not relay.ready
            assert not any(line.command == "JOIN" for line in fixture.lines)
