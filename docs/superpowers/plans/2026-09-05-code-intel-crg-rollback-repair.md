# CRG Indexed Rollback Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile CRG after an already indexed tracked edit or deletion is restored/reset, before the dispatcher publishes another successful freshness marker.

**Architecture:** Extend the checkout marker with versioned Git candidate history and select an explicit CRG diff base inside the existing root transaction. A disappeared candidate triggers `update` against an empty Git tree, which reaches CRG's existing per-file hash checks without invoking a full build or discarding surviving indexed untracked files. Both lifecycle readiness and explicit updates share this selection; diagnostics only observe state.

**Tech Stack:** Python standard library, Git, CodeGraph 1.6.0, code-review-graph 2.3.8, unittest, SQLite read-only integration assertions.

**Spec:** `docs/superpowers/specs/2026-09-05-code-intel-plugin-design.md`, narrowed to indexed restore/reset freshness and its direct regressions.

## Global Constraints

- `scripts/code_intel.py` remains Python-standard-library-only.
- Exact dependency pins remain `npm:@colbymchenry/codegraph@1.6.0` and `pipx:code-review-graph@2.3.8`.
- All child processes receive argument arrays without a shell; paths are never interpolated into shell commands.
- Every non-install child and MCP launch sets `CODEGRAPH_NO_DOWNLOAD=1`, `NODE_DISABLE_COMPILE_CACHE=1`, and `CODEGRAPH_INSTALL_DIR` to `os.devnull`.
- Every `PostToolUse` Bash event with a resolvable Git worktree is relevant and forces synchronization, even with unchanged `HEAD` and a clean working tree.
- CRG updates remain incremental. `setup-project --force` retains its existing explicit build semantics.
- Explicit update commands never initialize missing indexes.
- State and lock files live only under the selected plugin data directory. The canonical worktree root remains the key.
- Keep the existing root lock, pending/failed publication, finite overall deadline, descendant termination/reaping, and final checkout/index revalidation.
- `doctor` and `project-status` remain read-only, including Git objects/index, plugin storage, and lock files.
- Do not edit marketplace files, release metadata, manifests, other plugins, global configuration, or dependency installations.
- Do not import CRG's internal Python API, infer a pipx interpreter, copy its parser, or change its installed package.

## Established cause and tested repair

Read these diagnosis artifacts before implementation:

- `/private/tmp/code-intel-sandbox-e2e-report.md`
- `/private/tmp/crg-engine-diagnosis.eWqUFX/RESULTS.md`

CRG 2.3.8 derives changed paths from `git diff --name-status -z BASE --`, where its automatic base is graph metadata's last recorded commit. Restoring an indexed dirty file to that commit removes the file from this diff. Its stale-file reconciliation checks existence and eligibility, not changed contents; the normal no-change return happens before its per-file hash comparison. An indexed deletion followed by restoration has the inverse failure: the file exists again, but is not selected for parsing.

The following CLI-only experiment passed on 2026-09-05 using Git 2.55.0 and installed CRG 2.3.8. Evidence is `/private/tmp/crg-cli-rollback.Vf7I0U/probe.py` and `commands.json`; disposable databases remain under its `sha1/` and `sha256/` directories. Production source was not changed by this experiment.

| Independent database check | SHA-1 | SHA-256 |
| --- | --- | --- |
| Ordinary update still contains `ghost` after restoring A while B remains dirty | Reproduced | Reproduced |
| Empty-tree update removes `ghost` from nodes and FTS, retaining B's dirty symbol | PASS | PASS |
| An indexed tracked deletion remains missing after ordinary update following restoration | Reproduced | Reproduced |
| Empty-tree update restores that file's function | PASS | PASS |
| Same-HEAD `reset --hard HEAD` is reconciled | PASS | PASS |
| Previously indexed, now untracked file and exact `untracked_only` node row survive | PASS | PASS |
| All final file hashes equal independently calculated SHA-256 of disk contents | PASS | PASS |

Exact repair argv, executed with `cwd=root` and `timeout=remaining(deadline)` through `run_child`:

