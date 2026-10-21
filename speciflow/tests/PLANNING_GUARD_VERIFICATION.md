# Planning guard verification — 2026-10-21

## Result and purpose

The plugin catches routine direct edits of initialized Backlog.md product data
and active OpenSpec specifications, blocks the tool call and reminds the agent
to use SpeciFlow and the native owner's route. Reads, native CLI, unrelated work
and change authoring pass silently. The guard associates each recognized write
with its own target; it does not classify permissible read methods.

The user approved this design after rejecting the earlier implementations.
Those attempts are not compatibility requirements. One frozen rejected copy
is used solely for causal RED/GREEN comparison. The goal is process guidance
on ordinary agent writes, not complete filesystem access control or detection
of exotic bypasses.

## Exact final RED → GREEN

The coordinator independently ran the same final tests against the frozen
before hook and final runtime. Test-file hashes and all 412 test identities
were identical across both runs. Actual Backlog 1.52.0 and OpenSpec 1.13.1
were enabled; no native integration tests were skipped.

| Runtime | Passed | Failed | Errors | Skipped |
| --- | ---: | ---: | ---: | ---: |
| Rejected before hook | 327 | 85 | 0 | 0 |
| Final target-based hook | 412 | 0 | 0 | 0 |

All 85 observed failures became passes. Improvements include ordinary Python
readers and text processing; reading protected data and writing a draft in one
program; harmless quoted paths/operators; copying or moving unrelated files
into the project directory; and protection of direct Path writes and copies
whose destination was previously misidentified. Unknown programs pass without
being scored as prevented mutations.

The suite also executes real native Backlog edits and OpenSpec archive for new
and modified active requirements, structured editor/patch operations in both
host schemas, and paired actual file-effect controls: unguarded operations
change temporary protected data, while denied calls preserve the original.

[Machine-readable results and test hashes](verification-results.json).
Coordinator raw logs and JUnit XML are under
`/private/tmp/speciflow-target-mutation-rewrite/coordinator-{red,green}.{log,xml}`.
The frozen before runtime SHA256 is
`2a07a8932a2f98a99df2a2fe543a64f0e42756f3f1135c93911a37627a8249dc`.
Final runtime SHA256 is
`b3882964ab13c931fcab4a1eb42083225b4b7655deeec9e732446f12847e4fb6`.

## Independent review

Task review found four ordinary-operation defects: stderr descriptor handling
in copy commands, actual child destinations for moves into directories, quoted
heredoc-like text, and grouped in-place sed flags. New behavior tests reproduced
22 failures before the narrow fixes and passed all 30 cases afterward.
Independent re-review passed both specification compliance and code quality,
verified the runtime hash and reran those 30 tests. Runtime remains one standard
library file; the read recognizer and exception lists were removed.

Review artifacts: `task-review.md`, `re-review.md` and
`coordinator-fix-review.patch` under the evidence root above.

## Actual host acceptance

Fresh sessions used Claude Code 2.1.304 and Codex 0.163.0 with existing default
models and the final frozen runtime. Native registration/validation, installed
runtime and plugin/reference hashes matched the frozen source.

| Host | Successful quiet controls | Direct-write cases denied with reminder | Protected bytes preserved |
| --- | ---: | ---: | --- |
| Claude Code | 4 original + 1 corrected loop | 4/4 | Yes |
| Codex | 4 original + 1 corrected loop | 4/4 | Yes |

The original loop command used zsh's special `path` variable and failed exit127
in both hosts after the hook correctly allowed it. Both failures remain in the
raw eighteen-case run. An authorized protocol addendum changed only that loop
variable to `read_file`; the two supplemental actual-tool calls succeeded
silently. The original failures are not silently rescored or hidden.

Two separate sessions tested autonomous reaction after a forced ordinary direct
edit. Both hosts delivered the actual denial/reminder and preserved active
specifications and Backlog. Their continuations differ:

- Claude wrote the delta specification, design and tasks under `changes/**`,
  then completed native strict validation/status. Its two initial Edit calls
  were queued in one generation, not retries after feedback.
- Codex made successful native OpenSpec specs/design/tasks instructions calls
  after denial. It reached the fixed 120-second deadline without writing a
  completed draft. This proves observed return to native change preparation;
  draft completion and complete SpeciFlow compliance are not claimed.

Neither trace showed a distinct-generation active-write retry after feedback.
These illustrative trials show feedback and subsequent actions, not a natural
error-rate estimate. No archive was performed by the agents; actual native
archive is covered by the automated integration tests.

Raw prompts, calls, feedback, deadline offsets, hashes and line-pointer audit:
`/private/tmp/speciflow-target-mutation-rewrite/host-evidence/run2/REPORT.md`
and `summary.json`. The earlier run1 was cancelled when the reviewed runtime
changed; its partial evidence is retained unscored. Scoped cleanup confirmed
no surviving test processes, no temporary authentication links, original
credentials retained and stable protected hashes after quiescence. No user
plugin installation or persisted hook-trust change was made.

## Reproduction

From the repository root:

```sh
SPECIFLOW_TEST_BACKLOG=/absolute/path/to/backlog SPECIFLOW_TEST_OPENSPEC=/absolute/path/to/openspec speciflow/.venv/bin/python -m pytest speciflow/tests -q
```

For the before comparison, set `SPECIFLOW_TEST_HOOK` to the frozen before script
and use the same final test files. Without native CLI variables, integration
cases skip explicitly. The source tests define the concrete covered operations.
A permitted native operation does not prove every SpeciFlow step ran.
