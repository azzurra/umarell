# umarell

umarell sends concise GitHub webhook announcements to IRC over verified TLS. It runs one IRC connection and a configurable HTTP listener. GitHub delivery is best effort: there is no persistent queue, retry, or delivery guarantee, and no GitHub API token is needed.

It announces PR opening (including drafts), reopening, readiness for review, closing and merging; issue opening, reopening and closing; submitted review decisions (approval or changes requested); and every completed workflow attempt and conclusion. Reruns appear separately, with their attempt number. Review comments, discussion comments, review edits/dismissals, and other events produce no messages. Workflow output includes the branch and short commit SHA. Messages are sanitized and shortened to fit a single IRC line.

## Set up

Install Python 3.13+ and uv, then run:

```sh
uv sync
cp examples/config.yaml config.yaml
chmod 600 config.yaml  # Or chmod 640 with an appropriate owner/group.
```

Edit `config.yaml` with your IRC host, channel, allowed repository numeric IDs, and a long random webhook signing secret in `hooks.team.secret`. This file contains sensitive values: use mode `0600` or `0640` with an appropriate owner/group. Keep real credentials out of source control: root `config.yaml` and `config.yml` are ignored; other configuration paths need your own ignore rule. The example contains placeholders, never working credentials.

```sh
uv run umarell --config config.yaml --check-config
uv run umarell --config config.yaml
```

`python -m umarell` accepts the same arguments. `--config` is required. On POSIX, configuration permissions must not grant execute access, group write, or any access to others (use `0600` or `0640`). Validation opens no network connections. Exit codes are 0 for successful validation or normal shutdown, 2 for invalid configuration, and 1 for unexpected runtime failure. Restart the process to reload settings.

To migrate from the two-file format, copy each actual credential into its configuration field: replace `irc.password_secret` with `irc.password`, `irc.sasl.password_secret` with `irc.sasl.password`, and set each `hooks.<hook>.secret` to its signing secret value. Replace each on-connect `{secret: ID}` parameter with its literal value. An old hook secret ID is still a valid string and would be used literally, so replace it explicitly. Remove the secrets mapping/file and `--secrets` argument, then set permissions on the combined file.

IRC always uses verified TLS, normally on port 6697. `ca_file` optionally adds a CA file relative to the configuration directory. Optional `irc.password` contains the literal server password. Optional `sasl` has a literal `username` and `password`; it supports PLAIN only over verified TLS. When configured authentication fails or is unavailable, the bot does not join or announce and retries the connection. Channel keys are not supported.

Optional `irc.username` and `irc.realname` set the username and realname sent in IRC's USER registration command on every connection. Each defaults to the configured nickname when omitted. `irc.username` is separate from the authentication account in `irc.sasl.username`. For example:

```yaml
irc:
  # Keep your existing host, channels, and other settings here.
  nickname: umarell
  username: github-bot
  realname: "umarell GitHub announcements"
```

The username accepts 1–32 printable ASCII characters without whitespace and cannot start with `:`. The realname accepts up to 256 UTF-8 bytes, including spaces and Unicode; it must contain a non-whitespace character and cannot contain control, format, or surrogate characters. Supplied values must be nonempty strings; explicit nulls are rejected. The bot preserves realname text, although the IRC server may rewrite or truncate identity fields. Restart to apply configuration changes.

### On-connect commands and NickServ

Use `irc.on_connect` for commands that run once after registration and any configured SASL authentication, before the bot joins its channels. They run again on each reconnect. For a network without SASL, omit `irc.sasl` and add this to the configuration:

```yaml
irc:
  # Keep your existing host, channels, and other settings here.
  on_connect:
    - command: PRIVMSG
      params: [NickServ, "IDENTIFY YOUR_ACCOUNT YOUR_PASSWORD"]
      delay_after: 2
```

Adjust the complete trailing parameter to your network's NickServ syntax. Each parameter is a literal string; there is no interpolation or shell execution. Use IRC protocol command names such as `PRIVMSG`, not client slash commands such as `/msg`. Literal parameters can set modes or send other network-specific setup commands. Parameters are hidden from configuration representations and errors.

Commands execute in list order. `params` defaults to `[]`; `delay_after` defaults to zero and specifies seconds to wait after writing that command. Delays do not block PING/PONG or reading server responses. A delay gives services time to process identification, but the bot does not parse NickServ replies or confirm authentication success. There is no automatic fallback from configured SASL: failed required SASL prevents commands and joins.

At most 20 commands, 15 parameters per command, 30 seconds per delay, and 60 seconds of total configured delay are allowed. Command lines must fit 512 UTF-8 bytes including CRLF. Control characters are rejected; only the last parameter may contain whitespace or start with `:`. Registration/membership commands `PASS`, `NICK`, `USER`, `CAP`, `AUTHENTICATE`, `QUIT`, `JOIN`, and `PART` are managed by the bot and cannot be used here. Connection setup has a 90-second timeout after registration. Disconnect or shutdown cancels the sequence; a reconnect starts it anew rather than replaying an unfinished tail. Restart to reload configuration.