```python
empty_tree = run_child(
    ["git", "hash-object", "-t", "tree", "-w", "--stdin"],
    cwd=root, timeout=remaining(deadline), input_text="",
).stdout.strip()
run_child(
    [str(tools["crg"]), "update", "--base", empty_tree,
     "--skip-flows", "--repo", str(root)],
    cwd=root, timeout=remaining(deadline),
)
```

Git must materialize the object using `-w`; calculating a hash without storing its tree does not establish that CRG can use the base. Validate the returned object ID as lowercase hexadecimal with the same 40/64-character length as the captured HEAD. Do not hardcode SHA-1 in production. The observed IDs were `4b825dc642cb6eb9a060e54bf8d69288fbee4904` and `6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321` respectively. This writes one immutable empty object in the fixture/repository object database; it does not modify refs, files, or the staging index. Run it only inside a mutating transaction, after pending state publication.

This operation supplies all current tracked paths to CRG's incremental hash checks; it is not `build`. Existing eligible indexed untracked files are retained by the engine's incremental reconciliation. This is a preservation guarantee for this repair, not a claim that default CRG discovery indexes new untracked files.

CLI exit zero retains the existing subprocess-success contract. It does not certify parser completeness or semantic graph health. The independent database assertions below prove the particular rollback cases; generic parser/error-output interpretation, warnings policy, and postprocessor validation are outside this change.

## Marker and selection contract

Extend `FreshnessMarker` with trailing fields so existing positional pending-marker constructors continue working:

```python
schema_version: int = 2
crg_candidates: list[str] | None = None
```

`crg_candidates` is an observation of Git's candidate paths at the successful snapshot, not an assertion that each path has graph nodes. For a v2 success containing CRG it is required, sorted, unique, and may be empty. It is `null` for a CodeGraph-only umbrella and may be `null` for pending/failed markers.

Define `C(base, worktree)` by running the following read-only Git query and decoding NUL records:

```python
["git", "--no-optional-locks", "diff", "--name-status", "-z", base, "--"]
```

This has the same path-selection semantics as native CRG's Git query. `--no-optional-locks` prevents diagnostic captures from refreshing Git's index on disk. Honor the repository's Git rename/copy settings. Every normal status contributes one path; `R<score>` and `C<score>` contribute both old and new paths. Include deleted paths and staged additions. Do not use porcelain status, `--name-only`, newline parsing, an extension allowlist, or a tracked/untracked union; genuinely untracked paths are absent. Do not filter generated/configuration paths out of this candidate list: it describes the native query before CRG's own eligibility filters.

Recover raw bytes from `run_child` stdout with UTF-8/surrogateescape, split NUL records, then use `os.fsdecode` as CRG does. Sort by `os.fsencode` for deterministic byte ordering. Reject truncated records, unrecognized status shapes, empty paths, absolute paths, NULs, or `.`/`..` path components with `UserError`. Preserve tabs, newlines, carriage returns, spaces, and undecodable bytes in legitimate relative filenames. JSON's escaped string representation round-trips those paths. Do not silently treat malformed/failed/timed-out discovery as an empty candidate set.

At successful capture store `C(captured_HEAD, captured_worktree)`. At the next update use the **previous marker HEAD** as the ordinary update base and compute `C(previous.head, current_worktree)`. Always pass that same explicit base to the ordinary native update; otherwise the predicate would reason about a different candidate set from the engine's metadata-selected base.

The selection predicate is:

```python
def needs_crg_repair(previous, candidates_from_previous_head, *, crg_rebuilt):
    if crg_rebuilt:
        return False
    if (previous is None or previous.schema_version != 2
            or previous.status != "success"
            or "crg" not in previous.versions
            or previous.crg_candidates is None):
        return True
    return bool(set(previous.crg_candidates) - set(candidates_from_previous_head))
```

Only apply this predicate when CRG is selected and its index exists. A newly initialized CRG index or a CRG index rebuilt by explicit `--force` uses the existing normal update once, without a second broad refresh. A missing explicit-update index still errors before any initialization or empty-tree command.

