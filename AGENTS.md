# Repository Guidelines

## Project Structure & Module Organization

`src/umarell/` contains the Python package: `config.py` validates YAML, `events.py` normalizes GitHub events, `web.py` handles authenticated webhooks, `relay.py` owns temporary delivery state, and `irc.py` integrates ircrobots. `__init__.py` implements the CLI. Tests mirror these modules under `tests/`. Configuration templates live in `examples/`; PRDs and design history live in `notes/`. Consult README for current behavior when historical designs differ.

## Build, Test, and Development Commands

Use Python 3.13+ and uv:

- `uv sync`: install locked runtime and development dependencies.
- `uv run pytest`: run the full suite.
- `uv run pytest tests/test_irc.py`: run IRC integration tests.
- `uv run ruff check .`: lint imports, correctness, and async patterns.
- `uv run ruff format .`: format Python files; use `--check` for verification.
- `uv run ty check`: check application and test types.
- `uv build`: create wheel and source distributions in `dist/`.

Validate local settings before starting:

```sh
uv run umarell --config config.yaml --check-config
```

Remove `--check-config` to run the service.

## Coding Style & Naming Conventions

Use four-space indentation, Ruff formatting, `snake_case` functions/modules, and `PascalCase` classes. Annotate function parameters and returns; narrow optional values explicitly. Keep blocking work out of asyncio tasks. Preserve ircrobots' inherited `_send_lines` and `_read_lines`; do not override them. Keep announcement pacing and expiry in the relay.

## Testing Guidelines

Use pytest and pytest-asyncio; name files `test_*.py` and functions `test_*`. Async tests run automatically. Add focused regression tests for behavior changes, including error and cancellation paths. No coverage percentage is configured. IRC tests require OpenSSL and permission to bind loopback sockets; they use temporary TLS certificates without public services or real credentials.

## Commit & Pull Request Guidelines

No commits exist yet, so there is no established message convention. Use concise imperative subjects, such as `Add IRC identity configuration`. Describe the problem, resulting behavior, linked issues, and validation commands in PRs. Run lint, format checks, types, tests, and build before review. Preserve unrelated pending changes; stage or commit only when requested.

## Security & Configuration

The configuration file contains sensitive values; use mode `0600` or `0640` and an appropriate owner/group; never commit credentials or log private payloads. Use verified IRC TLS and an HTTPS proxy for the loopback HTTP listener. Preserve explicit repository/channel allowlists and best-effort delivery without backlog replay.