## Publish the webhook endpoint

The HTTP listener defaults to `::` (`IN6ADDR_ANY`), listening on all IPv6 interfaces on port 8080. Set `http.host` to override the bind address, for example `0.0.0.0`, `127.0.0.1`, or `::1`. IPv4 availability on an IPv6 listener depends on the operating system; configure an IPv4 address explicitly when required.

It is the operator's responsibility to provide a secure HTTPS reverse proxy in front of the HTTP listener and restrict direct access to the plain HTTP port. The bot does not terminate HTTPS or trust forwarded headers. Supply your own hostname, certificate, TLS policy, and network access controls. For example, inside an nginx HTTPS server block with your certificate configuration:

```nginx
server {
    listen 443 ssl;
    server_name hooks.example.org;
    ssl_certificate /etc/ssl/hooks/fullchain.pem;
    ssl_certificate_key /etc/ssl/hooks/privkey.pem;

    location /webhooks/github/ {
        client_max_body_size 1m;
        client_body_timeout 15s;
        proxy_connect_timeout 5s;
        proxy_send_timeout 15s;
        proxy_read_timeout 15s;
        proxy_pass http://[::1]:8080;
    }
}
```

Match proxy body limits to `http.max_body_bytes`, and bound request timeouts at the proxy. Expose only the intended webhook endpoint and keep the plain HTTP listener private. Adjust the proxy upstream to match your configured bind address and deployment topology.

In each allowed repository's GitHub webhook settings, set:

- Payload URL: `https://hooks.example.org/webhooks/github/team` (`team` is the configured hook key).
- Content type: `application/json`.
- Secret: the literal value of `hooks.team.secret`.
- Individual events: Pull requests, Issues, Workflow runs, and Pull request reviews.
- SSL verification: enabled.

Use numeric repository IDs from GitHub repository metadata, not names. Each hook explicitly lists its allowed IDs, destination channels, and event categories. There is no fallback routing. A hook can allow multiple repositories and routes; multiple hooks may use separate secrets. Every destination must match a configured IRC channel's spelling exactly. Omit a repository's `events` to enable all four categories; use `events: []` to disable them.

## Configuration and delivery

The example shows all HTTP and relay defaults. HTTP defaults to `[::]:8080`, a 1 MiB body limit and 32 concurrent requests. IRC defaults to nickname `umarell` and port 6697; host and a nonempty channel list are required. The pending queue holds 100 destination messages, expires each after 30 seconds and sends at a global interval of 1 second. The duplicate cache holds up to 10,000 delivery IDs for 3,600 seconds. All these limits are configurable within validated bounds. Unknown or duplicate YAML keys, invalid types, missing required values, and duplicate channels are rejected. Hook IDs allow ASCII letters, digits, `_` and `-`, up to 64 characters.

Messages are accepted for an IRC destination only after registration, any configured authentication, and observing the bot's own join. Events during startup/reconnect are discarded. Disconnect, channel loss, restart, queue overflow, or expiry can discard pending messages; rejoining does not replay them. Partial delivery across several channels is possible. Readiness follows observed IRC events, so already written bytes cannot be recalled.

The relay checks expiry and readiness before handing an announcement to ircrobots. Once handed over, a message cannot be reliably recalled from the library's queue, including after channel loss. Connections use ircrobots' native reader and sender; queued messages are never moved to a replacement connection. The bot leaves the library's initial 100-lines-per-second throttle in place and ignores its post-registration throttle change; `relay.send_interval` controls announcement pacing.

HTTP responses have empty bodies:

| Code | Meaning |
| --- | --- |
| 200 | Authenticated GitHub ping. |
| 202 | At least one destination queued; this does not guarantee IRC receipt. |
| 204 | Duplicate, filtered event/action, or every destination discarded. |
| 400 | Invalid JSON, headers, or enabled event payload. |
| 403 | Invalid/missing signature or unauthorized repository. |
| 404 | Unknown hook. |
| 413 | Body too large. |
| 415 | Unsupported content type or content encoding. |
| 503 | Concurrent processing limit reached. |
| 500 | Unexpected internal failure. |

Signatures use HMAC-SHA256 over the exact received bytes. Only absent or identity content encoding is accepted. Delivery IDs and event headers are dispatch hints; the bounded in-memory duplicate cache is not replay protection. It resets on restart and includes authenticated filtered and discarded deliveries. Malformed enabled events are not cached. There is no API redelivery or persistence.

Output URLs are limited to HTTPS `github.com`. GitHub Enterprise, interactive commands, digests, hot reload, health routes, and metrics are outside this version. Runtime diagnostics are minimal and omit credentials and webhook payloads.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv build
```

Tests use local fixtures and temporary credentials; public IRC or GitHub access is unnecessary.
