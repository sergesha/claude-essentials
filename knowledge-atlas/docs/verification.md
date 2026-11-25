# Knowledge Atlas verification

## Atlas image release and recovery

The Atlas image jobs run when Release Please creates an Atlas release from
`main`. The publishing job builds the tagged commit on a hosted Linux ARM64
runner. It checks the packaged application, scans the candidate with the
shared plugin image security gate and verifies the scanned config ID again
immediately before pushing the version tag to GHCR. The pipeline then
compares the registry's platform config ID with the candidate it scanned.

The publishing job retains its scan and publication artifacts. The clean
verification job pulls the recorded manifest digest anonymously, checks OCI
metadata and authenticated application status, and runs the native
multi-project test on that exact ARM64 image. Review both jobs and their
artifacts before treating the image as delivered. Native host installation and
provider-account qualification have separate evidence.

On first publication, the package owner sets the new package's visibility to
Public in [GitHub Packages settings](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).
If anonymous pull failed while the package was private, rerun the failed
verification job from the original release workflow. GitHub preserves the
successful publisher's output digest. A retry of the publisher itself verifies
an existing version against the publication receipt from the same run and
preserves that digest. A conflict, missing receipt or unreadable registry
response stops publication for maintainer investigation.

When the matching image remains unavailable, follow the
[explicit local build instructions](../docker/README.md) or
[native installation](installation.md). For a qualified public release, use
the versioned image and recorded digest with the Docker instructions.
The verification helper accepts the recorded digest reference directly.

```sh
python3 knowledge-atlas/scripts/verify-release-image.py \
  --image "ghcr.io/sergesha/knowledge-atlas@$ATLAS_DIGEST" \
  --version "$ATLAS_VERSION" --revision "$ATLAS_REVISION"
```

## Atlas 0.3.1 patch qualification

The Linux ARM64 package run used the locked Python environment and pinned Utopia extractor.
The 0.3.1 run passed 267 tests and 11 subtests, with 11 opt-in integrations skipped.
Separate native fixtures exercised Wiki reconciliation, Gemma embeddings, TerminusDB
historical reads, crash recovery, and format-compatible archive replay. The viewer suite
passed 29 JavaScript tests. A Chromium session checked caller-supplied attribution,
receipt indicators and an abstention with a working citation using a deterministic
local backend.

The 0.3.1 ARM64 production image ran as UID 1000. With an empty authentication volume
and a disposable copy of the 0.3.0 runtime, it migrated the interpreter, validated the
runtime and served authenticated Atlas and graph requests. The rehearsal started Gemma,
the embedding bridge and TerminusDB offline. Both packaged provider CLIs launched.
Native model invocation is a separate opt-in check.

A separate Linux ARM64 run published a pin-only parent at the configured Docker atlas path
using an external child and retrieved child evidence through hybrid search. Opening the
external child as a second owner returned `invalid_location` and preserved the parent’s
published and graph commits.

A separately exported 0.3.0 format-2 archive with mixed-case decision UUIDs restored both
decisions. A dedicated regression preserves its original case-sensitive inventory order.
The fresh macOS ARM64 Utopia build passed the package suite with the extractor enabled.

The regression fixtures cover stale candidates, out-of-order acceptance, unsigned and
missing decision receipts, source recording times, bounded restore, archive round trips and
concurrent requests. A 61-record shared-ground fixture now evaluates in about 0.21 ms on the
qualification host. Eight successive rejection decisions each issued 94 Git calls in that
fixture. These timings describe its specific graph and hardware.

The pinned Wiki MCP server advertised seven authoring tools during a real stdio session. The
extraction benchmark checks navigation filtering, factual table cells and native mood
handling. Cache misses carry reasons for subsequent runs. The cause of the zero hits in the
original audit remains undetermined.

## Earlier platform qualification

The following matrix describes earlier native qualification runs. Runtime and fixture identities are retained in `runtime/components.json`, `runtime/source-manifest.json`, and `tests/fixtures/manifest.json`.

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
