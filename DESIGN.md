# Design

The whole system is one state machine. Everything else is plumbing.

```
BASE ──┬─ validate lane ─> GUARDED ─> GOLD
       └─ run lane ──────> SOLVE ───> SCORED
```

| Phase | Contents | Assertion |
|---|---|---|
| BASE | repo at `base_commit`, deps installed, history truncated, snapshot committed | the test runner executes |
| GUARDED | BASE + `test_patch` | every p2p passes, every f2p fails |
| GOLD | GUARDED + `patch.diff` | everything passes |
| SOLVE | **fresh from BASE**, description only, `--network=none` | the solver produces a diff |
| SCORED | **fresh from BASE** + solution + force-restored tests + `test_patch` | grading |

Each transition is a `docker commit`, so `task shell <run_id> --phase solve`
can enter any state the harness passed through. That one decision bought the
best observability-per-line in the project.

---

## 1. A bundle is node IDs plus a test patch

A task is a directory: `task.json`, `description.md`, `patch.diff`,
`test_patch.diff`. The guardrail tests are a **diff**, and which tests matter is
a list of **framework-native node IDs** (`tests/test_x.py::test_y`).

The obvious alternative — a `tests/f2p/` directory of test files — cannot
express the problem. SWE-bench-style datasets define the test patch as the diff
of test files between the base and instance commits, and pass-to-pass tests
*already exist in the base repo*. A directory of files has no way to say "this
existing test must keep passing." Node IDs plus a patch is not a stylistic
choice; it is the only encoding that represents the data.

**What it costs.** You cannot read the guardrail tests as files. `task
show-tests <bundle>` renders the patch and the selector lists to compensate,
and `task lint` checks the two patches do not overlap — a gold patch touching a
test path fails the bundle, because it would be smuggling in the assertions it
exists to satisfy.

A `tests/f2p/` directory is still accepted as authoring sugar and compiled into
a test patch.

---

## 2. Hidden-test protection, and the threat model

**The threat model is the solver, not the task author.** A bundle is trusted
input: it is written by whoever runs the harness. The model in the solve
container is not. Every mechanism below defends the grading signal against the
thing being graded — and none of them tries to defend against a malicious
bundle, because a malicious bundle is just a wrong task.

Five mechanisms, each closing a distinct hole:

1. **SOLVE and SCORED branch from BASE, never from GUARDED or GOLD.** Those two
   images have the guardrail test files on disk. Reusing one would hand the
   solver the hidden tests directly. This is why the phase functions take a
   `BaseResult` rather than a generic image reference — there is no parameter
   to pass the wrong thing to.
2. **Git history is truncated.** BASE prep checks out `base_commit`, removes the
   `origin` remote, deletes `.git`, and re-commits the tree as one synthetic
   root commit with a fixed author and date. The commits after `base_commit`
   contain the very fix being asked for; `git log` would otherwise hand them
   over. Re-initialising also normalizes whatever state a prebuilt image shipped
   with, and makes every later diff clean.
3. **Test files are force-restored at grading.** Before the guardrail run:
   every tracked path matching `test_path_globs` is checked out from the base
   commit, every *untracked* file matching them is deleted, and only then is
   `test_patch.diff` applied. A solver that edited, deleted, or added a test
   gains nothing.
4. **No network in the solve container.** `--network=none`. An agent with egress
   could simply fetch the upstream commit containing the fix, which would defeat
   every other measure on this list.
5. **A closed tool set.** Six tools exist: `read_file`, `write_file`,
   `list_dir`, `run_bash`, `run_tests`, `done`. There is no web search and no
   package install to disable, because those tools are never defined. Paths are
   resolved with `realpath` **inside the container** and must land under the
   repo root — resolving host-side would be checking the wrong filesystem, since
   a symlink means whatever the container says it means.

### An honest limit

Force-restore covers `test_path_globs` plus files that *only* configure the test
runner: `conftest.py`, `pytest.ini`, `jest.config.*`. It deliberately does **not**
restore `pyproject.toml`, `setup.cfg`, or `package.json`, which carry pytest
configuration *and* real dependency and packaging changes an honest fix may
need. Reverting those would break legitimate solutions in order to defend
against illegitimate ones.

