"""Strict, value-safe configuration loading."""

import ipaddress
import math
import os
import re
import unicodedata
from collections.abc import Container
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NoReturn, cast, overload

import yaml
from irctokens.line import build
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

EVENTS = frozenset({"pull_request", "issues", "workflow_run", "pull_request_review"})
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class ConfigError(ValueError):
    """Configuration error whose message never contains supplied values."""


@dataclass(frozen=True)
class HTTPConfig:
    host: str = "::"
    port: int = 8080
    max_body_bytes: int = 1048576
    max_inflight: int = 32


@dataclass(frozen=True)
class SASLConfig:
    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class OnConnectCommand:
    command: str
    params: tuple[str, ...] = field(repr=False)
    delay_after: float = 0


@dataclass(frozen=True)
class IRCConfig:
    host: str
    port: int = 6697
    nickname: str = "umarell"
    channels: tuple[str, ...] = ()
    ca_file: Path | None = None
    password: str | None = field(default=None, repr=False)
    sasl: SASLConfig | None = field(default=None, repr=False)
    on_connect: tuple[OnConnectCommand, ...] = field(default=(), repr=False)
    username: str | None = None
    realname: str | None = None


@dataclass(frozen=True)
class RelayConfig:
    capacity: int = 100
    expiry_seconds: float = 30
    send_interval: float = 1.0
    dedupe_capacity: int = 10000
    dedupe_seconds: float = 3600


@dataclass(frozen=True)
class RepositoryConfig:
    id: int
    channels: tuple[str, ...]
    events: frozenset[str] = EVENTS


@dataclass(frozen=True)
class HookConfig:
    secret: str = field(repr=False)
    repositories: dict[int, RepositoryConfig]


@dataclass(frozen=True)
class Config:
    http: HTTPConfig
    irc: IRCConfig
    relay: RelayConfig
    hooks: dict[str, HookConfig]


def _fail(path: str) -> NoReturn:
    raise ConfigError(f"Invalid configuration field: {path}")


class _Loader(yaml.SafeLoader):
    pass


def _mapping(loader: _Loader, node: MappingNode) -> dict[str, object]:
    result: dict[str, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            _fail("document.mapping")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_Loader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _read(path: Path, name: str) -> object:
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.load(stream, Loader=_Loader)
    except ConfigError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, RecursionError):
        _fail(name)


def _object(value: Any, allowed: Container[str], path: str) -> dict[str, Any]:
    # YAML values remain dynamic until each field is validated by its helper.
    if type(value) is not dict or any(key not in allowed for key in value):
        _fail(path)
    return value


def _string(value: object, path: str) -> str:
    if type(value) is not str or not value:
        _fail(path)
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail(path)
    return value


@overload
def _number(
    value: object, low: float, high: float, path: str, integer: Literal[True]
) -> int: ...


@overload
def _number(
    value: object, low: float, high: float, path: str, integer: bool = False
) -> int | float: ...


def _number(
    value: object, low: float, high: float, path: str, integer: bool = False
) -> int | float:
    if type(value) is not int and (integer or type(value) is not float):
        _fail(path)
    if not low <= value <= high or (type(value) is float and not math.isfinite(value)):
        _fail(path)
    return value


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        _fail(path)
    return value


def _fold(value: str) -> str:
    return value.lower().translate(str.maketrans("[]\\^", "{}|~"))


def _channels(value: object, path: str) -> tuple[str, ...]:
    if type(value) is not list or not value:
        _fail(path)
    seen: set[str] = set()
    for channel in value:
        if (
            type(channel) is not str
            or not channel.isascii()
            or not 1 < len(channel) <= 80
            or channel[0] not in "#&"
            or any(
                c.isspace() or ord(c) < 32 or ord(c) == 127 or c in ":,"
                for c in channel
            )
        ):
            _fail(path)
        folded = _fold(channel)
        if folded in seen:
            _fail(path)
        seen.add(folded)
    return tuple(cast(list[str], value))


def _credential(value: object, limit: int, path: str, whitespace: bool = False) -> str:
    value = _string(value, path)
    if len(value.encode("utf-8")) > limit or any(
        unicodedata.category(c).startswith("C") or (whitespace and c.isspace())
        for c in value
    ):
        _fail(path)
    return value


def _on_connect(value: object) -> tuple[OnConnectCommand, ...]:
    path = "irc.on_connect"
    if type(value) is not list or len(value) > 20:
        _fail(path)
    commands: list[OnConnectCommand] = []
    total_delay: float = 0
    for raw in value:
        data = _object(raw, {"command", "params", "delay_after"}, path)
        command = data.get("command")
        if type(command) is not str or not re.fullmatch(r"[A-Za-z]{1,32}", command):
            _fail(path)
        command = command.upper()
        if command in {
            "PASS",
            "NICK",
            "USER",
            "CAP",
            "AUTHENTICATE",
            "QUIT",
            "JOIN",
            "PART",
        }:
            _fail(path)
        raw_params = data.get("params", [])
        if type(raw_params) is not list or len(raw_params) > 15:
            _fail(path)
        params: list[str] = []
        for index, param in enumerate(raw_params):
            param = _string(param, path)
            if any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in param):
                _fail(path)
            if index < len(raw_params) - 1 and (
                param.startswith(":") or any(c.isspace() for c in param)
            ):
                _fail(path)
            params.append(param)
        try:
            wire = build(command, params).format().encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            _fail(path)
        if len(wire) + 2 > 512:
            _fail(path)
        delay = _number(data.get("delay_after", 0), 0, 30, path)
        total_delay += delay
        if total_delay > 60:
            _fail(path)
        commands.append(OnConnectCommand(command, tuple(params), float(delay)))
    return tuple(commands)


