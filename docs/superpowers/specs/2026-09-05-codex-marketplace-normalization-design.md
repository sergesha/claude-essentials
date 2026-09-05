# Codex Marketplace Normalization Design

## Purpose

After `code-intel` is complete, perform a separate repository-wide packaging
task so every distributable component in `claude-essentials` uses the same
two-host model: Claude's repository marketplace plus Codex's repository
marketplace, with one plugin directory per independently versioned component.

This task is intentionally separate from the `code-intel` implementation and
release. It must not be folded into the initial plugin commit series.

## Current State on `main`

- `speciflow` already has both plugin manifests and is present in both
  marketplaces. Its CI path filters incorrectly reference the nonexistent
  `.codex-plugin/marketplace.json` instead of
  `.agents/plugins/marketplace.json`.
- `lockstep` already has both plugin manifests, `.mcp.json`, skills, and hooks,
  but is absent from the Codex marketplace. Its release configuration updates
  the Claude manifest and Python package version but not the Codex manifest.
  Its CI filters do not cover the Codex manifest or root Codex marketplace.
- `continuous-learning` has a Claude manifest, skill, command, and session
  hook, but no Codex manifest or Codex marketplace entry.
- `redis-memory` has a Claude manifest, shared `.mcp.json`, hooks, skill, and
  runtime, but no Codex manifest or Codex marketplace entry. Its current MCP
  environment uses Claude `userConfig` placeholders and therefore needs an
  explicit Codex configuration contract rather than a blind manifest copy.
- The root README describes the repository as Claude-only and documents only
  Claude installation.

## Target Contract

Every root marketplace component has:

- one entry in `.claude-plugin/marketplace.json`;
- one entry in `.agents/plugins/marketplace.json`;
- `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` with matching
  identity and release version;
- host-native pointers to its shipped skills, hooks, and MCP servers;
- release-please coverage for both manifests;
- CI path filters that include both manifests and both root marketplaces;
- an installed-layout packaging test for both hosts;
- README installation instructions for Claude and Codex.

Plugin-specific capabilities may differ only where a host lacks an equivalent
configuration primitive. Such a difference must be explicit in that plugin's
documentation and tests; the marketplace entry itself must not advertise a
nonfunctional installation.

## Task Boundaries

### SpeciFlow

Keep its existing manifests and marketplace entries. Correct CI filters,
centralize its version-parity assertion, and validate both installed layouts.

### Lockstep

Add the Codex marketplace entry. Add its Codex manifest to release-please and
CI paths. Verify that the default `hooks/hooks.json` discovery and `.mcp.json`
server declaration work from an installed Codex cache path.

### Continuous Learning

Add a Codex manifest declaring `./skills/` and `./hooks/session-start.json`,
then add the Codex marketplace entry. Preserve the Claude command component as
Claude-only if Codex has no equivalent command directory primitive; the shared
skill remains the canonical workflow on both hosts. Document invocation on
each host.

### Redis Memory

Design and test the Codex configuration path before adding its marketplace
entry. Do not pass unresolved `${user_config.*}` strings to the server.
Prefer a plugin-owned, explicit configuration command that writes non-secret
settings under `PLUGIN_DATA`; keep credentials in environment or an external
secret mechanism. The MCP launcher reads validated configuration and starts
the existing runtime. Claude `userConfig` remains supported through its host
manifest.

Continuous Learning's dependency on Redis Memory must be represented only by a
mechanism the Codex marketplace actually supports. If no dependency primitive
is available, its Codex documentation and doctor output must require a
separate `redis-memory` installation instead of inventing dependency metadata.

## Shared Packaging Tests

Introduce a small root-level packaging test suite that treats marketplace and
manifest consistency as repository invariants:

- marketplace names are unique;
- every marketplace source resolves inside the repository;
- component sets match across hosts unless an explicit tested exception is
  recorded;
- both manifests match the release-please version;
- every declared skills, hooks, and MCP path exists;
- release-please updates every independently versioned host manifest;
- CI filters cover every file whose contract that workflow tests;
- install documentation names valid host commands and plugin identifiers.

Plugin-specific tests remain beside each component. The root suite checks only
cross-component invariants and must not duplicate runtime behavior tests.

## Delivery Order

1. Correct shared marketplace/release/CI invariants and SpeciFlow coverage.
2. Complete Lockstep Codex registration.
3. Package Continuous Learning for Codex.
4. Establish Redis Memory's Codex configuration contract and then register it.
5. Update the root README and run installed-layout validation for all plugins.

Each step is independently reviewable and must leave both marketplaces valid.
No step removes or migrates any user-level legacy installation.