So a solver can still put `[tool.pytest.ini_options] addopts = ...` in
`pyproject.toml` and have it survive grading. It gets a gaming flag, so the run
grades `resolved_suspect` — never a silent `resolved`. That is the guarantee
this design actually makes, and it is weaker than "cheating is impossible".

This limit was found by attacking the harness, not by reasoning about it. A
solver that wrote a root `conftest.py` rebinding the module under test made
every guardrail pass *for real* — `conftest.py` matched no `tests/**` glob and
is imported before test modules. The first run of that attack graded
`resolved_suspect`. Force-restore was widened in response; the residual gap
above is what remains.

---

## 3. Isolation: the agent outside, the code inside

The agent loop runs on the **host**. The code lives in the **container**. The
boundary between them is `docker exec`.

That is the isolation model, and it follows from a single conflict: the agent
needs the Anthropic API, and the workspace must have no egress. Put the network
boundary *between* them and both hold at once. An in-container agent would need
egress, and egress is the one thing that defeats hidden-test protection outright.

**Why not an egress-allowlist proxy?** It is a weaker version of the same idea
with more moving parts. An allowlist that permits `api.anthropic.com` has to be
correct about DNS, TLS interception, redirects, and every SDK retry path, and it
fails open — a hole in the allowlist is a hole in the grading signal, silently.
`--network=none` fails closed and has no configuration to get wrong.

The container also runs non-root-capable with `--pids-limit`, memory and CPU
caps, no docker socket, and a wall-clock timeout on every exec.

**What it costs.** Every tool call pays `docker exec` startup, roughly 50ms.
Irrelevant at agent-loop timescales.

---

## 4. Result classification: why `inconclusive` and `resolved_suspect` exist