For existing CRG with trusted v2 history, ordinary argv is:

```python
[str(tools["crg"]), "update", "--base", previous.head,
 "--skip-flows", "--repo", str(root)]
```

For CRG just built in this transaction, preserve the existing argv:

```python
[str(tools["crg"]), "update", "--skip-flows", "--repo", str(root)]
```

If there is no usable success history, the existing index gets one empty-tree update. This is also the recovery path after a failed/pending operation, since a writer might already have changed some graph files. If discovery against a previously valid HEAD now fails (including an unavailable Git object), fail the current operation and publish failed state; the next operation can take this known recovery path. Never accept native CRG's silent diff fallback as evidence that the intended base was used.

A valid legacy marker has exactly the existing six fields, with all existing type/root/success checks satisfied. Read it as `schema_version=1, crg_candidates=None`; do not trust or rewrite it from status. A subsequent successful mutation upgrades it to v2 after repair when CRG exists. Unknown versions, unexpected fields, and invalid v2 candidate data are corrupt, retain the existing fail-open/no-implicit-repair behavior, and are never treated as legacy. Do not preserve stale candidate history in pending/failed markers.

The new candidate snapshot participates in stability checks, marker equality, and read-only status. Git index-only changes can change candidates without changing file-content fingerprints. Capture candidates twice around the existing checkout/index passes, and compare candidate snapshots from before synchronization with the final observation using the same captured current HEAD. A changed candidate set, HEAD, checkout, index, failed engine, state error, or timeout prevents success. Keep the final `ensure_ready` revalidation after prompt-hook and before lock release.

This selection uses successful plugin history. The existing generic index-fingerprint mismatch path still forces synchronization; it does not claim to reconstruct unobserved worktree histories created entirely by external engine writers between plugin observations. Do not broaden this repair into a semantic auditor for external writers.

## File responsibilities

- Modify `code-intel/scripts/code_intel.py`: marker validation/capture; a small NUL candidate decoder/query; shared base selection; CRG argv; integration into `ensure_ready` and `mutate_project`.
- Modify `code-intel/tests/test_code_intel.py`: marker/candidate boundary tests, lifecycle/explicit-update routing tests, opt-in real-CRG installed-layout regressions. Reuse `stage_installed_copy` from `test_packaging.py`; add no distributable files.
- Read, do not modify, `code-intel/tests/test_packaging.py`: existing installed-layout staging and package gates.
- Do not split the controller or alter packaging/CI just to implement this repair.

### Task 1: Reconcile omitted rollback candidates and prove the lifecycle contract

**Interfaces:**

- Keep `run_child`, `root_lock`, `capture_checkout`, `handle_hook`, public CLI names, and the six original marker fields.
- Add `crg_candidate_paths(root: Path, base: str, deadline: float) -> list[str]` for the strict read-only query above.
- Add `needs_crg_repair(previous: FreshnessMarker | None, candidates_from_previous_head: list[str] | None, *, crg_rebuilt: bool) -> bool` with the exact predicate above. `None` for current candidates is permitted only when no usable previous history exists or CRG was just built.
- Add `select_crg_base(root: Path, previous: FreshnessMarker | None, *, crg_rebuilt: bool, deadline: float) -> str | None`; returns the stored HEAD, materialized empty-tree OID, or `None` for a fresh build. Skip it entirely for CodeGraph-only roots.
- Add optional keyword `crg_base: str | None = None` to `update_indexes_locked`; when provided, append `--base` and its value immediately after `update`. The function keeps CodeGraph-first execution and missing-index checks.
- Have `initialize_indexes_locked` return `set[str]` naming the engines it actually initialized/rebuilt. Existing callers ignoring the return stay valid; callers orchestrating readiness use membership of `"crg"` to suppress redundant repair.

- [ ] **Step 1: Add real-engine failing regressions before controller edits.**

Break caught: the dispatcher runs CRG successfully but an already restored tracked file is omitted from its candidate set. A command-count-only fake would miss this bug; use real CRG and independently inspect SQLite after supervised processes exit.

