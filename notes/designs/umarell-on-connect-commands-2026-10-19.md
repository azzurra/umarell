# umarell on-connect IRC commands

> Transport update, 2026-10-20: the copied reader/sender and write-time guards referenced in this original design were removed. Both I/O loops are inherited unchanged; setup commands continue to use the native sender. Announcement checks occur before handoff, with connection-boundary teardown. See README for the current delivery boundary and throttle behavior.

## Why and current system

Support network-specific setup, such as NickServ identification, on every IRC connection. Current `src/umarell/config.py` defines strict frozen IRCConfig and loads separate YAML configuration/secrets. `src/umarell/irc.py` RelayServer.line_read handles welcome 001, checks configured SASL, sets registration and immediately sends JOINs. IRCBot._session supervises reader, writer and registration watchdog; any task completion tears down that connection. Existing private writer adapter checks announcement guards after throttling; teardown cancels/awaits workers and discards sends. Tests use a real loopback TLS IRC fixture. Python >=3.13, uv, pytest/pytest-asyncio and Ruff. No applicable AGENTS.md/CLAUDE.md or local workflow routing rules found. Preserve existing staged/unstaged changes; no staging or commits.

## Scope

Add optional irc.on_connect commands, sent in order after successful registration and any configured SASL authentication, before automatic channel joins. Existing configurations default to no commands. Network-specific success-notice parsing and automatic SASL fallback are out of scope.

## Configuration interface

```yaml
irc:
  on_connect:
    - command: PRIVMSG
      params: [NickServ, {secret: nickserv_identify}]
      delay_after: 2
```

The separate secrets file contains:

```yaml
secrets:
  nickserv_identify: "IDENTIFY account password"
```

Each parameter is a literal string or an exact {secret: ID} reference. There is no string interpolation. command is normalized to uppercase ASCII letters ([A-Za-z]{1,32}); parameters are sent through irctokens.build, not a raw-line parser. `params` defaults to [] (commands without parameters supported); `delay_after` defaults to0. Literal/resolved parameter strings must be nonempty and contain no Unicode Cc/Cf/Cs/control characters. Non-final parameters cannot contain whitespace or start with colon; final parameter can contain spaces or start with colon (structured builder quotes it). Other Unicode printable parameters are permitted.

Reject unknown keys, missing secret references, wrong types (including bool for numeric delay), control characters, invalid IRC parameter boundaries, and fully expanded lines exceeding 512 UTF-8 bytes including CRLF. Validate `.format()` before checking length; formatting errors become safe ConfigError. Permit at most20 commands, 15 parameters per command, delays finite 0..30 seconds, and total delay <=60 seconds. irc.on_connect must be a list; explicit [] allowed, null rejected. Commands conflicting with managed registration/membership—PASS, NICK, USER, CAP, AUTHENTICATE, QUIT, JOIN, PART—are rejected. This still permits PRIVMSG, NOTICE, MODE, and network-specific service commands such as NICKSERV. Resolved params are hidden from all config representations/errors (literal params may also contain credentials).

## Runtime interfaces

Append `on_connect: tuple[OnConnectCommand, ...] = ()` with repr=False to IRCConfig after existing fields, preserving positional callers. Frozen OnConnectCommand has `command: str`, resolved `params: tuple[str, ...]` with repr=False, and `delay_after: float = 0`. Load uses existing `_identifier` for secret IDs, requires values from existing Config.secrets mapping and same file separation/permissions; no extra credential byte cap beyond complete wire limit. Safe error paths use constant `irc.on_connect` field identifiers, never command/parameter/secret values.

## Connection flow and concurrency

Add RelayServer setup task method `_setup_connection()` supervised alongside reader/writer/watchdog. It waits for `registration`, then (within90 seconds) sends each command through `await asyncio.shield(self.send(build(command, list(params))))`, awaits optional delay, marks setup_complete=True, and sends automatic JOINs. It runs separately from reader so delays do not block PING/PONG, incoming notices, or disconnect detection. After JOIN writes it waits forever until cancelled (normal setup completion must not trigger FIRST_COMPLETED disconnect). Registration waiting uses existing 60s watchdog; 90s setup timeout starts only after registration. Sending uses existing native throttle; no global announcement queue or replay.

line_read on001 checks SASL then sets registration idempotently; it no longer sends JOINs. A fresh server/task per connection ensures once-per-connection execution; duplicate001 does not start a new sequence. Own JOIN only marks relay channel ready if setup_complete=True. No app announcements admitted before normal join readiness. SASL remains fail-closed: configured SASL failure prevents registration event, on-connect commands and JOINs. Commands may also run after successful SASL if configured.

