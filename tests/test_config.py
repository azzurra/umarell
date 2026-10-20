import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from umarell.config import Config, ConfigError, HTTPConfig, load_config

type ConfigDocuments = tuple[Path, dict[str, Any], Callable[..., Config]]


@pytest.fixture
def documents(tmp_path):
    config = tmp_path / "config.yaml"
    config.touch(mode=0o600)
    data = {
        "irc": {"host": "irc.example.org", "channels": ["#team"]},
        "hooks": {
            "github": {
                "secret": "very-private-secret",
                "repositories": [{"id": 123, "channels": ["#team"]}],
            }
        },
    }

    def load(change=None):
        if change:
            change(data)
        config.write_text(yaml.safe_dump(data))
        return load_config(config)

    return config, data, load


def test_defaults_and_redaction(documents):
    _, _, load = documents
    config = load()
    assert config.http.host == "::"
    assert config.relay.capacity == 100
    assert config.irc.channels == ("#team",)
    assert config.irc.username is None
    assert config.irc.realname is None
    assert len(config.hooks["github"].repositories[123].events) == 4
    assert "very-private-secret" not in repr(config)
    with pytest.raises(AttributeError):
        config.irc.host = "other"


def test_http_dataclass_defaults_to_ipv6_any() -> None:
    assert HTTPConfig().host == "::"


@pytest.mark.parametrize("section", [{}, {"port": 8081}])
def test_http_omitted_host_defaults_to_ipv6_any(
    documents: ConfigDocuments, section: dict[str, object]
) -> None:
    _, data, load = documents
    data["http"] = section
    assert load().http.host == "::"


@pytest.mark.parametrize(
    "host",
    [
        "::",
        "0.0.0.0",
        "192.0.2.10",
        "2001:db8::10",
        "127.0.0.1",
        "::1",
        "localhost",
        "hooks.internal",
    ],
)
def test_http_host_override_is_preserved(documents: ConfigDocuments, host: str) -> None:
    _, data, load = documents
    data["http"] = {"host": host}
    assert load().http.host == host


@pytest.mark.parametrize(
    "host", [None, False, 123, [], {}, "", " ", "bad host", "bad\nname", "bad\x00name"]
)
def test_http_host_rejects_invalid_values(
    documents: ConfigDocuments, host: object
) -> None:
    _, data, load = documents
    data["http"] = {"host": host}
    with pytest.raises(ConfigError) as error:
        load()
    assert str(error.value) == "Invalid configuration field: http.host"


@pytest.mark.parametrize(
    "values",
    [
        {"username": "github-bot"},
        {"realname": " umarell — GitHub café "},
        {"username": "github-bot", "realname": ":umarell GitHub announcements"},
    ],
)
def test_irc_identity_overrides_are_independent_and_preserved(documents, values):
    _, data, load = documents
    data["irc"].update(values)
    config = load()
    assert config.irc.username == values.get("username")
    assert config.irc.realname == values.get("realname")


@pytest.mark.parametrize("field", ["username", "realname"])
@pytest.mark.parametrize(
    "value", [None, "", False, 123, [], {}, "private\r\nUSER injected", "private\x00"]
)
def test_irc_identity_invalid_types_and_controls_are_safe(documents, field, value):
    _, data, load = documents
    data["irc"][field] = value
    with pytest.raises(ConfigError) as error:
        load()
    assert str(error.value) == f"Invalid configuration field: irc.{field}"


@pytest.mark.parametrize(
    "value",
    [" ", "two words", ":prefix", "é", "x" * 33, "bad\tuser", "bad\x7f", "bad\ud800"],
)
def test_irc_username_rejects_invalid_middle_parameter(documents, value):
    _, data, load = documents
    data["irc"]["username"] = value
    with pytest.raises(
        ConfigError, match=r"^Invalid configuration field: irc.username$"
    ):
        load()