Append `RealCRGRollbackTests(ControllerCase)` in `test_code_intel.py`. In its `setUp`, require opt-in `CODE_INTEL_REAL_CRG`; skip with an explicit reason if absent, fail if a supplied executable is not version 2.3.8. Set fixture-local `CRG_HOME` before launching any CRG child, including `--version`, so its registry and other home state cannot reach the user's installation. Stage the complete installed-layout copy using the existing helper. Put an executable wrapper/symlink named `code-review-graph` in a temporary PATH directory pointing to that supplied executable, and a tiny CodeGraph boundary fake there which returns `1.6.0`, creates its local index on `init`, accepts `sync`, and returns empty stdout for `prompt-hook`. This keeps the engine under repair real; the unrelated engine's native behavior is already covered elsewhere.

Use these concrete setup/helpers and assertion shape (class members use the existing `ControllerCase` fixture and `executable` helper):

```python
def setUp(self):
    super().setUp()
    selected = os.environ.get("CODE_INTEL_REAL_CRG")
    if not selected:
        self.skipTest("set CODE_INTEL_REAL_CRG to the installed pinned 2.3.8 executable")
    selected = str(Path(selected).absolute())  # Preserve a supplied mise shim's name.
    self.repo = self.repo.resolve()
    from test_packaging import stage_installed_copy
    self.installed_root = stage_installed_copy(self.base)
    directory = self.base / "real-crg-bin"
    self.executable(directory, "code-review-graph",
        f"import os, sys\nos.execv({selected!r}, [{selected!r}, *sys.argv[1:]])\n")
    self.executable(directory, "codegraph",
        "import sys\nfrom pathlib import Path\n"
        "if sys.argv[1:] == ['--version']: print('1.6.0')\n"
        "elif sys.argv[1] in ('init', 'index'): (Path.cwd() / '.codegraph').mkdir(exist_ok=True)\n"
        "elif sys.argv[1] not in ('sync', 'prompt-hook'): sys.exit(9)\n")
    env = patch.dict(os.environ, {
        "PATH": str(directory) + os.pathsep + os.environ["PATH"],
        "CRG_HOME": str(self.base / "crg-home"),
        "CRG_DATA_DIR": "", "CRG_SERIAL_PARSE": "1",
        "XDG_CONFIG_HOME": str(self.base / "xdg-config"),
        "XDG_DATA_HOME": str(self.base / "xdg-data"),
        "XDG_CACHE_HOME": str(self.base / "xdg-cache"),
        "XDG_STATE_HOME": str(self.base / "xdg-state"),
    })
    env.start()
    self.addCleanup(env.stop)
    version = self.module.run_child([selected, "--version"], cwd=self.repo, timeout=15)
    self.assertEqual(version.stdout.strip(), "code-review-graph 2.3.8")

def installed(self, command, *, payload=None):
    args = [sys.executable, "-B", str(self.installed_root / "scripts/code_intel.py"), command]
    if command in ("update-project", "project-status"):
        args.append(str(self.repo))
    outer_timeout = {
        "hook-status": 75, "hook-prompt": 75, "hook-update": 75,
        "update-project": 330, "project-status": 330,
    }[command]
    return self.module.run_child(
        args, cwd=self.repo, timeout=outer_timeout,
        input_text=None if payload is None else json.dumps(payload),
    )

def hook_sync(self, command="hook-update"):
    result = self.installed(command, payload={
        "cwd": str(self.repo), "tool_name": "Bash", "prompt": "",
    })
    self.assertNotIn("Code intelligence unavailable", result.stdout)
    return result

def sql(self, statement, args=()):
    database = self.repo / ".code-review-graph/graph.db"
    wal = database.with_name(database.name + "-wal")
    self.assertFalse(wal.exists() and wal.stat().st_size,
                     "immutable SQL fixture would ignore nonempty WAL")
    with contextlib.closing(sqlite3.connect(
            database.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        return connection.execute(statement, args).fetchall()

def count_symbol(self, name):
    return self.sql("SELECT COUNT(*) FROM nodes WHERE name = ?", (name,))[0][0]

def test_installed_partial_restore_removes_ghost(self):
    (self.repo / "a.py").write_text("def stable_a():\n    return 1\n")
    (self.repo / "b.py").write_text("def stable_b():\n    return 2\n")
    git(self.repo, "add", "a.py", "b.py")
    git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "rollback fixture")
    self.hook_sync("hook-status")
    before_head = git(self.repo, "rev-parse", "HEAD")
    (self.repo / "a.py").write_text("def stable_a():\n    return 1\n\ndef ghost():\n    return 9\n")
    (self.repo / "b.py").write_text("def stable_b():\n    return 2\n\ndef dirty_b():\n    return 8\n")
    self.hook_sync()
    self.assertEqual(self.count_symbol("ghost"), 1)
    self.assertEqual(self.count_symbol("dirty_b"), 1)
    git(self.repo, "restore", "--", "a.py")
    self.hook_sync()
    self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before_head)
    self.assertEqual(self.count_symbol("ghost"), 0)
    self.assertEqual(self.sql("SELECT COUNT(*) FROM nodes_fts WHERE name = ?", ("ghost",)), [(0,)])
    self.assertEqual(self.count_symbol("dirty_b"), 1)
    self.assertEqual(self.sql("SELECT DISTINCT file_hash FROM nodes WHERE file_path = ?",
                             (str(self.repo / "a.py"),)),
                     [(hashlib.sha256((self.repo / "a.py").read_bytes()).hexdigest(),)])
    report = json.loads(self.installed("project-status").stdout)
    self.assertIs(report["healthy"], True)
```

