# Contributing

cc-continuity is a pure-stdlib Python package: zero runtime dependencies,
hermetic tests.

## Setup

- Python >= 3.11.
- `pip install -e .` — or just run from the checkout; nothing to build.
- Tests: `python3 -m pytest -q`. The suite is hermetic: every test runs
  against a temp `CC_CONTINUITY_HOME`, no live Claude Code, no network —
  the ntfy server in the Phase 9 integration test is an in-process
  `http.server`.

## Test conventions (Phase 9)

- Patch `notify.send` (the low level) so `safe_send`'s enabled-gate and
  dedup are exercised; patch `notify.safe_send` only for helper unit
  tests.
- Use the `hookutil` helpers: `cc_home` fixture (temp
  `CC_CONTINUITY_HOME`), `make_repo`, `payload`, `fixture_payload`,
  `register_session`, `run_hook`, `set_metric`.
- `ledger.append` adds a `ts` field to every entry — pop it before
  dict-equality asserts.
- The `cc-continuity signal` / `request-rotation` CLIs resolve the
  session via the `CC_CONTINUITY_SESSION` env var first; set it in
  in-process CLI tests (`monkeypatch.setenv`).
- `tests/test_hygiene.py` keeps the public tree free of personal
  endpoints, topics, paths and keys — keep new tests fixture-based
  (the generic fixture users in that file show the pattern).

## Workflow

- Small, focused PRs; the full suite must pass (`python3 -m pytest -q`).
- No runtime dependencies: stdlib only (pytest is a test dependency).
- New config keys need an entry in `config.example.json` and a line in
  the configuration documentation.
