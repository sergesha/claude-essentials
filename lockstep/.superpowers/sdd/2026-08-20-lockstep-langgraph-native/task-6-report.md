# Task 6 real Codex managed-effect vertical report

## Scope and threat-model disposition

Task 6 implements the real Codex `managed` runner behind the Task 5
provider-neutral boundary. `EffectRequest` and `EffectResult` contain no Codex
session, argv, environment, credential, workspace-path, or spool fields.
LangGraph remains the sole workflow/checkpoint authority; the coordinator and
effect ledger were extended only with provider-neutral definitive-rejection and
quarantine proof semantics. No workflow state store, artifact registry,
temporary artifact persistence, publication path, or remote reconciliation path
was added.

The selected profile remains `Local unsandboxed single-user`. The exact runner
requires the explicit full `os_user_execution` authority. Codex input, output,
and reasoning are untrusted protocol data, but the process is TCB for ambient
OS-user authority. Sandbox evidence therefore records requested Codex mechanics
honestly and does not claim Constrained-runner isolation. Per SI-23 and the stop
rule, lifecycle supervision is correctness/recovery machinery, not confinement
against a malicious `setsid` or double-fork escape.

## RED contracts and managed launch

The RED commit `bf2a8d5` established exact request/launch binding, argv arrays
without a shell, owner-captured permission profile, sanitized environment and
closed FDs, deadline and launcher-decision fencing, lookup/adoption, sandbox
evidence, bounded capture, workspace containment, rollover, quiescence, and
cleanup. Subsequent review rounds added deterministic RED cases for:

- ready-supervisor adoption crossing the decision fence;
- exact supervisor liveness and terminal receipt publication;
- executable and credential rotation at the inner spawn boundary;
- prepare crash recovery and retained-attempt quotas;
- orphan workspace recovery, executable files, empty directories, and VCS/PATH
  mutation;
- artifact-bearing and non-managed requests that Task 6 cannot yet represent;
- deadline/cancel checks adjacent to the inner `Popen`;
- stored-PGID reuse, immediate child stdin closure, delayed process-group death,
  and capture-thread completion ordering; and
- definitive prelaunch rejection and invalid-output quarantine through durable
  coordinator sealing.

The production adapter commits the absolute Codex executable identity, model,
CLI version, fixed `workspace-write`/`never` permission mechanics, dedicated
owner-only `CODEX_HOME` credential identity and audience, sanitized environment,
exact workspace, exact argv, deadline, sandbox evidence, and launcher generation.
The detached supervisor reads bounded stdin, revalidates executable and
credential identity, performs the final deadline/cancel check, then immediately
uses `Popen` with `shell=False`, `close_fds=True`, and a new process session.

## Workspace, result, and recovery semantics

`LocalGitWorkspaceProvider` materializes Task 2 sealed snapshots into a bounded
portable project tree and creates the minimal unborn Git control structure
in-process, without ambient `PATH`, subprocesses, or Git helpers. One shared
`PortableProjectPath` and `ProjectTreeLimits` boundary now drives snapshot,
manifest, and workspace admission. Materialization recovers exact digest-keyed
orphan staging directories under the workspace record lock.

Rollover first durably renames the checkout into quarantine, then verifies the
complete Git control tree, portable paths, symlink/traversal/outside-root rules,
declared writes, file/aggregate ceilings, content-only snapshot fidelity, and a
before/copy/after capture. Executable outputs and empty directories fail closed
because Task 2 snapshots cannot reproduce them. Accepted output becomes one
immutable successor snapshot and the quarantine is released. Rejected output
becomes a bounded `writes_invalid` terminal result and stays durably quarantined
without exposing a successor snapshot.

Preparation is idempotent across a crash after immutable launch publication.
Adoption remains inside the current decision/deadline fence. Cancellation writes
an owner-state request consumed by the exact live supervisor; the adapter never
signals a persisted, potentially reused PGID. The supervisor repeatedly kills
and checks its own trusted process group with exponential backoff capped at the
owner-selected ten seconds. It retains its liveness lock until actual group
death, joins stdout/stderr capture threads to completion, publishes the immutable
terminal receipt, and only then releases liveness. Broken stdin, output overflow,
deadline, spawn failure, and invalid result paths all produce bounded terminal
facts rather than retry loops.

Artifact-bearing runner effects fail closed before provider request construction
until Task 10 introduces the single provider-neutral `ArtifactRegistry` contract.

## Real smoke and review

The integration smoke instantiates the production `CodexRunnerAdapter` and
exercises the exact managed chain:

`prepare → supervisor → codex exec → terminal → quarantine/rollover → snapshot → release`

It uses only a disposable managed Git project. Absence of the Codex CLI, or of a
dedicated owner-captured credential home/model, is an explicit prerequisite skip.
The test no longer substitutes a separate `codex sandbox` command or profile for
the production vertical. In this environment it skipped because the dedicated
credential/model prerequisites were absent; no network call was made.

The final independent current-hash review returned **ZERO** findings:
Critical 0, Important 0, Minor 0. It closed all nine original full-review
findings plus the later deadline, cancellation, deterministic-failure,
quarantine, BrokenPipe, delayed-death, and capture-stability findings. Its
architecture/simplification audit found no duplicated workflow authority,
canonical state, artifact persistence, provider leakage, or premature generalized
framework. The workspace/path/snapshot production surface is net smaller than
the initial workspace implementation after consolidation.

## Verification

- Final focused providers/effects/snapshot/manifest/integration gate:
  `188 passed, 1 skipped`.
- Independent final relevant suite: `201 passed, 1 skipped`.
- Six final lifecycle/capture regressions: passed.
- Ruff over every changed Task 6 source and test file: clean.
- `compileall` over runtime and affected tests: clean.
- `git diff --check`: clean.
- The complete-suite environment still cannot execute two unchanged dependency
  packaging tests because their nested `uv sync`/`uv build` attempts require an
  unavailable cached `hatchling` or network access. The last external full run
  before the final one-test lifecycle fix reported `629 passed, 3 failed,
  1 skipped`; the sole Task 6 RED was then fixed and independently verified, and
  the other two failures remain the same offline packaging prerequisites.

No dependency mutation, push, network operation, publication, fork, or GitHub
operation was performed.
