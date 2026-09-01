# Task 2B recipe-authority report

## Scope and threat-model disposition

This task implements the A-07 admission boundary for the native runtime path.
It does not add a graph compiler: yamlgraph remains the sole YAML-to-LangGraph
compiler, and only `recipe/yamlgraph_adapter.py` imports yamlgraph/LangGraph.

The Local unsandboxed single-user MVP threat model is explicit about the
authority being granted. A reviewed Python or shell definition is full
`os_user_execution`, not constrained execution. Untrusted recipe edit authority
cannot create that grant. The policy is TCB-owned configuration and binds:

- the digest of the complete canonical recipe definition (root plus all
  transitive supported sources);
- the digest of the complete executable tool descriptor and every static use
  coordinate in the dependency DAG;
- an explicit `os_user_execution` authority label; and
- for Python, an exact owner-reviewed installed Lockstep module/function pair.

The module prefix is not an allowlist. For example,
`lockstep.runtime.validators.run_checks` is denied unless that exact callable,
requirement digest, and definition digest are all present in owner policy. The
trusted Lockstep installation identity is part of the local MVP TCB. A future
constrained-runner profile will need a real isolation boundary and separate
runtime effect grants; this definition-time grant does not claim either.

## Exact yamlgraph 0.5.22 authority inventory

The installed and locked version was independently inspected and confirmed as
`yamlgraph==0.5.22`. The inventory below was derived from its installed source,
not from Lockstep's former partial resolver.

| yamlgraph surface | Phase and authority | Task 2B disposition |
|---|---|---|
| Root graph path, `compile.graph_loader.load_graph_config` | Compile-time YAML read | Root is strict-decoded and compiled only from canonical immutable materialization |
| `nodes.*.type=subgraph.graph`, including `models.relay_fields` | Recursive compile-time YAML reads and relay schema read | Supported; every direct/transitive path is resolved once by strict ingress and represented in `ValidatedDependencyDAG` |
| `tools.*.type=graph.path` | Recursive child read and compile | Rejected as an unsupported loader-retained directive |
| top-level `data_files`, including glob patterns | Compile-time graph-relative reads/glob expansion | Rejected |
| `tools.*.manifest` and manifest runtime `path`/graph paths | Compile-time manifest read followed by Python/graph expansion | Rejected |
| Python tool `module`/`function` | Import and attribute resolution during `compile_graph` | Requires exact definition grant plus exact installed module/function registry entry |
| Python tool `path` | Compile-time file read and `exec_module` | Rejected |
| shell tool command/cwd/env/timeout/parse/success codes | Runtime `subprocess.run(shell=True)` | Complete canonical descriptor requires exact full-OS definition grant |
| prompt name/files, `prompts_dir`, `prompts_relative` | Compile-time schema reads for output models and runtime prompt reads | Prompt-bearing LLM/router/agent/copilot/race/map/pipeline/interactive nodes and prompt directives are rejected |
| dynamic output model module/class | Compile-time dynamic import | Rejected with its node/profile |
| `schema_loader` path/schema_dir/paths-from-state | Runtime filesystem reads | Rejected |
| `write_data_file` | Runtime graph-root filesystem writes | Rejected |
| LLM/router/agent/tool_call/map/race/pipeline/interactive/copilot | Provider, dynamic tool, graph expansion, subprocess, or prompt authority | Rejected as unsupported node kinds |
| top-level recipe `checkpointer` | Backend/resource selection | Rejected; persistence is engine-owned |
| observability/route-log configuration | Process-global logging state and file output | Rejected by the closed top-level schema |
| source maps and other `x-*` metadata | yamlgraph 0.5.22 does not retain/read these during graph compilation | Only the currently defined `x-lockstep-generated` metadata is admitted; future source-map inputs must be explicitly added to the dependency schema before use |

CLI export, discovery, diary, benchmark, worktree, and standalone FSM utilities
also contain filesystem/process operations, but `compile_graph` does not dispatch
to them from any admitted recipe construct. No Lockstep production path calls
those yamlgraph utility surfaces.

The supported filesystem profile is therefore intentionally complete and small:
the root plus recursive `subgraph.graph` files. It is not a partial imitation of
yamlgraph's loader. Every other identified loader-retained path construct fails
closed before yamlgraph is imported or called.

## Implemented boundary

`StrictRecipeIngress` holds a source-root descriptor and performs bounded UTF-8
event scanning before object construction. It rejects aliases, anchors, explicit
tags, duplicate/non-string keys, YAML 1.1 coercion ambiguities, non-finite or
out-of-range JSON values, unsafe paths, links/special files, cycles, and limit
excesses for source bytes, per-file bytes, file count, depth, YAML nodes,
container cardinality, scalar bytes, and integers.

