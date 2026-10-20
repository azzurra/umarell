# umarell implementation design

> I/O implementation superseded on 2026-10-20: `RelayServer` now inherits both ircrobots I/O loops unchanged. The copied-loop design and post-throttle guard descriptions below are historical. Readiness and expiry are checked before library handoff; connection teardown releases outstanding announcement waiters and transport read helpers. A no-op `set_throttle` retains the library's initial throttle while the relay controls announcement pacing. Messages already handed to the library cannot be reliably recalled after channel loss.

## Why and current stateghbot

Implement the approved `notes/prds/umarell-github-irc-bot-2026-10-19.md`: one team's GitHub repository webhooks produce concise IRC announcements. Delivery is best effort; pending events never survive disconnects or restarts. Current code is only `src/malgioglio/__init__.py:1` greeting and `pyproject.toml:1` uv project. No AGENTS.md/CLAUDE.md rules, tests, or CI exist. Python >=3.13. Preserve existing user changes and staging; do not stage or commit.

## Scope and surfaces

Rename package/distribution/CLI to `umarell`; replace greeting with CLI. New modules: `src/umarell/{__init__,__main__,config,events,relay,irc,web}.py`. Update `pyproject.toml`, `uv.lock`, README, .gitignore; add `examples/config.yaml`, `examples/secrets.example.yaml`, `tests/test_{config,events,relay,web,irc,cli}.py`. Keep the PRD historical current-state references unchanged. Remove unused aiosqlite, add aiohttp, pin ircrobots==0.7.2 to the inspected adapter API; use PyYAML. Dev tools pytest, pytest-asyncio, ruff. No DB, comments, external CI, interactive commands, metrics or health routes.

## Configuration interface

`umarell --config PATH --secrets PATH [--check-config]`; both paths required, separate resolved files, check-config validates without networking. `python -m umarell` equivalent. Exit 0 for successful validation/normal termination, 2 invalid configuration, 1 unexpected runtime failure. No credentials/config/payload values in errors; use fixed field-path diagnostics. Minimal stderr lifecycle/error messages only, no raw IRC logging. Restart to reload. YAML safe_load, reject unknown keys, duplicate mapping keys, wrong types (bool is not int), nonfinite numbers, missing required fields, and same config/secrets path. Secrets file on POSIX must have no group/other permission bits. Example file uses placeholders and is intentionally named .example; document chmod 600. No environment secret fallback.

Schema (defaults shown):

- `http: {host: 127.0.0.1, port: 8080, max_body_bytes: 1048576, max_inflight: 32}`. port 1..65535, body 1024..26214400, inflight 1..1024. Upstream verified HTTPS reverse proxy required; HTTP binds loopback only (127.0.0.1, ::1, localhost), no forwarded-header trust. TLS terminates at operator proxy; document body/time limits and external HTTPS setup.
- `irc: {host: REQUIRED, port: 6697, nickname: umarell, channels: REQUIRED_LIST, ca_file: OPTIONAL_PATH, password_secret: OPTIONAL_ID, sasl: OPTIONAL {username: REQUIRED, password_secret: REQUIRED_ID}}`. TLS mandatory, no insecure switch. ca_file relative to config directory; ssl default verified context with optional additional CA file. host nonempty hostname/IP no whitespace/control; nickname ASCII IRC nickname <=30 bytes. Channel names ASCII starting # or &, <=80 bytes, no whitespace, colon, comma or controls; reject casefold-equivalent duplicates (RFC1459), route references must match configured spelling exactly. Nonempty channels list. Server password optional, SASL USERPASS restricted to PLAIN over verified TLS (avoid upstream SCRAM validation concern). Credential strings never logged; validate no CR/LF/NUL in server password, no NUL in SASL username/password, cap credential lengths to prevent excessive protocol output. No channel keys in MVP.
- `relay: {capacity: 100, expiry_seconds: 30, send_interval: 1.0, dedupe_capacity: 10000, dedupe_seconds: 3600}` optional. capacity 1..10000, expiry .1..3600, interval .1..60, dedupe capacity 1..100000, dedupe ttl 1..86400. All finite. Bounds are implementation defaults, not an SLA.
- `hooks: {HOOK_KEY: {secret: SECRET_ID, repositories: [{id: POSITIVE_INT, channels: NONEMPTY_LIST, events: [pull_request, issues, workflow_run, pull_request_review]}]}}`. Hooks and repository lists nonempty. Hook/secret IDs ASCII [A-Za-z0-9_-]{1,64}; repository IDs unique within hook, destinations subset of configured channels without duplicates; events optional defaults all four, explicit [] disables all. Unknown event names rejected. No fallback repository/channel. Distinct webhook and IRC secret references may use one secrets mapping.
- Secrets document: `secrets: {SECRET_ID: NONEMPTY_STRING}`; require all references exist; webhook secret max4096 bytes; never include secret values in repr/errors. Frozen dataclasses for config, secret fields repr=False.