The installed-process outer deadlines deliberately exceed the product deadlines: 75 seconds for a hook allows its 45-second inner budget plus a 30-second margin; 330 seconds for explicit update allows its 300-second inner budget plus the same margin. The margin covers interpreter startup, the inner supervisor's descendant cleanup, and response serialization before the outer supervisor can intervene. Read-only `project-status` also receives the 330-second outer cap. These are test-harness limits only; do not alter product deadlines or shorten an outer limit to match its inner budget.

Keep the remaining real cases in the same class, using the same installed CLI and SQL helpers. These are literal fixtures and expected outcomes, not source-text assertions:

| Test method | Arrange / operation | Required independent result |
| --- | --- | --- |
| `test_installed_full_restore_and_reset` | For each fresh fixture: index `ghost` in A, then `git restore -- a.py` or `git reset --hard HEAD`; invoke Bash hook | Precondition ghost count 1; final nodes/FTS counts 0, stable A count 1, HEAD unchanged, file hash current |
| `test_installed_restored_tracked_deletion` | Commit A with `stable_a`; initialize; `git rm a.py`; hook; `git restore --source=HEAD --staged --worktree -- a.py`; explicit `update-project` | Count 1 before delete, 0 after delete hook, 1 after restore/update; current file hash |
| `test_installed_restore_survives_branch_switch` | Create another branch changing only B; return; index dirty A; restore A; switch to other branch; prompt hook with empty prompt | Ghost 0; other branch's B symbol 1; previous branch's B-only symbol 0 |
| `test_installed_repair_preserves_indexed_untracked` | Create `local.py` with `untracked_only`; temporarily stage it and run native CRG update to index it; unstage with `git reset -- local.py`; record its full node row; then run the partial restore fixture | Exact row for `untracked_only` unchanged; file still untracked and byte-identical; ghost 0 |
| `test_installed_sha256_restore` | Create a separate temporary repo with `git init --object-format=sha256`, make `self.repo` refer to it, run same partial restore sequence | 64-character HEAD; same SQL results. Skip only when Git rejects SHA-256 initialization |

Before immutable SQL reads, all writers must have exited and there must be no nonempty `graph.db-wal`; otherwise fail the fixture with a diagnostic instead of ignoring committed WAL contents. Keep fixture-specific environment overrides local to the test, including plugin data, `CRG_HOME`, `CRG_DATA_DIR` if needed to ensure root-local storage, and XDG directories; do not copy credentials or install tools. Treat a staged-installed copy as installed-layout verification, not a new marketplace installation.