Per-test status is one of `passed`, `failed`, `error`, `collection_error`,
`not_found`, `skipped`, `timeout`, `infra_error` — derived only from structured
output (pytest's junitxml) and process metadata. **Nothing is ever parsed from
stdout**, because a test that prints the word "FAILED" would otherwise be enough
to confuse grading, and a solver can print whatever it likes.

Two rules do most of the work:

**Only `passed` counts as success.** `skipped` is not a pass — otherwise a
solver satisfies a fail-to-pass test with `@pytest.mark.skip`. `not_found` is
not a pass and never a skip — a selector that produced no result means a broken
bundle or a deleted test, and treating it as neutral would let a vanished test
shrink the denominator silently.

**Failure kinds are never conflated.** `timeout` and `infra_error` are the
machine failing, not the solution. A container killed at the wall clock has said
nothing about whether the code works. Folding those into `failed` would report a
correct patch as broken because Docker ran out of memory — so any run touching
them grades **`inconclusive`**, checked *before* the pass/fail counts, because
an environment failure means the other counts cannot be trusted.

**`resolved_suspect`** exists for the opposite failure. Gaming flags — the diff
touched a test path, a test config, a CI file, or the installed framework — are
scanned from `solution.diff` after grading. They never change a pass into a
fail, because a flag is a statement about *where* the diff landed, not about
whether the code works. But a gamed pass must not be indistinguishable from an
earned one, so the outcome is downgraded and the flags render prominently.

The four outcomes are therefore: `resolved`, `resolved_suspect`, `unresolved`,
`inconclusive`. A wrapper can tell "the solution failed" from "the harness
failed" without reading any output — the same reason exit codes are distinct
(4 = baseline invalid, 5 = solver errored, 6 = grading inconclusive, 7 = no
Docker).

---

## 5. Arbitrary tasks: the adapter protocol, and its real limits

Everything framework-specific sits behind `TestAdapter`: how to prove the runner
is installed, how to build an argv for a set of selectors, and how to turn a
finished run into one outcome per requested selector.

**pytest is fully implemented.** The interesting part is mapping a node ID back
to a junit `<testcase>`. junit records `classname` and `name`, not node IDs, so
`tests/test_x.py::TestFoo::test_bar` arrives as
`classname="tests.test_x.TestFoo" name="test_bar"`. Reversing that is ambiguous
— you cannot tell where the module path ends and the class begins. So the
mapping runs *forwards*: each requested selector is translated into the pair
pytest would have written, and looked up. `not_found` then falls out naturally
rather than being a special case.

**go and jest are stubs.** They implement `smoke_argv` and share argv
construction; `parse` raises `NotImplementedError` naming this document. They
exist so the shape of multi-language support is real rather than claimed. This
is a genuine limit, not a placeholder that happens to work — the harness will
lint, build, and validate a Go bundle right up to the point of reading results,
and then stop.

Selector formats differ more than the protocol suggests, which is itself the
argument for the boundary: SWE-Bench Pro's Python rows carry real pytest node
IDs, while its JS rows carry mocha descriptors like
`test/database.js | Test database ... should return multiple keys`. Those cannot
share a parser.

The container runtime is behind a protocol too (`ContainerRuntime`, declared in
`core/` because it is what `core` *requires*). `core/` imports nothing from
`runtime/`, which is what lets the phase machine, grading, force-restore
ordering, and the path jail be tested against `FakeRuntime` in milliseconds with
no daemon. A Podman implementation would drop in unchanged; it is not written.

---

## 6. Known gaps and next steps

**Not demonstrated.** The agent loop is complete, tested against a stub
transport, and verified end to end against a real container using a scripted
cassette — but **no live model run has happened**, because no funded API key was
available. `--solver replay:<run_id>` reproduces a recorded run offline and
fails loudly on divergence; that machinery is proven, the trajectory it would
record is not. This is the single largest gap.

**Real gaps, in the order I would close them:**

1. **A live agent run**, then a committed cassette so anyone can replay the
   trajectory with no key at all.
2. **`before_repo_set_cmd` has no home in the schema.** SWE-Bench Pro ships a
   per-instance setup command (`git reset --hard <sha>; git clean -fd`). The
   current `Environment` allows exactly one of `image`/`dockerfile`/`recipe`,
   and install commands live only inside `recipe`, so an image-based environment
   that also needs setup commands cannot be expressed. The instance imported
   here does not need it; many will.
3. **`--repeat N` flake detection.** The dataset's own construction runs each
   test set three times and drops inconsistent tests. The harness runs once.
4. **`task diagnose`** — LLM-as-judge failure classification over a recorded
   trajectory, using the taxonomy from the SWE-Bench Pro paper.
5. **Real go and jest adapters.**
6. **Parallelism.** `--jobs N` is unimplemented. On Apple Silicon, where amd64
   instance images run under emulation, parallel containers contend for the same
   emulated CPU and get slower rather than faster — so this is worth less here
   than it looks.

**Smaller known rough edges.** The validation cache scans the last 200
`validation` events rather than using an index. `timings_ms.setup` reads 0
because BASE preparation happens before the run clock starts. Force-restore on a
repo like ansible checks out its entire `test/` tree (~10k files) because the
generic `test/**` fallback glob is broad — safe, but wasteful.

---

## Appendix: things that were wrong first

Kept because a design document that only describes the final state is less
useful than one that says where the edges actually are.

| What | How it surfaced |
|---|---|
| ULIDs were not monotonic within a millisecond, while the docstring promised `ORDER BY id` was chronological | a test |
| `core/phases.py` imported `runtime.base`, violating the layering rule while looking harmless | a type check |
| An unappliable gold patch exited 1 (unexpected) instead of 4 (baseline invalid) | deliberately corrupting a patch |
| A root `conftest.py` could fake every guardrail result | attacking the harness with `--solver cmd:` |
| Agent events referenced a run row written only at the *end*, so the first replay died on a foreign-key error | running it |
| The replay fingerprint compared tool-result content, which contains pytest durations and diverges every time | replaying against a real container |
| SWE-Bench Pro's prose fields are JSON-encoded; `description.md` was one unreadable line | reading the imported bundle |
| `restored_test_paths` enumerated ~10k ansible files, making the committed artifact 272KB | looking at the artifact |