## Design-check revision 1

=== CRITIC GAPS ===

1. Registration watchdog: successful registration disarms the timeout, then the watchdog waits forever until cancellation; it never finishes normally and disconnects a healthy connection. Test sustained connection and multiple announcements.
2. Content encoding: reject Content-Encoding other than absent/identity with 415 before body reading; construct aiohttp server with automatic decompression disabled. Test gzip rejection.
3. Repository display and templates: require nonempty repository.full_name string for normalized announcements; render that full name. Exact templates below.
4. Filtered dedupe: disabled/unknown events and unsupported actions insert the cache after signature and repository authorization; malformed supported events never insert it. Exact flow below.

=== READINESS OPEN QUESTIONS ===

1. Repository and workflow display: repository.full_name required; branch string|null defaults unknown, SHA string|null defaults unknown; branch and first seven SHA characters always shown. Exact templates below.
2. Credential byte limits: server password max256 UTF-8 bytes with no whitespace/control; SASL username max128 and password max256 UTF-8 bytes, no Unicode control characters. Bound strings before sending; errors mention field only.
3. Filtered-event cache insertion: same explicit flow below resolves this independently reported gap.

=== COMPREHENSION FEEDBACK ===

Comprehension passed; no unclear sections.

## Event interfaces and semantics

`normalize(event: str, payload: dict) -> Announcement | None` raises `InvalidEvent` for malformed supported event/action payloads. Announcement has `text: str` (without URL), `url: str`; repository ID authorization happens before normalization. Require JSON object; top-level repository object with positive integer id for all non-ping events, then explicit hook allowlist. Unknown event or unsupported action returns None without validating irrelevant subject fields. Allowed actions require strictly typed required fields; optional display fields have safe fallbacks. Enforce URL is HTTPS github.com without credentials, whitespace, controls, or nonstandard port. No enterprise support in MVP.

- PR actions opened/reopened/ready_for_review/closed; require pull_request number positive int, title string, html_url, draft bool for opened, merged bool for closed; merge closure => merged, otherwise closed. Sender login optional fallback unknown; draft opened marked draft.
- Issues opened/reopened/closed: issue number positive int/title/html_url. Sender optional fallback unknown.
- Reviews submitted only: review object/state string; approved or changes_requested require reviewer login, PR number/html_url; other states ignored, no body/comment fields read. Edited/dismissed ignored.
- Actions completed only: workflow_run id positive int, html_url, name string, run_attempt positive int (default 1), optional head_branch/head_sha, conclusion string|null. Render every conclusion (including unknown sanitized values); absent/null => unknown; attempt >1 shown. No workflow_job/check_run output. No recovery state. Delivery-ID dedupe never keyed only by run ID.

`format_message(announcement, channel) -> str | None`: collapse whitespace, remove Unicode control/format/surrogate characters and IRC controls from every display field; preserve printable Unicode. Compose text plus URL; truncate text at UTF-8 boundaries to fit <=400 encoded bytes for entire outgoing `PRIVMSG channel :text\r\n` (conservative allowance for relayed server prefix); if URL itself cannot fit, discard message. Never split across lines or fetch URL. Message examples follow PRD. Missing required fields/types or invalid URLs => 400 without payload leakage. Optional strings with wrong non-null types => InvalidEvent.

