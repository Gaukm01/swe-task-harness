# swe-task-harness

A CLI that packages SWE-bench-style coding tasks into Docker containers,
validates them, runs an LLM coding agent against them, grades the result, and
records everything in SQLite with a static HTML viewer.

> **Status: milestone M5 (grading + report).** Everything except the agent
> loop (`--solver agent` / `replay`, M6), the importer (M7), and the HTML UI
> (M8). Every other command is registered with its final
> argument contract and reports the milestone that lands it. The full README
> with a quickstart arrives at M8.

## Install

```bash
uv sync
uv run task doctor
```

## Credentials (only for `--solver agent`)

```bash
cp .env.example .env          # then paste your key into it
uv run task doctor --check-api
```

`.env` is gitignored. An exported `ANTHROPIC_API_KEY` always wins over the
file. The key is read from the environment and never written to a report,
cassette, database row, or log line — `task doctor` shows only a masked
fingerprint. `gold`, `noop`, `replay`, and `cmd` make zero API calls and need
no key at all.

## What works today

```bash
uv run task lint examples/tiny-fixture   # validate a bundle, print its digest
uv run task init examples/tiny-fixture   # build + snapshot the BASE phase
uv run task validate examples/tiny-fixture   # assert the GUARDED and GOLD phases
uv run task show-tests examples/tiny-fixture # what the guardrail tests actually are
uv run task run examples/tiny-fixture --solver gold   # must grade `resolved`
uv run task run examples/tiny-fixture --solver noop   # must grade `unresolved`
uv run task log last                     # what the previous command did
```

`gold` and `noop` are the harness's own regression suite and make zero API
calls. `gold` applies the reference patch and must resolve; `noop` changes
nothing and must leave every fail-to-pass test `still_failing`.

`task init` builds the environment, truncates the repo's git history to a
single synthetic commit, checks the test runner executes, and commits the
result as `harness/<task_id>:base-<cache_key>`. It takes ~16s cold on the tiny
fixture and ~0.4s when the snapshot is cached. Editing the problem statement
does not invalidate the snapshot; editing the repo does.

Every CLI call writes a row to `harness.db` before it does any work and updates
it on exit, so a crashed command still leaves a record — one with a NULL
`ended_at`, which is itself the evidence the process died rather than exited.

## Bundle format

```
<task>/
  task.json          metadata, validated by `task lint`
  description.md     problem_statement + requirements + interface
  patch.diff         gold patch       (never shown to the agent)
  test_patch.diff    guardrail tests  (never shown to the agent)
```

`examples/tiny-fixture` is a complete worked example: a two-bug `merge()`
function with four tests that pass at base and two that do not.

## Tests

```bash
uv run pytest              # unit suite, no docker, ~3s
uv run pytest -m docker    # integration: builds a real BASE snapshot
```

The unit suite is hermetic by construction — any unmarked test that reaches the
Docker daemon fails with an assertion rather than quietly working.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | unexpected error |
| 2 | usage error |
| 3 | bundle invalid |
| 4 | baseline validation failed |
| 5 | solver failed |
| 6 | grading inconclusive (infrastructure, not the solution) |
| 7 | docker unavailable |

`task doctor` exits 7 when the container runtime is missing or unreachable, and
0 otherwise. Warnings — host emulation, tight disk, no API key — do not block:
the `gold` and `noop` solvers make zero API calls and are the harness's own
regression suite.