@pytest.mark.parametrize(
    "value", ["   ", "\u2003", "bad\u200b", "bad\ud800", "x" * 257]
)
def test_irc_realname_rejects_blank_control_and_oversized_values(documents, value):
    _, data, load = documents
    data["irc"]["realname"] = value
    with pytest.raises(
        ConfigError, match=r"^Invalid configuration field: irc.realname$"
    ):
        load()


@pytest.mark.parametrize("suffix,valid", [("", True), ("x", False)])
def test_irc_identity_utf8_byte_boundaries(documents, suffix, valid):
    _, data, load = documents
    data["irc"].update(username="x" * 32, realname="é" * 128 + suffix)
    if valid:
        config = load()
        assert len(config.irc.username.encode()) == 32
        assert len(config.irc.realname.encode()) == 256
    else:
        with pytest.raises(ConfigError, match="irc.realname"):
            load()


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("http", "port", True),
        ("http", "port", 0),
        ("http", "max_body_bytes", 1),
        ("http", "max_inflight", 1025),
        ("relay", "send_interval", float("nan")),
        ("relay", "capacity", 10001),
        ("relay", "expiry_seconds", float("inf")),
        ("irc", "host", "host\nsecret"),
        ("irc", "nickname", "bad nick"),
        ("irc", "nickname", "1invalid"),
        ("irc", "channels", ["#a", "#A"]),
        ("irc", "channels", ["#a[", "#a{"]),
        ("irc", "channels", []),
        ("irc", "channels", ["#bad,channel"]),
        ("irc", "unknown", "sensitive"),
        ("irc", "password_secret", "missing"),
    ],
)
def test_invalid_fields(documents, section, key, value):
    _, _, load = documents
    with pytest.raises(ConfigError) as error:
        load(lambda data: data.setdefault(section, {}).__setitem__(key, value))
    assert "sensitive" not in str(error.value)
    assert "host\nsecret" not in str(error.value)


@pytest.mark.parametrize(
    "update",
    [
        {"id": True},
        {"id": 0},
        {"channels": ["#other"]},
        {"channels": ["#TEAM"]},
        {"events": ["push"]},
        {"events": None},
    ],
)
def test_repository_validation(documents, update):
    _, _, load = documents
    with pytest.raises(ConfigError):
        load(lambda data: data["hooks"]["github"]["repositories"][0].update(update))


def test_disabled_events(documents):
    _, _, load = documents
    config = load(
        lambda data: data["hooks"]["github"]["repositories"][0].update(events=[])
    )
    assert config.hooks["github"].repositories[123].events == frozenset()


def test_duplicate_repositories(documents):
    _, _, load = documents
    with pytest.raises(ConfigError):
        load(
            lambda data: data["hooks"]["github"]["repositories"].append(
                {"id": 123, "channels": ["#team"]}
            )
        )


def test_duplicate_yaml_and_parse_errors_are_safe(documents):
    config, _, _ = documents
    for content in [
        "irc: secret-value\nirc: other\n",
        "secret-value: [",
        "!!python/object:secret-value {}",
    ]:
        config.write_text(content)
        with pytest.raises(ConfigError) as error:
            load_config(config)
        assert "secret-value" not in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.parametrize(
    "mode", [0o600, 0o640, 0o644, 0o660, 0o604, 0o700, 0o610, 0o601]
)
def test_config_permissions(documents, mode):
    config, _, load = documents
    load()
    config.chmod(mode)
    if mode in {0o600, 0o640}:
        load()
    else:
        with pytest.raises(ConfigError, match="config.permissions"):
            load()


@pytest.mark.parametrize(
    "field,secret",
    [
        ("password", "has whitespace"),
        ("password", "x" * 257),
        ("sasl", "contains\x00nul"),
        ("sasl", "x" * 257),
    ],
)
def test_credentials(documents, field, secret):
    _, data, load = documents
    data["irc"][field] = (
        secret if field == "password" else {"username": "user", "password": secret}
    )
    with pytest.raises(ConfigError):
        load()