- [ ] **Step 2: Run the red real regression and preserve its result.**

```sh
CODE_INTEL_REAL_CRG="$(command -v code-review-graph)" python3 -B code-intel/tests/test_code_intel.py RealCRGRollbackTests.test_installed_partial_restore_removes_ghost -v
```

Expected before production changes: FAIL at the independent `ghost` count (`1 != 0`), after successful dirty-index preconditions and successful hook execution. If fixture setup errors or an earlier assertion fails, fix the fixture first. Do not accept skip as the red result when the tested installed engine is available.

- [ ] **Step 3: Add focused Git candidate and marker compatibility tests.**

Breaks caught: missing rename source, deletion omitted, newline-aliased paths, index-only changes missed, or a marker without history treated as current. Exercise actual Git through the new helper; feed malformed NUL output only at the subprocess boundary.

```python
def test_candidates_keep_deleted_and_both_rename_paths(self):
    (self.repo / "old name.py").write_text("def keep():\n    return 1\n")
    git(self.repo, "add", "old name.py")
    git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "rename fixture")
    head = git(self.repo, "rev-parse", "HEAD")
    git(self.repo, "mv", "old name.py", "new\nname.py")
    git(self.repo, "rm", "source.py")
    (self.repo / "untracked.py").write_text("value = 1\n")
    self.assertEqual(self.module.crg_candidate_paths(self.repo, head, time.monotonic() + 10),
                     ["new\nname.py", "old name.py", "source.py"])
```

Add literal boundary vectors for copy `C100\0old.py\0copy.py\0`, modified `M\0tab\tname.py\0`, deleted `D\0gone.py\0`, an empty successful diff, and raw `b"M\0raw-\xff.py\0"`; expected candidate lists must be hand-written. Truncated `R100\0old.py\0`, missing terminal NUL, empty path, absolute path, and `../escape.py` must raise. Make normal/error outputs distinct so malformed discovery cannot satisfy the empty-diff test.

For marker tests, write a valid existing six-field success fixture and assert read-only observation reports unhealthy without changing its bytes; mutation then upgrades it to v2 through broad repair. Round-trip v2 paths including surrogateescape. Reject version 3, boolean version, duplicate/unsorted paths, non-string paths, extra fields, invalid roots, and `null` candidates on a v2 CRG success. Keep CodeGraph-only success with `null` candidates valid. Preserve old corrupt-state tests and their no-write assertions.

Put the candidate cases in `CRGCandidateTests(ControllerCase)`. Run `python3 -B code-intel/tests/test_code_intel.py StateTests FingerprintTests CRGCandidateTests -v`; verify new tests fail for missing candidate/schema behavior before implementation.

- [ ] **Step 4: Implement the minimal marker/candidate observation changes.**

Implement the exact schema, legacy rules, NUL query, and stability capture described above. Validate stored HEAD before using it as a Git argument. Use the same snapshot representation before/after synchronization; keep the existing `capture_checkout` public return shape intact.

The core capture order is:

```python
checkout = capture_checkout(root, deadline)
candidates = crg_candidate_paths(root, checkout[0], deadline) if "crg" in tools else None
fingerprints = {name: index_fingerprint(root, name, deadline) for name in sorted(tools)}
if checkout != capture_checkout(root, deadline):
    raise UserError("Checkout changed during index capture.")
if candidates != (crg_candidate_paths(root, checkout[0], deadline) if "crg" in tools else None):
    raise UserError("Git candidates changed during index capture.")
```

Retain the existing second index-fingerprint comparison and final checkout comparison around this addition. Return v2 markers with current-head candidates. Update existing intentionally valid successful CRG marker fixtures to provide explicit `crg_candidates=[]` or their hand-derived dirty paths; keep intentionally incomplete-success fixtures incomplete. Pending positional constructors and CodeGraph-only markers keep `None`. Re-run Step 3's tests until green; do not remove the legacy marker's stale-status assertion to make schema validation pass.

- [ ] **Step 5: Add failing selection/lifecycle tests with literal argv expectations.**