## Lifecycle and failure modes

Disconnect or shutdown cancels/awaits setup with the other tasks and discards pending library sends; commands cannot migrate to a new connection. Each reconnect reruns the configured commands from the beginning, never a partial tail. Transport failure and setup timeout follow existing reconnect path. Unexpected programming failures propagate through service supervision. No command/parameter values or network responses are logged. Existing write future cancellation protections retained. Existing verified TLS mandatory. An operator's command rejected by remote server does not cause inferred authentication success/failure: generic commands have no portable acknowledgment contract. Subsequent JOIN can fail per normal behavior.

## NickServ semantics and operator UX

Sending IDENTIFY does not prove authentication succeeded. delay_after lets operators allow time for services to respond; it is a delay, not confirmation. Networks without SASL must omit irc.sasl; configured SASL remains required with no fallback. Commands go directly to server (e.g. PRIVMSG target NickServ), not slash commands such as /msg; no placeholders/nickname interpolation. Keep complete sensitive trailing parameter in secrets file as illustrated. Restart to reload. Default config/example stays backward-compatible with a commented opt-in example and separate placeholder secret.

## Surfaces touched

`src/umarell/config.py`, `src/umarell/irc.py`, `tests/test_config.py`, `tests/test_irc.py`, README.md, examples/config.yaml and examples/secrets.example.yaml. No webhook/relay API changes, dependency changes, persistence or schema migrations.

## Verification criteria

Focused configuration tests: absent/empty default, uppercasing/literal/secret expansion, hidden values in repr and errors, missing references, malformed lists/mappings/keys/commands, prohibited commands, control injection, nonfinal whitespace/colon, parameter count, expanded UTF8 boundary512, per/total delay count/types/NaN/infinity. Test final parameter quotes correctly on wire, no-parameter command and multiple-command order.

TLS IRC fixture tests: commands before JOIN, delay precedes JOIN, once per connection despite duplicate001 and again after reconnect; successful NickServ-style secret command without SASL; commands after SASL success and none after SASL failure; PING/PONG serviced during delay; cancellation/disconnect during setup prevents stale queued writes/JOINs; fresh connection restarts sequence; no command contents in logs. Existing zero-command TLS tests remain green. Test setup timeout with controlled wait or reduced module constant so it does not wait90 seconds; verify reconnect or propagated recoverable TimeoutError. Test setup task success stays alive. Existing suite covers verified TLS and full webhook flow.

Run `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, `.venv/bin/pytest -q --tb=short` (loopback socket tests need sandbox escalation), `uv build`; installed CLI --check-config with example-derived opt-in configuration. No public IRC messages or credentials. Independent precommit review and fix/review loop, no staging/commit.

## Out-of-scope follow-ups

NickServ response matching/verified service authentication, SASL fallback, dynamic commands or shell execution, registration/membership overrides, placeholders, metrics, persistence and channel rejoin improvements.

## Design-check outcome

All three sequential fresh-reader checks passed in round 1: `comprehension passed`, `design ready`, and `implementation ready`. Both required gates closed before application code edits; no design gaps or rebuttals remained.

## Implementation and validation outcome

Implemented on 2026-10-19 in the planned surfaces. Configuration resolves secret parameters once, validates expanded wire lines, and hides parameters from representations. Each IRC session supervises a separate setup task; registration no longer sends JOINs directly. No dependencies or webhook/relay interfaces changed.

- Added 62 configuration cases and eight IRC cases; all 230 tests pass. Focused configuration/IRC run: 112 tests pass.
- Ruff lint and formatting pass; wheel and source distribution build successfully; git diff whitespace check passes.
- Local TLS IRC walkthrough verifies secret-bearing NickServ-style commands before JOIN without SASL, ordered and zero-parameter commands, duplicate-welcome suppression, reconnect execution, PING/PONG during delays, cancellation, setup timeout reconnect, and required SASL gating. No public IRC or real credentials used.
- Public CLI --check-config accepts an example-derived opt-in configuration with the sensitive trailing parameter in a separate mode0600 secrets file.
- Independent precommit review completed in one round with `no findings`; no fixes or rebuttals required. The reviewer also ran the 96 configuration tests. Existing branch has no main/HEAD commits, so review used staged, unstaged and untracked files instead of commit comparisons.
- Socket tests required permission to bind local loopback listeners outside the sandbox. No excluded/pre-existing test or lint failures. Existing staging preserved; no files staged or committed by implementation.

Network-specific NickServ reply matching, automatic fallback from required SASL, and placeholders remain out of scope. Sending and delaying a command is not authentication confirmation.