def test_sasl_and_ca_path(documents):
    config_path, data, load = documents
    ca = config_path.parent / "ca.pem"
    ca.write_text("placeholder")
    data["irc"].update(
        ca_file="ca.pem",
        sasl={"username": "sasl-private-account", "password": "private-password"},
    )
    config = load()
    assert config.irc.ca_file == ca
    assert config.irc.sasl.username == "sasl-private-account"
    assert "sasl-private-account" not in repr(config)


def test_on_connect_defaults_and_literals(documents):
    from irctokens.line import build

    _, data, load = documents
    assert load().irc.on_connect == ()
    data["irc"]["on_connect"] = []
    assert load().irc.on_connect == ()
    data["irc"]["on_connect"] = [
        {
            "command": "privmsg",
            "params": ["NickServ", "password"],
            "delay_after": 2,
        },
        {"command": "notice", "params": ["target", ":literal private value"]},
        {"command": "LIST"},
    ]
    config = load()
    first, second, third = config.irc.on_connect
    assert (first.command, first.params, first.delay_after) == (
        "PRIVMSG",
        ("NickServ", "password"),
        2.0,
    )
    assert (
        build(second.command, list(second.params)).format()
        == "NOTICE target ::literal private value"
    )
    assert third.params == ()
    assert third.delay_after == 0
    for value in (config, config.irc, first, second):
        assert "password" not in repr(value)
        assert "literal private value" not in repr(value)


@pytest.mark.parametrize(
    "value", [None, {}, "private", 1, [None], [{"command": "LIST", "private": True}]]
)
def test_on_connect_invalid_container(documents, value):
    _, data, load = documents
    data["irc"]["on_connect"] = value
    with pytest.raises(
        ConfigError, match=r"^Invalid configuration field: irc.on_connect$"
    ):
        load()


@pytest.mark.parametrize(
    "command",
    [
        None,
        1,
        "",
        "/msg",
        "PRIV MSG",
        "A" * 33,
        "é",
        "123",
        "PRIVMSG\nprivate",
        "pass",
        "Nick",
        "USER",
        "CAP",
        "AUTHENTICATE",
        "QUIT",
        "JOIN",
        "PART",
    ],
)
def test_on_connect_invalid_commands(documents, command):
    _, data, load = documents
    data["irc"]["on_connect"] = [{"command": command}]
    with pytest.raises(
        ConfigError, match=r"^Invalid configuration field: irc.on_connect$"
    ):
        load()


@pytest.mark.parametrize(
    "params",
    [
        None,
        "private",
        {},
        [""],
        [False],
        [1],
        [{}],
        [{"secret": "missing-private"}],
        [{"secret": "irc", "extra": "private"}],
        [{"secret": False}],
        ["x"] * 16,
        ["bad space", "final"],
        [":bad", "final"],
        ["bad\u2003space", "final"],
        ["private\rJOIN #bad"],
        ["private\nJOIN #bad"],
        ["private\x00"],
        ["private\x7f"],
        ["private\u200b"],
    ],
)
def test_on_connect_invalid_params(documents, params):
    _, data, load = documents
    data["irc"]["on_connect"] = [{"command": "PRIVMSG", "params": params}]
    with pytest.raises(ConfigError) as error:
        load()
    assert str(error.value) == "Invalid configuration field: irc.on_connect"


@pytest.mark.parametrize(
    "secret", ["bad\r\nJOIN #private", "bad\u200bvalue", "bad space", ":bad"]
)
def test_on_connect_literal_validation_is_safe(documents, secret):
    _, data, load = documents
    data["irc"]["on_connect"] = [{"command": "PRIVMSG", "params": [secret, "final"]}]
    with pytest.raises(ConfigError) as error:
        load()
    assert str(error.value) == "Invalid configuration field: irc.on_connect"