Exact untruncated templates (bracket label is repository.full_name, not shortened repository.name): PR `[owner/repo] PR #N ACTION by ACTOR: TITLE`, with ACTION `opened (draft)` for draft opening or `opened`, `reopened`, `ready for review`, `closed`, `merged`; issue `[owner/repo] Issue #N ACTION by ACTOR: TITLE`; review `[owner/repo] PR #N approved by REVIEWER` or `changes requested by REVIEWER`; workflow `[owner/repo] CI NAME on BRANCH @SHA7: CONCLUSION` plus ` (attempt N)` only for attempt>1. Append one space and URL. Unknown actor/branch/SHA/conclusion displays `unknown`. Required display strings may not be empty. Review reviewer is review.user.login. URL validation rejects controls/whitespace before sanitizing; display sanitization never changes URL.

## HTTP request flow

`create_app(config, relay) -> aiohttp.web.Application`; production lifecycle starts relay/IRC tasks, tests may inject fake relay and disable networking. `POST /webhooks/github/{hook_key}`; unknown hook 404. Reject non-application/json 415; bound raw body bytes 413. Limit concurrent processing with counter (no waiting backlog): limit reached 503. HMAC-SHA256 exact bytes; ASCII signature `sha256=` plus 64 hex characters, constant-time compare; missing/mismatch/malformed =>403. Decode JSON (reject NaN/Infinity) and require object =>400. Required event/delivery headers: event nonempty ASCII <=64, delivery nonempty ASCII printable <=128 =>400. Headers are dispatch/dedupe hints, not signed replay protection. Authorized ping requires verified signature and object, needs no repository, returns 200.

For all other events validate repository ID then authorize it (403 unknown repository). Check enabled category; only enabled categories are normalized (400 malformed). Disabled category or ignored action has announcement=None. Then atomically check duplicate (204 if present), insert cache entry, and offer announcement if any; filtered and unavailable/full destinations are cached too. Malformed enabled events never insert. Ping returns200 without cache. TTL/capacity bounded OrderedDict, monotonic clock, reset on restart. Accepted enqueue =>202; filtered or all destinations discarded =>204; response empty, never implies IRC receipt. Dedupe and enqueue operations contain no awaits, avoiding duplicate concurrent admission. Unexpected errors =>generic 500; safe minimal diagnostics, no exception text containing input. No GitHub token/API calls/redelivery. HTTPS reverse proxy exposure documented and local plain HTTP test harness only.

## Relay and lifecycle

`Relay` owns bounded FIFO pending records, dedupe cache, generation/readiness, and a single dispatch task. Queue item = destination, rendered text, monotonic deadline, connection generation and per-channel generation. Queue capacity counts destination items; partial fanout allowed. `offer(announcement, channels) -> int` enqueues only registered/authenticated/joined destinations; full drops new item. Single FIFO plus fixed global send interval provides rate limiting. No per-webhook tasks. TTL evaluated at offer and again immediately before actual write. No periodic backlog, retry, or persistence. API accepts during startup/reconnect but drops output until channel ready.

Only current connection owns readiness. Own JOIN enables a configured channel; own PART/KICK removes it and increments channel generation, invalidating queued items; disconnect invalidates all. Foreign events never change bot readiness. Casefold uses IRC server casefold. A generation mismatch invalidates popped/queued/in-flight work even after channel rejoin. Shutdown first prevents offers, clears pending, cancels dispatcher and IRC runner, awaits both, closes socket with bounded timeout. Readiness lost only after observed network/protocol event; cannot recall already-written bytes.

## IRC adapter and dependency constraints

Subclass ircrobots Bot/Server for state parsing, CAP negotiation and structured sending. Own asyncio supervisor instead of upstream AnyIO task-group runner, avoiding deprecated AnyIO APIs; use asyncio.open_connection wrapped in ircrobots TCPReader/TCPWriter, verified ssl context/hostname and 30s connect timeout. Build new Server each connection; params no autojoin, explicit configured joins after welcome AND successful configured SASL. PLAIN-only SASL override of server.sasl_auth uses upstream SASLContext.plain; no SASL configured => registration sufficient. Configured SASL absent/refused/fails => no join/output, close then backoff retry. Registration timeout 60s. Sleep reconnect backoff 1,2,4..60s; reset after successful registration. Nick fallback supported by library.

