# Design notes

Distilled from ~2,400 lines of working notes. Six tradeoffs, each stating what
was chosen, what it cost, and what is still open.

---

## 1. A task is node IDs plus a test patch — not a directory of test files

A bundle is `task.json` + `description.md` + `patch.diff` + `test_patch.diff`.
Which tests matter is a list of framework-native node IDs
(`tests/test_x.py::test_y`); the tests themselves arrive as a **diff**.

The obvious alternative — a `tests/f2p/` directory — cannot express the problem.
SWE-bench-style datasets define the test patch as the diff of test files between
two commits, and **pass-to-pass tests already exist in the base repo**. A
directory of files has no way to say "this existing test must keep passing".

**Cost:** you cannot read the guardrail tests as files. `task show-tests`
renders the patch and selectors to compensate, and `task lint` rejects a bundle
whose gold patch touches a test path (it would be smuggling in the assertions it
exists to satisfy) or whose test patch reaches outside `test_path_globs` (those
edits would be silently dropped by force-restore at grading).

---

## 2. The whole system is one state machine

```
BASE ──┬─ validate lane ─> GUARDED ─> GOLD
       └─ run lane ──────> SOLVE ───> SCORED
```

BASE is the repo at `base_commit` with dependencies installed, `origin` removed,
and git history **truncated to a single synthetic commit**. GUARDED adds the
test patch and asserts f2p fails / p2p passes. GOLD adds the reference patch and
asserts everything passes — proving the task is solvable *before* any solver is
blamed for failing it.

**SOLVE and SCORED each branch fresh from BASE, never from GUARDED or GOLD**,
which carry the guardrail tests on disk. The phase functions take a
`BaseResult`, so there is no parameter through which the wrong image can be
passed. Grading compares GUARDED (baseline) against SCORED (post); GOLD never
touches a solver's score.

**Cost:** more containers and more disk than a single mutable workspace. Bought:
every phase is a `docker commit`, so `task shell <run_id> --phase solve` can
enter any state the harness passed through, including a failed one.

---

## 3. The agent runs outside; the code runs inside

The agent loop runs on the **host**. The repository lives in the **container**.
`docker exec` is the boundary.

This follows from one conflict: the agent needs the Anthropic API, and the
workspace must have no egress. Put the network boundary *between* them and both
hold. An in-container agent would need egress — and egress defeats everything
else, because the upstream commit containing the fix is one `git clone` away.

**Why not an egress-allowlist proxy?** It fails *open*. A hole in the allowlist
is a silent hole in every grade you have published. `--network=none` fails
closed and has nothing to misconfigure.

Six tools exist and nothing else does — `read_file`, `write_file`, `list_dir`,
`run_bash`, `run_tests`, `done`. There is no web search to disable because it is
never defined. Paths are resolved with `realpath` **inside the container** (a
symlink means whatever the container says it means) and must land under the repo
root. Writes to test paths are refused — which fired for real: on the
openlibrary run the model tried to edit the test file and was blocked.

**What is deliberately *not* hidden:** the pass-to-pass tests. They are ordinary
pre-existing repo tests, indistinguishable from unlabelled ones, and removing
them would hand the agent a repo that no longer matches `base_commit`. We hide
the grading **manifest**, not the test **suite**. An earlier version had
`run_tests` refuse graded selectors; that was removed because it was an oracle —
probing one id at a time and watching for the refusal enumerated the whole
graded set.

---

## 4. Failure kinds are never conflated

Eight per-test statuses (`passed`, `failed`, `error`, `collection_error`,
`not_found`, `skipped`, `timeout`, `infra_error`), all derived from **junit XML
plus process metadata**. Nothing is parsed from stdout: a test that prints
"FAILED" must not be able to confuse grading.

Two rules carry the design. **Only `passed` is success** — `skipped` is not
(else `@pytest.mark.skip` satisfies a fail-to-pass test) and `not_found` is not
(else a deleted test silently shrinks the denominator). And **`inconclusive` is
checked first**: a timeout or a container failure says nothing about the
solution, so reporting `unresolved` off a half-run suite would blame a correct
patch for a broken machine.

Four outcomes — `resolved`, `resolved_suspect`, `unresolved`, `inconclusive` —
and six exit codes, so a caller distinguishes *the bundle is wrong* (4) from
*the solver failed* (5) from *the machine failed* (6) without parsing output.
`resolved_suspect` exists so that a pass earned by editing test configuration is
never silently indistinguishable from an honest one.

---

## 5. Results integrity: mitigated, not solved

The junit file grading reads is written **inside the container the solver's code
runs in** — unavoidably, because pytest must import the solution to measure it.
An `atexit` hook in ordinary source therefore runs *after* pytest writes results
and *before* the harness reads them. That produced a `resolved` verdict, zero
flags, from a solver that never touched the function under test.

Four layers now, honestly ranked:

| layer | defeated by |
|---|---|
| results path is nonced, outside the repo and `/tmp` | `os.walk("/")` |
| junit verdict cross-checked against process exit code | `os._exit(0)` |
| suspicious source flagged in the diff | literals assembled at runtime |
| **integrity canary** — a harness test that must report `failed` | a *surgical* forge only |

The canary sits inside the guardrail tests' own directory (so it cannot be
skipped by path) and carries a per-run nonce (so it cannot be recognised without
already knowing the hidden selector names). Every blanket forgery — delete all
`<failure>`, set `failures=0`, replace the document — flips or drops it, and the
run grades `inconclusive`.

**This is a reduction, not a proof.** Evading the canary requires the graded
selector names, which are not in the container — so the attack is reduced to,
and no stronger than, the hidden-test protection. Anyone running untrusted code
inside the process that measures it inherits this class of problem. The real fix
is an append-only results channel consumed as it is produced; that is next work,
not done.

---

## 6. Layering, and what it bought

`core/` contains the phase machine, grading, classification, and the path jail —
and **imports nothing from `runtime/`**. The `ContainerRuntime` protocol is
declared *in* `core`, because it expresses what core requires; Docker is one
detail that satisfies it.

That single rule is why 287 unit tests run in ~5 seconds with no daemon,
covering failure paths that are painful to provoke for real: OOM, wall-clock
timeout, a commit failing on a full disk, a forged results file. Thirteen docker
integration tests cover the rest. No test ever calls the Anthropic API —
`StubTransport` is a peer implementation of the transport protocol, not a mock,
which is also why a recorded run replays forever at zero cost.

**Test names are the specification:**
`test_solve_branches_from_base_not_from_guarded`,
`test_force_restore_happens_before_the_test_patch_is_applied`,
`test_a_failing_baseline_aborts_before_the_solver_runs`,
`test_a_solver_cannot_forge_the_results_file`.

---

## Known limits

1. **Results forgery is mitigated, not closed** (§5).
2. **`pyproject.toml` / `setup.cfg` are not force-restored.** They carry real
   dependency changes an honest fix may need, so reverting them would break
   legitimate solutions. pytest config placed there survives grading — flagged,
   so `resolved_suspect`, never a silent pass.
3. **go and jest adapters are stubs.** Smoke probe and argv construction work;
   `parse` raises. The harness will lint, build and validate a Go bundle right
   up to reading results, then stop.
4. **`before_repo_set_cmd`** from SWE-Bench Pro has no home in the schema.
5. **No parallelism.** Under emulation, parallel containers contend for the same
   emulated CPU anyway.
6. **Not every dataset instance is runnable** — truncated node IDs, tests
   needing a display. Both are classified correctly (exit 4 vs 6) rather than
   papered over.