@pytest.mark.parametrize(
    "delay",
    [None, True, False, "1", -1, 30.1, float("nan"), float("inf"), float("-inf")],
)
def test_on_connect_invalid_delay(documents, delay):
    _, data, load = documents
    data["irc"]["on_connect"] = [{"command": "LIST", "delay_after": delay}]
    with pytest.raises(ConfigError, match="irc.on_connect"):
        load()


def test_on_connect_count_and_total_delay_limits(documents):
    _, data, load = documents
    data["irc"]["on_connect"] = [{"command": "LIST"}] * 20
    assert len(load().irc.on_connect) == 20
    data["irc"]["on_connect"].append({"command": "LIST"})
    with pytest.raises(ConfigError):
        load()
    data["irc"]["on_connect"] = [{"command": "LIST", "delay_after": 30}] * 2
    assert sum(c.delay_after for c in load().irc.on_connect) == 60
    data["irc"]["on_connect"].append({"command": "LIST", "delay_after": 0.01})
    with pytest.raises(ConfigError):
        load()


@pytest.mark.parametrize("suffix,valid", [("", True), ("x", False)])
def test_on_connect_utf8_wire_boundary(documents, suffix, valid):
    from irctokens.line import build

    _, data, load = documents
    # PRIVMSG NickServ + separating space + parameter + CRLF = 512 bytes.
    secret = "é" * 246 + "x" + suffix
    data["irc"]["on_connect"] = [{"command": "PRIVMSG", "params": ["NickServ", secret]}]
    if valid:
        command = load().irc.on_connect[0]
        assert (
            len(build(command.command, list(command.params)).format().encode()) + 2
            == 512
        )
    else:
        with pytest.raises(ConfigError, match="irc.on_connect"):
            load()


def test_on_connect_parameter_and_command_length_limits(documents):
    _, data, load = documents
    data["irc"]["on_connect"] = [{"command": "a" * 32, "params": ["x"] * 15}]
    command = load().irc.on_connect[0]
    assert command.command == "A" * 32
    assert command.params == ("x",) * 15


def test_on_connect_format_errors_are_safe(documents, monkeypatch):
    _, data, load = documents
    data["irc"]["on_connect"] = [{"command": "LIST", "params": ["private-value"]}]

    def broken_build(*args):
        raise ValueError("private-value")

    monkeypatch.setattr("umarell.config.build", broken_build)
    with pytest.raises(ConfigError) as error:
        load()
    assert str(error.value) == "Invalid configuration field: irc.on_connect"


def test_on_connect_surrogate_is_safe(documents):
    _, data, load = documents
    data["irc"]["on_connect"] = [{"command": "LIST", "params": ["private\ud800"]}]
    with pytest.raises(ConfigError) as error:
        load()
    assert str(error.value) == "Invalid configuration field: irc.on_connect"


@pytest.mark.parametrize("value", [None, "", False, {}, "é" * 2049])
def test_invalid_literal_webhook_secret(documents, value):
    _, data, load = documents
    data["hooks"]["github"]["secret"] = value
    with pytest.raises(ConfigError, match="hooks.secret"):
        load()


def test_literal_credentials_are_used_and_redacted(documents):
    _, data, load = documents
    data["irc"]["password"] = "server-private"
    data["irc"]["sasl"] = {"username": "account", "password": "sasl-private"}
    data["hooks"]["github"]["secret"] = "é" * 2048
    config = load()
    assert config.irc.password == "server-private"
    assert config.irc.sasl.password == "sasl-private"
    assert config.hooks["github"].secret == "é" * 2048
    assert "server-private" not in repr(config)
    assert "sasl-private" not in repr(config)
    assert "é" not in repr(config)


def test_legacy_secrets_mapping_is_rejected(documents):
    _, data, load = documents
    data["secrets"] = {"hook": "private"}
    with pytest.raises(ConfigError, match="config"):
        load()


def test_legacy_sasl_reference_is_rejected(documents):
    _, data, load = documents
    data["irc"]["sasl"] = {"username": "account", "password_secret": "irc"}
    with pytest.raises(ConfigError, match="irc.sasl"):
        load()