Put these cases in `RollbackReadinessTests(HookCase)`. Use the existing `HookCase` fake executables only at the external CLI boundary. Git, marker persistence, locking, dispatcher branches, and file mutations remain real. Read the already existing event log and extract CRG update argv using `[row[1] for row in map(json.loads, self.events.read_text().splitlines()) if row[1][0] == "update"]`; assert that this list contains exactly the one expected argv for a synchronization. Assertions on argv are warranted here because the bug is a wrong external command contract.

For example, after `warm()`, index dirty A and B through a real `hook-update`, restore A, clear the event log, and execute another Bash hook:

```python
crg_update_argv = [row[1] for row in map(json.loads, self.events.read_text().splitlines())
                   if row[1][0] == "update"]
self.assertEqual(crg_update_argv, [[
    "update", "--base", "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
    "--skip-flows", "--repo", str(self.repo.resolve()),
]])
self.assertEqual(self.marker().crg_candidates, ["b.py"])
self.assertEqual(self.marker().status, "success")
```

Use a committed A/B fixture, not only `source.py`, and the following table as independent input/expectation cases. Each case gets a fresh successful marker unless its row says otherwise:

| Prior indexed state → current state | Expected CRG base / effect |
| --- | --- |
| Clean → ordinary edit A | Previous HEAD; no build and no empty-tree write |
| Dirty A → another edit A | Previous HEAD; no broad repair |
| Dirty A+B → restore A, B still dirty | Empty tree |
| Dirty A → same-HEAD restore or reset | Empty tree |
| Indexed deletion A → restore A | Empty tree |
| Clean → clean branch switch changing B | Previous HEAD; new marker's candidates relative to new HEAD are empty |
| Dirty A → restore A + branch switch changing only B | Empty tree even though HEAD changed and B is a candidate |
| Dirty A → branch switch changing A to a different committed content | Previous HEAD because A is still a candidate |
| Indexed dirty A → commit A without changing its bytes | Previous HEAD; no unnecessary broad repair |
| Matching clean success → session/prompt | No index update; prompt may run its adapter |
| Matching clean success → Bash hook | One ordinary update with previous HEAD |
| Missing/valid legacy/failed/pending history, CRG exists | One empty-tree update, then v2 success |
| CRG missing → lifecycle initialization | One CRG build, one normal update, no empty-tree write |
| Existing CRG → explicit setup `--force` | Existing forced build, one normal update, no additional repair |
| Explicit update with missing CRG | Nonzero; no initialization or empty-tree write |
| CodeGraph-only umbrella | No CRG candidate query, empty-tree object, or CRG process |

Parameterize the partial-restore transition over `hook-status`, `hook-prompt`, `hook-update` with `Bash`, `hook-update` with `Write`, `update-project`, and `update-batch` containing the repository. Each must select the same repair and publish a current v2 marker. A prompt case must not invoke `prompt-hook` before repair completes. Run the new class before integrating selection; the current ordinary-argv behavior must fail the repair rows.

- [ ] **Step 6: Integrate base selection into the existing transaction.**

Implement `needs_crg_repair` exactly as specified. `select_crg_base` computes current candidates against the previous HEAD only for a valid v2 CRG success; otherwise it materializes the empty tree unless `crg_rebuilt=True`. Run all its child commands with the shared deadline. Check the returned OID, and propagate errors rather than silently dropping `--base`.

Thread the selected base through both `mutate_project` and `ensure_ready`. Read and retain `previous` before pending publication. For `ensure_ready`, keep the matching-marker reuse check before any mutation; legacy and candidate changes cannot reuse. For explicit update, verify required indexes before selecting a base, preserving its no-initialization contract.

The shared ordering, with existing error handling around it, is:

```python
write_marker(root, data, pending)
before = capture_checkout(root, deadline)
before_candidates = crg_candidate_paths(root, before[0], deadline) if "crg" in tools else None
rebuilt = initialize_indexes_locked(root, tools, force=force, deadline=deadline) if do_setup else set()
crg_base = select_crg_base(root, previous, crg_rebuilt="crg" in rebuilt, deadline=deadline) if "crg" in tools else None
update_indexes_locked(root, tools, crg_base=crg_base, deadline=deadline)
observed = capture(root, tools, deadline)
if before != capture_checkout(root, deadline) or before_candidates != observed.crg_candidates:
    raise UserError("Checkout or Git candidates changed during indexing.")
```

