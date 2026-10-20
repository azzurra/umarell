# PRD: umarell GitHub-to-IRC Bot

**Status:** Approved 2026-10-19  
**Depth:** Standard  
**Research:** Public web sources  
**Release:** Core relay and default filters; no fixed deadline

## Executive Summary

umarell is a Python bot that announces GitHub activity in a team's IRC channel. It uses **ircrobots** for IRC connectivity and **aiohttp** for receiving webhooks within one asyncio process.

The first release supports public and private repositories, one IRC network, and one or more explicitly configured channels. It announces pull request and issue lifecycle events, every GitHub Actions workflow completion, and review decisions.

Delivery is **best effort with no catch-up**. Events received while IRC is unavailable are discarded. Pending announcements are discarded on disconnect or restart. This replaces the earlier brainstorm's durable inbox/outbox design.

## Problem Statement

The team needs visibility into repository activity without continually checking GitHub. Announcements should show what happened and provide a link while excluding comments and routine metadata changes.

The service should remain simple to operate. Historical completeness and recovery after outages are not requirements.

## Target Users

- **Team members** following development activity through IRC.
- **The operator** configuring repositories, channels, credentials, and the long-running process.

This release serves one team, without tenant isolation, accounts, or a management UI.

## Current State

The repository contains a minimal Python package:

- Python >=3.13, uv packaging, and a `src/` layout.
- Existing dependencies: ircrobots, aiosqlite, and PyYAML.
- A `ghbot` CLI that currently prints a greeting.
- No webhook endpoint, IRC behavior, configuration schema, tests, or deployment documentation.

The existing scaffold is named `ghbot`; the approved product name is **umarell**. Implementation shall rename the distribution, Python package, and CLI to `umarell` and update corresponding references.

Grounding references: `pyproject.toml:1`, `src/ghbot/__init__.py:1`. No applicable AGENTS.md or CLAUDE.md conventions were found.

## Proposed Solution

Run one process containing:

1. An aiohttp webhook endpoint.
2. Signature verification and payload validation.
3. Repository authorization, event filtering, and channel routing.
4. A bounded, memory-only announcement queue.
5. An ircrobots connection and rate-limited dispatcher.