Supervise one reader and one writer task per Server plus registration watchdog. First completion cancels/awaits companions and tears down; retry network/timeout/protocol-disconnect failures only, unexpected errors propagate to application fatal supervisor. Initial connection failure leaves HTTP alive and retries. Fatal relay/IRC worker stops HTTP and exits1. Normal SIGINT/SIGTERM exits0. Close sockets on failure/cancellation including partially connected server; cancel and await ircrobots-created read helpers to avoid leaks if upstream cancellation path leaves them (adapter wraps _read_line/wait cleanup).

Override pinned Server._send_lines in adapter to process one queued SentLine at a time, await native throttle, and only then check delivery guard before writer.write/drain. Announcement guard includes ready connection/channel generations and TTL. Protocol lines still sent normally; annotate application PRIVMSG Lines by identity, never protocol lines. Resolve skipped send futures safely; do not cancel futures still used by writer. At most one announcement enters library queue because dispatch awaits its completion. On channel loss generation guards prevent a queued line from writing even if it was popped before loss; on disconnect close socket and clear/cancel pending futures only after writer stopped. Use latest Server reference, never reuse obsolete instance. Test private pinned API explicitly. This is necessary because checking only before Server.send cannot enforce no replay after throttle waits.

## Verification and completion criteria

Use pytest/pytest-asyncio, ruff check/format; no existing typecheck or CI tier to preserve. Commands: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest`, `uv build`, CLI help and --check-config. Focused tests after each layer. Tests cover strict configuration/secrets separation and permissions/redaction; PR drafts/merge/close, issue lifecycle, approvals/change requests/comments ignored, every CI conclusion/rerun; invalid signatures/raw Unicode body/malformed JSON/body bounds/authorization/ping/duplicates/overflow; UTF8 line budget and injection; deterministic expiry, rate limit, partial fanout, disconnect/rejoin invalidation, dedupe expiry/capacity, restart fresh state. Local asyncio IRC fixture exercises real ircrobots over actual loopback transport: CAP registration, welcome, join readiness, PING/PONG, PRIVMSG, reconnect, pending drop, failed/absent SASL, successful PLAIN, hostname/certificate validation via test CA and TLS server. No public IRC/GitHub credentials or outbound announcements needed. aiohttp HTTP fixture golden path signed payload -> local IRC line; negative invalid signature -> no line. Cancellation and fatal task tests verify HTTP stops and no task leaks. Build/install/CLI tests confirm umarell rename. Independent review after tests, fix and rerun until clean. No staging/commit.

## Out-of-scope follow-ups

Instrumentation (G7 deferred), metrics/health endpoints, persistence/recovery, external CI, additional networks, comments, digests, hot reload, GitHub Enterprise, and channel keys.

## Completion and validation

Implemented on 2026-10-19. Design round 1 comprehension passed; critic and readiness gaps were resolved by the normative revision above. Round 2 closed with `design ready` and `implementation ready` before application code was written.

- Configuration and event layers: strict loading, safe diagnostics, explicit routing, event templates, and single-line UTF-8 formatting.
- Runtime layers: memory-only relay, authenticated aiohttp endpoint, guarded ircrobots sends, verified TLS/PLAIN SASL, reconnect and supervised shutdown.
- Operator surfaces: renamed package/CLI, configuration and secret examples, README setup and HTTPS proxy instructions.
- Final checks: Ruff lint and formatting passed; all 160 pytest tests passed; wheel and source distribution built successfully; installed CLI and wheel entry point verified.
- Local protocol walkthrough: signed HTTP payload reached a real loopback TLS IRC fixture; invalid signature produced no announcement; registration, PING/PONG, SASL success/failure/unavailability, certificate/hostname rejection, reconnect, and stale-send invalidation exercised without external credentials.
- Independent review: two rounds. One finding fixed: translate upstream unsupported-SASL-mechanism errors into authentication-unavailable reconnect behavior. Regression tests verify no JOIN and continued reconnects. Second review returned `no findings`; no rebuttals.

Socket-based tests required execution outside the network-restricted sandbox to bind loopback listeners. No application test failures remain. No pre-existing lint/typecheck/test failures were excluded; no prior typecheck/CI configuration existed. The repository has no `main`/HEAD commit baseline, so review used staged, unstaged and untracked files. Existing staging was preserved; implementation did not stage or commit changes. Production IRC/GitHub deployment was not performed.
