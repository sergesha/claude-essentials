# Verification and support limits

This is the public qualification summary for Knowledge Atlas 0.2.0. Reported
results describe the tested builds and scenarios; they are not a guarantee of
semantic accuracy or support for every host configuration. Runtime identities
and original PoC adaptations are retained in `runtime/components.json`,
`runtime/source-manifest.json`, and `tests/fixtures/manifest.json`.

## Supported and qualified targets

| Capability | Evidence | Limits |
| --- | --- | --- |
| macOS ARM64 | Native runtime checks and full Codex/Claude Code lifecycles passed | Prepared runtime; not every macOS/toolchain combination |
| Ubuntu 24.04 ARM64 | Native runtime checks and both provider lifecycles passed | Linux x86_64 and Windows are not qualified |
| Local Docker ARM64 | Fresh runtime preparation, seven native checks, provider lifecycles, archive/search replay passed | Local build; no published ready-to-use image or fresh-build guarantee for every dependency mirror |
| Host-mounted TerminusDB | Two bounded process-kill recovery cases retained prior transactions and allowed subsequent writes | Not a power-loss, disk-corruption, or complete deployment-recovery test |
| Codex and Claude Code | Native authentication, MCP attachment, ingestion, decisions, search, feedback and recovery exercised | Host attachment does not select the processing model automatically; see provider configuration |
| Local viewer | Wiki, questions, citations, review, history, diffs and graph exercised in a real browser | Single owner, authenticated localhost HTTP; no remote/multiuser qualification |

The qualified CLI versions were Codex 0.167.0 and Claude Code 2.1.319. Newer
versions require appropriate compatibility checks. Native ARM64 Docker execution
does not require Rosetta or an x86_64 emulation change.

## Regression and native checks

Publication preparation passed **188 Python tests, 32 skipped, four subtests**,
plus **28 JavaScript tests**. Both source distribution and wheel built successfully.
A simulated release version bump updated all configured version-bearing files and
passed lockfile validation without changing dependency versions. The skips are opt-in native
integration/extractor checks, not passes. Separate configured native runs supply
the platform/provider evidence above. These local checks do not establish a hosted
CI result or approval to publish.

From the package directory:

```sh
uv sync --locked
uv run pytest -q
node --test tests/viewer_*.test.mjs
uv build
```

Native prerequisites and explicit configuration are defined in
`tests/integration/conftest.py`; runtime preparation is described in
[installation](installation.md) and the [Docker guide](../docker/README.md).
Tests that require external runtimes or authenticated providers skip when those
prerequisites are absent. Do not treat an unconfigured green suite as fresh
native qualification.

## Knowledge lifecycle and persistence

Native scenarios covered source ingestion/removal, extraction, grouped type
questions, explicit acceptance/rejection, publication-selected retrieval,
application/feedback, corrections, immutable history, package composition,
archive export/restore and interrupted publication recovery. Archive replay
preserved retained document vectors without regenerating Wiki text or rerunning
document inference; fresh query inference was tested separately.

Canonical archive recovery and full deployment restoration are different
contracts. Preserve required runtime, service and credential state according to
[archives](archives.md) and [operations](operations.md). Do not assume that
regenerating knowledge with an LLM reproduces an accepted historical version.

## Native LLM Wiki quality evidence

The shared operational instruction delegates authoring to the pinned upstream
Wiki guide and native tools. Per-task guidance is supported. Real maintenance
used guide/read/search/edit/lint and reference-graph operations. It repaired
three observed source attribution or interpretation issues in a large survey.
A completed seven-page Wiki was subsequently reused exactly for downstream
extraction/alignment and publication; the earlier 13-page publication remained
available. All eight retained source parts were byte-identical.

This demonstrates native workflow use and specific corrections, not exhaustive
source review or universal quality. The maintained sample retained eight
unresolved relations, 26 type questions and six stale-page warnings. Acceptance
does not resolve these automatically. Neither page count nor lint establishes
completeness. The demonstrated glossary was absent, figures were not visually
interpreted, and the survey's referenced papers were not independently checked.

Two corrected external links were checked against actual arXiv paragraph targets.
Those verified anchors were supplied as task context; automatic precise
paragraph linking for arbitrary sources is not established. Retained local-text
paragraph navigation is not available. Citation membership validation does not
prove that every answer sentence follows from its evidence.

## Viewer and graph evidence

Real browser checks covered seven published pages, the previous 13-page version,
read-only history and return to Latest, source paragraph links, graph neighborhood
inspection, source evidence, full-text search and mobile width. No JavaScript
exceptions were observed in those checks. They are not a full accessibility audit.

An observed history response included approximately 50 MB of raw projection
artifacts. The repaired publication-history response excludes those transport
payloads while retaining complete Git publication receipts; the same two-version
timeline response measured 1,756 bytes. A regression reproduced the oversized
response before repair. Targeted read/viewer tests and the full suite passed.

The graph shows bounded local neighborhoods. Backend reads still load the
selected full projection before bounding output. Large-atlas memory, latency,
throughput, arbitrary pathfinding and graph comparison are not qualified.

## Release limitations

Current evidence supports a single-owner technical pilot. Comparative answer
quality, repeatable clean upgrades, full deployment-state restore and broad
platform support require further qualification. No hosted service, public image,
ACP integration, remote HTTPS deployment or multiuser access is claimed.
