# Deliverables

Everything the assignment asks for, and where it lives.

| asked for | here |
|---|---|
| CLI code + README with usage | repository root: `src/harness/`, [`../README.md`](../README.md) |
| One example task bundle that validates | [`../examples/tiny-fixture/`](../examples/tiny-fixture/) — offline, builds in ~16s |
| Evaluation artifact from `task run` | [`evaluation/`](evaluation/) — three real runs, JSON per run |
| Design notes on key tradeoffs | [`DESIGN-NOTES.md`](DESIGN-NOTES.md) — six sections |

---

## The three runs

All three are **live agent runs** against a real LLM, on one clean database.
Browsable at [`../site/index.html`](../site/index.html) — self-contained HTML,
double-click it.

| # | task | outcome | f2p fixed | turns | cost |
|---|---|---|---|---|---|
| 1 | `ansible/ansible` — `no_log` secret redaction | **unresolved** | **3/4** | 44 | $1.9550 |
| 2 | `internetarchive/openlibrary` — typed CLI args | **resolved** | 2/2 | 16 | $0.4404 |
| 3 | `tiny-fixture` — interval merge | **resolved** | 2/2 | 5 | $0.0325 |
| | | | | | **$2.4279** |

A fourth row in `task runs` is a **replay** of run 3 — the same cassettes
played back offline, producing an identical report with **zero API calls**.

### Run 1 is the most informative, and it did not pass

The agent had to *create* `sanitize_keys` in a 2,740-line file. It got three of
four fail-to-pass tests, broke nothing, and earned no gaming flags — and the
harness called it `unresolved`, because 3/4 is not a fix.

The per-test detail is the point:

```
f2p  collection_error -> passed   fixed          test_sanitize_keys_non_dict_types
f2p  collection_error -> passed   fixed          test_sanitize_keys_with_ignores
f2p            failed -> passed   fixed          test_strings_to_remove
f2p  collection_error -> failed   still_failing  test_sanitize_keys_dict
p2p            passed -> passed   held           (×5)
```

`collection_error -> failed` says something precise: the module could not even
import at baseline (the function did not exist), and now it imports and runs —
so the agent genuinely built the API, and got one case's semantics wrong. A
harness that reported only pass/fail counts could not tell you that.

---

## Reproducing any of it, with no API key

```bash
uv run task runs                                    # the four runs
uv run task report <run_id>                         # the JSON evaluation artifact
uv run task log <run_id>                            # the agent's full trajectory
uv run task run examples/tiny-fixture --solver replay:01M0TVWVEDKB8182DP39HKXD4T
```

The last command replays a recorded run from its committed cassettes
(`runs/<id>/llm/`). It costs nothing and fails loudly if the prompt or tool set
has changed since recording — a replay that quietly served stale responses would
make every artifact here a fiction.

Zero-API-call solvers for checking the harness itself:

```bash
uv run task run examples/tiny-fixture --solver gold   # must be `resolved`
uv run task run examples/tiny-fixture --solver noop   # must be `unresolved`
```

---

## What is in `evaluation/`

Per run: `*.report.json` (the graded result — every test with its baseline
status, post status, and transition) and `*.solution.diff` (what the agent
actually changed, computed by the harness from the container with
`git add -A && git diff --cached --binary HEAD`, never taken from the model).

Full artifacts including junit XML, stdout/stderr and API cassettes are under
`../runs/<run_id>/`.
