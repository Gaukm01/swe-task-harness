# swe-task-harness

A CLI that packages SWE-bench-style coding tasks into Docker containers,
validates them, runs an LLM coding agent against them, grades the result, and
records everything in SQLite with a static HTML viewer.

> **Status: milestone M1 (skeleton).** Only `task doctor` is implemented. Every
> other command is registered with its final argument contract and reports the
> milestone that lands it. The full README with a quickstart arrives at M8.

## Install

```bash
uv sync
uv run task doctor
```

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