def load_config(config_path: Path) -> Config:
    """Load one sensitive YAML file, rejecting unsafe or ambiguous settings."""
    try:
        config_path = Path(config_path).resolve()
        if os.name == "posix" and config_path.stat().st_mode & 0o137:
            _fail("config.permissions")
    except ConfigError:
        raise
    except (OSError, ValueError, RuntimeError):
        _fail("config")
    root = _object(
        _read(config_path, "config"), {"http", "irc", "relay", "hooks"}, "config"
    )

    http = _object(root.get("http", {}), HTTPConfig.__dataclass_fields__, "http")
    host = _string(http.get("host", "::"), "http.host")
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in host):
        _fail("http.host")
    http_config = HTTPConfig(
        host,
        *[
            _number(http.get(key, default), low, high, "http." + key, True)
            for key, default, low, high in (
                ("port", 8080, 1, 65535),
                ("max_body_bytes", 1048576, 1024, 26214400),
                ("max_inflight", 32, 1, 1024),
            )
        ],
    )
    irc = _object(root.get("irc"), IRCConfig.__dataclass_fields__, "irc")
    host = _string(irc.get("host"), "irc.host")
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in host):
        _fail("irc.host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            hostname = host.encode("idna").decode("ascii").removesuffix(".")
        except UnicodeError:
            _fail("irc.host")
        if len(hostname) > 253 or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in hostname.split(".")
        ):
            _fail("irc.host")
    nickname = irc.get("nickname", "umarell")
    if type(nickname) is not str or not re.fullmatch(
        r"[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}-]{0,29}", nickname
    ):
        _fail("irc.nickname")
    username = None
    if "username" in irc:
        username = _string(irc["username"], "irc.username")
        if (
            len(username) > 32
            or not username.isascii()
            or not username.isprintable()
            or any(c.isspace() for c in username)
            or username.startswith(":")
        ):
            _fail("irc.username")
    realname = None
    if "realname" in irc:
        realname = _string(irc["realname"], "irc.realname")
        if (
            len(realname.encode("utf-8")) > 256
            or not realname.strip()
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in realname)
        ):
            _fail("irc.realname")
    channels = _channels(irc.get("channels"), "irc.channels")
    ca_file = None
    if "ca_file" in irc:
        try:
            ca_file = (
                config_path.parent / _string(irc["ca_file"], "irc.ca_file")
            ).resolve()
            if not ca_file.is_file():
                _fail("irc.ca_file")
        except (OSError, ValueError, RuntimeError):
            _fail("irc.ca_file")
    password = (
        _credential(irc["password"], 256, "irc.password", whitespace=True)
        if "password" in irc
        else None
    )
    sasl = None
    if "sasl" in irc:
        data = _object(irc["sasl"], {"username", "password"}, "irc.sasl")
        sasl = SASLConfig(
            _credential(data.get("username"), 128, "irc.sasl.username"),
            _credential(data.get("password"), 256, "irc.sasl.password"),
        )
    irc_config = IRCConfig(
        host,
        _number(irc.get("port", 6697), 1, 65535, "irc.port", True),
        nickname,
        channels,
        ca_file,
        password,
        sasl,
        _on_connect(irc.get("on_connect", [])),
        username=username,
        realname=realname,
    )
    relay = _object(root.get("relay", {}), RelayConfig.__dataclass_fields__, "relay")
    relay_config = RelayConfig(
        _number(relay.get("capacity", 100), 1, 10000, "relay.capacity", True),
        _number(relay.get("expiry_seconds", 30), 0.1, 3600, "relay.expiry_seconds"),
        _number(relay.get("send_interval", 1.0), 0.1, 60, "relay.send_interval"),
        _number(
            relay.get("dedupe_capacity", 10000),
            1,
            100000,
            "relay.dedupe_capacity",
            True,
        ),
        _number(relay.get("dedupe_seconds", 3600), 1, 86400, "relay.dedupe_seconds"),
    )
    raw_hooks = root.get("hooks")
    if type(raw_hooks) is not dict or not raw_hooks:
        _fail("hooks")
    hooks: dict[str, HookConfig] = {}
    for key, raw_hook in raw_hooks.items():
        _identifier(key, "hooks.id")
        hook = _object(raw_hook, {"secret", "repositories"}, "hooks.hook")
        secret = _string(hook.get("secret"), "hooks.secret")
        if len(secret.encode("utf-8")) > 4096:
            _fail("hooks.secret")
        raw_repositories = hook.get("repositories")
        if type(raw_repositories) is not list or not raw_repositories:
            _fail("hooks.repositories")
        repositories: dict[int, RepositoryConfig] = {}
        for raw_repo in raw_repositories:
            repo = _object(raw_repo, {"id", "channels", "events"}, "hooks.repositories")
            repo_id = _number(
                repo.get("id"), 1, math.inf, "hooks.repositories.id", True
            )
            if repo_id in repositories:
                _fail("hooks.repositories.id")
            destinations = _channels(
                repo.get("channels"), "hooks.repositories.channels"
            )
            if any(channel not in channels for channel in destinations):
                _fail("hooks.repositories.channels")
            events = repo.get("events", list(EVENTS))
            if type(events) is not list or any(
                type(event) is not str or event not in EVENTS for event in events
            ):
                _fail("hooks.repositories.events")
            repositories[repo_id] = RepositoryConfig(
                repo_id, destinations, frozenset(cast(list[str], events))
            )
        hooks[key] = HookConfig(secret, repositories)
    return Config(http_config, irc_config, relay_config, hooks)