Use application lifecycle management to start, supervise, and stop the HTTP and IRC tasks together. aiohttp documents this pattern for long-running background tasks. [aiohttp lifecycle guidance](https://docs.aiohttp.org/en/stable/web_advanced.html#background-tasks).

No delivery database or durable outbox is required.

## Scope

**Included**

- GitHub repository webhooks for public and private repositories.
- One IRC network; one channel by default, optional multiple channels.
- PR and issue lifecycle announcements.
- Every Actions workflow completion.
- Review approvals and requests for changes.
- YAML configuration and a separate secrets file.
- Verified HTTPS webhook ingress and verified TLS IRC connections.
- Temporary flood-control queuing and automatic IRC reconnection.

**Excluded**

- Issue comments, PR comments, inline review comments, and comment-only reviews.
- Backlog replay, missed-webhook recovery, historical synchronization, or delivery persistence.
- Multiple IRC networks, interactive IRC commands, dashboards, and GitHub API enrichment.
- External CI providers through check-run or commit-status events.
- Digests and recovery-only CI policies.

## User Stories / Jobs To Be Done

- As a team member, I can see PR and issue lifecycle changes with direct GitHub links.
- As a developer, I can see every completed workflow outcome, including successful runs.
- As a PR author, I can see approvals and requests for changes without comment text.
- As an operator, I can route authorized repositories to trusted channels.
- As an operator, I can restart the bot without triggering historical announcements.

## Functional Requirements

### FR1 — Startup and configuration

The CLI shall accept paths to a configuration file and a separate secrets file.

Configuration shall define:

- HTTP bind address and webhook endpoint settings.
- IRC server, port, nickname, TLS, and optional authentication.
- Authorized repositories and their channel destinations.
- Enabled event categories.
- Queue capacity, announcement expiry, and send-rate settings.

Secrets shall contain webhook signing secrets and IRC credentials, referenced by configuration identifiers. Invalid or missing required configuration shall fail startup with a useful error that does not expose secrets.

Both files shall use safe YAML parsing and explicit schema validation. [PyYAML documentation](https://pyyaml.org/wiki/PyYAMLDocumentation).

### FR2 — Webhook ingestion

Provide `POST /webhooks/github/{hook_key}`, where `hook_key` selects configured webhook settings and is not itself a secret.

The handler shall:

- Bound request-body size.
- Verify `X-Hub-Signature-256` against the original bytes using HMAC-SHA256 and constant-time comparison.
- Validate JSON and the fields required for the selected event.
- Authorize the payload's repository against the selected webhook configuration.
- Handle authenticated GitHub `ping` requests without IRC output.

Missing or invalid signatures shall receive `403`; malformed supported payloads `400`; oversized requests `413`.

GitHub documents signature verification over the original body, including Python's `hmac.compare_digest`. [Webhook validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries).

### FR3 — Event policy

The following are the approved launch defaults:

| Event | Announcement behavior |
|---|---|
| `pull_request` | Opened, reopened, ready for review, closed, and merged; identify draft PRs when opened |
| `issues` | Opened, reopened, and closed |
| `workflow_run.completed` | Every completion, preserving the supplied conclusion |
| `pull_request_review.submitted` | Only `approved` and `changes_requested` |
| Comments, metadata changes, unknown events/actions | No announcement |

A merged PR shall be distinguished from an unmerged closure using its merge flag. Workflow completions shall be counted per attempt, rather than per matrix job. Reruns must remain eligible for announcements.

Comment-only reviews, review bodies, inline comments, and review edits or dismissals shall not generate output. These distinctions follow GitHub's separate event and review models. [Webhook event reference](https://docs.github.com/en/webhooks/webhook-events-and-payloads), [review states](https://docs.github.com/en/rest/pulls/reviews).

### FR4 — Routing and private repositories

Every enabled repository shall have explicit trusted destinations. Configuration may map many repositories to the same channel or one repository to several channels.

The bot shall:

- Use the event's repository identity, rather than a contributor's fork, for authorization and routing.
- Never derive destinations from titles, branches, labels, or other contributor-controlled text.
- Never send an unknown repository to a fallback channel.
- Send private-repository activity only to explicitly configured destinations.

The operator is responsible for ensuring channel membership is appropriate for private repository information.

### FR5 — Message formatting

Each announcement shall normally occupy one IRC line and contain repository, event outcome, relevant subject or workflow, and a direct URL.

Examples:

```text
[widget] PR #42 merged by bob: Add retry support <url>
[widget] Issue #73 reopened: Reconnect loses messages <url>
[widget] PR #42 approved by alice <url>
[widget] CI Tests on main: success (attempt 2) <url>
```

Formatting shall remove newline, NUL, CTCP, and IRC formatting controls from untrusted fields. Truncation shall respect encoded protocol limits, account for framing, and preserve the URL where possible. [IRC protocol specification](https://modern.ircdocs.horse/).

### FR6 — Best-effort delivery and discarding

HTTP acceptance shall not imply IRC delivery.

| Condition | Required behavior |
|---|---|
| IRC and destination channel ready | Enqueue for a rate-limited send attempt |
| IRC disconnected or destination not joined | Discard that destination's announcement |
| Queue full | Discard the new announcement; do not block indefinitely |
| IRC disconnect | Discard pending announcements associated with that connection |
| Channel lost | Discard pending announcements for that channel |
| Restart or shutdown | Discard remaining pending announcements |
| Reconnection | Resume with new events only |

Discard behavior must cover both application queues and any pending library sends. Old connection objects must not publish announcements after reconnection.

Authenticated, authorized deliveries that are filtered or intentionally discarded shall receive a successful HTTP response without claiming delivery.

There shall be no automated recovery of failed GitHub deliveries. GitHub itself does not automatically redeliver failures. [Failed-delivery behavior](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries).

### FR7 — Duplicate suppression

Use a bounded, expiring, in-memory cache keyed by configured webhook identity and delivery ID to suppress ordinary duplicate deliveries.

This cache shall not be persisted. It does not provide durable replay prevention or exactly-once delivery. A repeated delivery after cache expiry or restart may be treated as new.

### FR8 — Connection lifecycle

Use verified IRC TLS. If authentication is configured, sending shall remain disabled until authentication succeeds.

Reconnect with backoff, register, and rejoin configured channels. A destination becomes eligible only after confirmed readiness. Background task failures must be supervised rather than silently terminating the relay.

## Non-Functional Requirements

- **Compatibility:** Validate the locked dependency graph on Python 3.13.
- **Responsiveness:** Respond to valid webhooks within GitHub's ten-second deadline under supported operating conditions; never wait for IRC delivery. [GitHub best practices](https://docs.github.com/en/webhooks/using-webhooks/best-practices-for-using-webhooks).
- **Performance:** Best effort; no throughput or announcement-latency SLA.
- **Resource use:** Bound request sizes, queues, duplicate caches, and concurrent processing.
- **Security:** HTTPS ingress, verified IRC TLS, separate secret files, and no secret or raw private-payload exposure in diagnostics.
- **Operation:** One long-running process. Configuration changes may require restart.
- **Instrumentation:** Detailed logging, health endpoints, and metrics remain deferred.

## Success Metrics

Release acceptance shall demonstrate:

- Correct output for every supported event and review state.
- Every workflow completion is eligible, including successes and reruns.
- No comment events produce announcements.
- Unauthorized repositories produce no output.
- No pending announcement crosses a disconnect or restart.
- Flood control and bounded queues behave predictably.
- Invalid signatures and malicious message fields are handled safely.

These are validation criteria, not a commitment to production telemetry.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| ircrobots dependency incompatibility | Validate connect, send, reconnect, and shutdown early on Python 3.13 |
| Stale library sends survive connection loss | Explicitly manage connection ownership and queued-send cancellation |
| Private information reaches an inappropriate channel | Require explicit repository authorization and destinations |
| CI activity overwhelms conversation | Apply flood control and bounded queues; retain the requested all-completions policy |
| Users assume complete delivery | Document intentional discarding and lack of catch-up |
| Oversized or hostile input affects the process | Bound ingestion and sanitize rendered fields |

## Implementation Hints

Preserve the `src/` package layout and uv tooling. Rename the existing `ghbot` distribution, package, and CLI to `umarell`, updating corresponding references.

Suggested internal boundaries are configuration, webhook verification, event normalization, routing, formatting, and IRC dispatch. Use memory-only delivery state; the existing aiosqlite dependency does not justify adding persistence.

Research identified an ircrobots/AnyIO compatibility concern and library-level send queues that require validation. [ircrobots source](https://github.com/jesopo/ircrobots/blob/v0.7.2/ircrobots/bot.py), [AnyIO migration guidance](https://anyio.readthedocs.io/en/stable/migration.html#starting-tasks).

Tests should exercise event fixtures, signature validation, private routing, queue overflow, and disconnect/restart behavior. A real IRC smoke test should verify dependency compatibility and channel readiness.

## Open Questions

- **G7 — Deferred:** Logging format, health endpoints, and metrics.
- Exact queue capacity, expiry, request-size limit, and send-rate defaults.
- Final deployment environment, IRC authentication method, and channel names.

G1–G6 and G8 are resolved. Queue and formatting behavior specified above was approved with this PRD; numeric operational defaults remain open.

## Sources & References

Three research lenses informed this document:

- **Technical patterns:** asyncio lifecycle, dependency compatibility, and temporary queues.
- **Event semantics:** workflow attempts, review decisions, and lifecycle filtering.
- **Security and failure modes:** signed requests, private routing, message sanitization, and deliberate discarding.

Primary references are linked beside the requirements they support.

## Out-of-Scope Follow-Ups

Durable delivery, missed-webhook recovery, digests, additional IRC networks, external CI, comments, GitHub API enrichment, and operational dashboards.