Here `do_setup` is true for lifecycle initialization and explicit setup, false for explicit update; `force` is the existing explicit setup flag and false in lifecycle paths. Compute candidate decisions after pending publication, but compare the before/after candidates as shown so a staging-only change cannot be stamped as the successfully indexed input. Preserve all existing final revalidation and failure invalidation; do not introduce an early success write in `ensure_ready` before yielded prompt work finishes.

- [ ] **Step 7: Add repair-specific failure and read-only regressions before handling any newly exposed gap.**

Extend the existing CRG executable fixture with a mode that fails only on `update --base <empty-tree>`, and a separate mode that starts a writer descendant and blocks. Keep CodeGraph success to prove a later CRG failure invalidates readiness. Do not change successful fake behavior to manufacture semantic assertions.

```python
response = self.hook("hook-prompt")
self.assert_fallback(response)
self.assertEqual(self.marker().status, "failed")
self.assertNotIn("prompt-hook", self.commands())
```

Apply those exact observable assertions to CRG repair exit 9, repair timeout, failed candidate discovery, malformed empty-tree stdout, and empty-tree Git failure. On explicit update assert nonzero instead of hook JSON. After removing a transient failure, retry and assert empty-tree repair is used and success is published. For timeout use the existing process-group test utilities to prove the descendant has exited before another root operation proceeds; reuse the existing finite-deadline machinery rather than increasing the 45-second hook budget.

Inject a real file or HEAD change at the repair child boundary and assert failed state. Separately stage an existing untracked file during synchronization, leaving its bytes unchanged: the new candidate recheck must reject that snapshot. Preserve lock-deadline behavior: a contender must leave the current owner's marker untouched.

For status/doctor, snapshot the entire disposable checkout including `.git`, both index directories, and plugin data; invoke diagnostics after rollback and with a legacy marker, and assert unchanged snapshots and unhealthy output. Repeat after successful repair and assert unchanged snapshots and healthy output. Verify no `hash-object -w`, CRG update/build, lock creation, or marker upgrade occurs in diagnostics; the filesystem snapshot is the primary proof.

- [ ] **Step 8: Run all focused real-engine cases and required existing checks.**

```sh
CODE_INTEL_REAL_CRG="$(command -v code-review-graph)" python3 -B code-intel/tests/test_code_intel.py RealCRGRollbackTests -v
python3 -B code-intel/tests/test_code_intel.py -v
python3 -B code-intel/tests/test_packaging.py -v
git diff --check
```

Expected: the original red ghost assertion and all restored-deletion/hash/FTS/untracked/SHA-256 assertions pass; existing state, read-only doctor, tool arguments, timeout, worktree-isolation, setup/update, and packaging checks pass. Existing optional CodeGraph-specific checks may skip when their documented launcher variable is absent; report those skips, and do not describe fake CodeGraph coverage as real two-engine E2E. Do not install CRG or modify CI in this task. If a supplied real CRG path is available, its regressions must execute rather than skip.

Review the diff against these mutations: remove `--base`; use current HEAD instead of previous HEAD on a branch switch; drop deleted or rename-source candidates; treat failed/legacy history as empty trusted history; publish marker before repair; run empty-tree refresh immediately after first build; and invoke repair from doctor. At least one behavioral test above must fail for each mutation. Do not add generic parser-exception injection or native stdout error parsing for this change.

- [ ] **Step 9: Commit the cohesive implementation and its tests.**

```sh
git add code-intel/scripts/code_intel.py code-intel/tests/test_code_intel.py
git commit -m "fix(code-intel): repair CRG after indexed restore and reset"
git status --short
```

The implementation handoff must report the real-engine red/green evidence, SHA formats exercised, remaining optional skips, and the native-exit-code limitation. This plan does not itself claim the product defect is fixed.