The decoded domain is closed over the admitted yamlgraph profile. Authority-
bearing fields have both closed key sets and value-domain validation. Canonical
sorted compact JSON plus a trailing newline is used as valid unambiguous YAML.
The definition digest covers canonical root identity and every sorted
`{path, sha256, size}` entry. `RecipeLoader` now uses this same ingress and binds
`RecipeRef` to the complete definition digest.

Admission produces a typed `ValidatedDependencyDAG`. `AuthorizedRecipe.capture`
stages only the already canonical bytes, and `RecipeBundleStore` descriptor-
safely captures exactly that DAG. Compilation and validation accept the same
read-only materialization returned by the content-addressed bundle store; changes
or deletion in the live recipe tree do not affect it.

The MCP `validate_recipe` external ingress no longer calls raw yamlgraph
validation. It strict-decodes first, applies the default-deny executable policy,
materializes canonical bytes in an ephemeral owner store, applies the existing
Lockstep semantic profile to those frozen bytes, and only then calls
`validate_native`. Rejection performs no yamlgraph compile/import, process
launch, catalog/checkpoint write, or persistent owner-state mutation.

## Legacy cutover boundary

The old `Engine` cannot preserve its arbitrary-command recipes while satisfying
the new authority model. No compatibility bypass was added. Its existing raw
compiler remains temporarily reachable only through explicitly named
`legacy_compile_recipe` and `legacy_validate_recipe` functions. The generic raw
`compile_recipe` and `validate` adapter names no longer exist, and regression
tests make that boundary visible.

This commit must not be described as converting legacy Engine execution to the
new authority path. Task 3 is the atomic release gate: it removes/disables the
legacy raw start/check/resume path and routes all public execution through the
immutable authority bundle and native runtime. Until that cutover, the legacy
Engine retains its pre-existing trusted-recipe execution contract and is not a
released implementation of Recipe Authority. Moving the clean native seam
forward was chosen over inserting a second staging/validation/compiler path into
the legacy Engine that Task 3 immediately deletes.

## TDD and negative evidence

RED was observed for each new class of behavior, including:

- missing strict loader; duplicate keys, aliases, ambiguous scalars, unknown
  fields, malformed authority values, and every configured hard limit;
- incomplete recursive DAG, shared-child false cycle, real cycle, traversal,
  absolute paths, symlink/special-file inputs, and unsupported retained loaders;
- missing exact grants, wrong recipe/requirement digests, missing/wrong exact
  Python registry target, and recipe mutation invalidating an existing grant;
- live source mutation/deletion after capture;
- raw `Path` accepted by the native adapter;
- `RecipeRef` accepting a changed transitive definition;
- MCP validation reaching Python import-time code before authority rejection.

Negative tests assert rejection occurs before bundle publication or durable
state creation. The MCP import witness uses a module whose import writes a
sentinel; the sentinel and owner-state directory remain absent.

## Independent review and stop rule

The independent design reviewer first recommended a strict native artifact path
and Task 3 atomic legacy cutover. Its code review found the real MCP raw-validation
bypass, which was fixed. It initially described public Python dataclasses as
forgeable capabilities. On required threat-model re-review, that was reclassified
as defense-in-depth/API misuse: no untrusted boundary receives these in-process
TCB objects, and forging them requires arbitrary Python/reflection already inside
the orchestration TCB. Adding a fake seal would violate the stop rule without
reducing a reachable authority delta. Runtime type checks and documentation keep
the intended internal API explicit.

The same re-review confirmed that native run/effect coordinates do not yet exist
in Task 2B. The definition digest and exact descriptor/provenance are appropriate
for definition-time full-OS compile/import authorization; coordinate/generation,
actor, expiry, policy epoch, and dynamic checkpoint provenance belong to Task 3's
runtime effect grant.

The final independent threat-model re-review reported **ZERO confirmed P1/P2
blockers** and found no retained-read, symlink, artifact-binding, or raw-path
regression in the new native authority path.

## Verification

Commands were run from `engine/` unless noted:

- Focused authority/loader/native/bundle/server/legacy-boundary tests:
  `97 passed in 3.39s`.
- Full suite excluding the dependency-patch module:
  `656 passed in 133.62s`.
- Full dependency-patch module outside the filesystem sandbox with
  `UV_OFFLINE=1`: `25 passed in 20.42s`.
  Together these cover the complete current suite: `681 passed`, with no
  network access.
- Ruff full configured check on all new/substantially rewritten Task 2B files:
  `All checks passed!`.
- Ruff `F,I` integration check on the pre-existing legacy/integration files
  touched only at call sites: `All checks passed!`.
- `git diff --check`: clean.
