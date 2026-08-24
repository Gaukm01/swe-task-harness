# Deliverables

Everything the assignment asks for, and exactly where it lives.

| Asked for | Where |
|---|---|
| **CLI code + README with usage** | the repository — `src/harness/`, [`../README.md`](../README.md) (overview, full command reference, setup) |
| **One example task bundle that validates** | [`../examples/tiny-fixture/`](../examples/tiny-fixture/) — offline, self-contained, builds in ~16s and validates with zero API calls |
| **JSON evaluation artifact from `task run`** | [`evaluation/`](evaluation/) — three real agent runs, one JSON report + solution diff each |
| **Design notes on key tradeoffs** | [`../DESIGN.md`](../DESIGN.md) — six tradeoffs, with the threat model and honest limits |

Supporting docs: [`../ARCHITECTURE.md`](../ARCHITECTURE.md) walks the system end
to end; the full run records (junit, logs, API cassettes) are under
[`../runs/`](../runs/) and the browsable UI is
[`../site/index.html`](../site/index.html).

---

## The three evaluation runs

All three are **live agent runs** (`claude-sonnet-4-6`) on one clean database.
Open [`../site/index.html`](../site/index.html) — self-contained HTML,
double-click it.

| # | Task | Outcome | f2p fixed | p2p regressed | Turns | Cost |
|---|---|---|---|---|---|---|
| 1 | `ansible/ansible` — deterministic `no_log` secret redaction | **unresolved** | **3 / 4** | 0 / 5 | 44 | $1.96 |
| 2 | `internetarchive/openlibrary` — typed CLI arguments | **resolved** | 2 / 2 | 0 / 4 | 16 | $0.44 |
| 3 | `tiny-fixture` — interval merge | **resolved** | 2 / 2 | 0 / 4 | 5 | $0.03 |

A fourth row in `task runs` is a **replay** of run 3 — the same cassettes played
back offline, producing an identical report with **zero API calls**.

### Run 1 is the most informative, and it did not pass

The agent had to *create* the public function `sanitize_keys` from a
description. It got three of four fail-to-pass tests, broke none of the five
pass-to-pass tests, and earned no gaming flags — and the harness called it
**`unresolved`**, because 3/4 is not a fix. The per-test transitions are the
point:

```
f2p  collection_error -> passed   fixed          test_sanitize_keys_non_dict_types
f2p  collection_error -> passed   fixed          test_sanitize_keys_with_ignores
f2p            failed -> passed   fixed          test_strings_to_remove
f2p  collection_error -> failed   still_failing  test_sanitize_keys_dict
p2p            passed -> passed   held           (×5)
```

`collection_error -> passed` says the module could not even *import* at baseline
(the function did not exist) and now imports and passes — the agent genuinely
built the API. `collection_error -> failed` on the last one says it built the API
but got that case's semantics wrong. A harness reporting only a pass/fail count
could not tell you any of that.

---

## Reproducing any of it, with no API key

```bash
uv run task runs                                    # the four runs
uv run task report 01M0TTR7RH8JFM1Y1TXDBQZB1F       # a JSON evaluation artifact
uv run task log    01M0TTR7RH8JFM1Y1TXDBQZB1F       # the agent's full trajectory
uv run task run examples/tiny-fixture --solver replay:01M0TVWVEDKB8182DP39HKXD4T
```

The last command replays run 3 from its committed cassettes (`../runs/<id>/llm/`)
— it costs nothing and **fails loudly** if the prompt or tool set changed since
recording, so a replay can never quietly serve stale responses.

Check the harness itself, also with no key:

```bash
uv run task run examples/tiny-fixture --solver gold   # must be `resolved`
uv run task run examples/tiny-fixture --solver noop   # must be `unresolved`
```

## What is in `evaluation/`

Per run: `*.report.json` (the graded result — every test with its baseline
status, post status, and transition) and `*.solution.diff` (what the agent
actually changed, computed by the harness inside the container with
`git add -A && git diff --cached --binary HEAD`, never taken from the model).
The complete artifacts — junit XML, stdout/stderr, and the API cassettes — are
under `../runs/<run_id>/`.
