# Changelog

## [0.3.1](https://github.com/sergesha/claude-essentials/compare/knowledge-atlas-v0.3.0...knowledge-atlas-v0.3.1) (2026-11-20)


### Bug Fixes

* **knowledge-atlas:** preserve legacy archive decision order during replay ([c52131f](https://github.com/sergesha/claude-essentials/commit/c52131fbdb868b00a5965585d4732ccc78ac388c))
* **knowledge-atlas:** repair audit findings across publication, archives and local interfaces ([6afc69f](https://github.com/sergesha/claude-essentials/commit/6afc69f8e5da90b9818803fa91bc7d1a11eecead))
* **knowledge-atlas:** repair publication, restore and local service reliability ([f477149](https://github.com/sergesha/claude-essentials/commit/f477149741c575c1eab129c21359b70482da6cab))

## [0.3.0](https://github.com/sergesha/claude-essentials/compare/knowledge-atlas-v0.2.0...knowledge-atlas-v0.3.0) (2026-11-19)


### Features

* **knowledge-atlas:** add reviewed knowledge plugin for Codex and Claude Code ([d7505ef](https://github.com/sergesha/claude-essentials/commit/d7505ef7d87f1803923def97fe04e23e914049f4))
* **knowledge-atlas:** add reviewed knowledge plugin for Codex and Claude Code ([fbea172](https://github.com/sergesha/claude-essentials/commit/fbea17249f17d73815f1aba5647fa8e2f6d44d42))

## 0.2.0

Adds an authenticated localhost viewer, publication-bound wiki reads and cited
questions, timeline/diff browsing and explicit clarification review. Docker remains
private behind MCP attachment; native installation is retained. Adds optional host
storage overrides with durable volume defaults and bounded host-mounted TerminusDB
process-crash recovery evidence. Versioned GHCR publication remains deferred.

## 0.1.0

Initial plugin implementation reusing PoC9 canonical Git, Wiki/Utopia processing,
publication-selected graph/search, reviewed package composition and portable
history. Adds durable operations, canonical decisions, Codex/Claude provider
adapters, and authenticated local HTTP with Python and MCP clients.

Includes optional persistent local ARM64 Docker deployment. Native macOS/Linux
Codex and Claude lifecycles, fresh Docker runtime preparation and archive recovery
have passing evidence; see docs/verification.md for scope and limitations.
