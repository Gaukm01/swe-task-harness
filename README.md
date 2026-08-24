# swe-task-harness

A CLI that packages SWE-bench-style coding tasks into Docker containers,
validates them, runs an LLM coding agent against them, grades the result, and
records everything in SQLite with a static HTML viewer.

## Quickstart

```bash
uv sync                                                # install
uv run task doctor                                     # docker, disk, arch, credentials
uv run task lint     examples/tiny-fixture             # validate the bundle
uv run task init     examples/tiny-fixture             # build + snapshot the BASE phase
uv run task validate examples/tiny-fixture             # assert p2p pass, f2p fail, gold fixes
uv run task run      examples/tiny-fixture --solver gold   # must grade `resolved`
uv run task run      examples/tiny-fixture --solver noop   # must grade `unresolved`
uv run task runs                                       # what has been run
uv run task ui && open site/index.html                 # browsable results
```

That is the whole loop, and it makes **zero API calls**. `gold` and `noop` are
the harness's own regression suite: `gold` applies the reference patch and must
resolve, `noop` changes nothing and must leave every fail-to-pass test
`still_failing`. If either ever disagrees, the harness is broken.

## A real SWE-Bench Pro instance

```bash
uv run task import --survey 200 --language python      # rank candidates by size
uv run task import --instance-id instance_ansible__ansible-12734fa...
uv run task run examples/swebench-pro/<task> --solver gold
```

`artifacts/example-run.json` is a committed report from exactly this: a real
`ansible/ansible` instance where `to_yaml` on an undefined Jinja variable raised
a cryptic `RepresenterError`. Gold grades `resolved` (1/1 fail-to-pass fixed, 0/4
regressions); noop grades `unresolved`. `artifacts/example-run.html` is the
rendered page.

## Running an agent

```bash
cp .env.example .env                    # paste ANTHROPIC_API_KEY into it
uv run task doctor --check-api          # verifies the key; bills nothing
uv run task run examples/tiny-fixture --solver agent --max-turns 40 --max-cost-usd 1.00
uv run task run examples/tiny-fixture --solver replay:<run_id>   # offline, free
```

Every API exchange is recorded to `runs/<id>/llm/NNN.json`. `replay` plays them
back with no key at all and fails loudly if the prompt or tool set has changed
since recording — a silently-wrong replay would make every downstream artifact a
fiction.

> **Status.** Every phase, solver, and report path is implemented and tested.
> **No live model run has been performed** — no funded API key was available —
> so the agent trajectory is the one thing not demonstrated. See DESIGN.md §6.

## Inspecting a run

```bash
uv run task log <invocation_id>                  # what a command did
uv run task report <run_id>                      # the JSON report
uv run task report <run_id> --format html
uv run task shell  <run_id> --phase solve        # a shell inside that snapshot
uv run task show-tests <bundle>                  # what the guardrail tests are
```

Every phase transition is a `docker commit`, so `task shell` can enter any state
the harness passed through — including a failed one.

## Bundle format

```
<task>/
  task.json          metadata, validated by `task lint`
  description.md     problem_statement + requirements + interface
  patch.diff         gold patch       (never shown to the agent)
  test_patch.diff    guardrail tests  (never shown to the agent)
```

`examples/tiny-fixture` is a complete worked example: a `merge()` with two real
bugs, four tests that pass at base and two that do not.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok — including `unresolved`, which is a result, not an error |
| 1 | unexpected error |
| 2 | usage error |
| 3 | bundle invalid |
| 4 | baseline validation failed |
| 5 | solver failed |
| 6 | grading inconclusive (infrastructure, not the solution) — raised after artifacts are written |
| 7 | docker unavailable |

A caller can tell "this bundle is broken" from "this solver failed" from "the
machine failed" without parsing any output.

## Tests

```bash
uv run pytest              # unit suite, no docker, ~4s
uv run pytest -m docker    # integration: real containers
```

The unit suite is hermetic by construction — any unmarked test that reaches the
Docker daemon fails with an assertion rather than quietly working. No test ever
calls the Anthropic API.

## Layout

```
src/harness/
  cli/        Typer commands, Rich rendering, error formatting
  core/       phase machine, grading, classification, models — NO docker imports
  runtime/    DockerRuntime (subprocess argv) and FakeRuntime
  adapters/   pytest (full); go/jest stubs behind the same protocol
  solvers/    gold, noop, agent, replay, cmd — one Solver protocol
  store/      schema.sql + queries
  report/     json + jinja2 html
  importers/  swebench_pro
```

`core/` takes the runtime as a protocol parameter and imports nothing from
`runtime/`. That is what makes phase ordering, grading, force-restore, and the
path jail testable in milliseconds without a daemon.

See **DESIGN.md** for the tradeoffs, the threat model, and what is knowingly
incomplete.
