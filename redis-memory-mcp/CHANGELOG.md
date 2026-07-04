# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0](https://github.com/sergesha/claude-essentials/compare/redis-memory-mcp-v0.7.0...redis-memory-mcp-v0.8.0) (2026-07-04)


### Features

* **redis-memory-mcp:** enforced per-NAMESPACE isolation via Redis ACLs ([#17](https://github.com/sergesha/claude-essentials/issues/17)) ([#20](https://github.com/sergesha/claude-essentials/issues/20)) ([97789bd](https://github.com/sergesha/claude-essentials/commit/97789bd614913e82c9f2e46d0fc7e79515938e92))


### Bug Fixes

* **redis-memory-mcp:** stop shipping/advertising an open secrets store ([#19](https://github.com/sergesha/claude-essentials/issues/19)) ([27cb495](https://github.com/sergesha/claude-essentials/commit/27cb4951be94a864629db3983756c00778dbed33))

## [0.7.0](https://github.com/sergesha/claude-essentials/compare/redis-memory-mcp-v0.6.0...redis-memory-mcp-v0.7.0) (2026-07-04)


### Features

* **redis-memory-mcp:** Unix-socket transport for cross-OS-user access ([#16](https://github.com/sergesha/claude-essentials/issues/16)) ([9e604ef](https://github.com/sergesha/claude-essentials/commit/9e604eff6cd0a22235e1efc51d5c182f014d57cd))

## [0.6.0](https://github.com/sergesha/claude-essentials/compare/redis-memory-mcp-v0.5.2...redis-memory-mcp-v0.6.0) (2026-07-02)


### Features

* **redis-memory-mcp:** support joining a shared container network ([#14](https://github.com/sergesha/claude-essentials/issues/14)) ([119aea8](https://github.com/sergesha/claude-essentials/commit/119aea8bdb81b18286b7210f3f1745e1ea81c3b5))

## [0.5.2](https://github.com/sergesha/claude-essentials/compare/redis-memory-mcp-v0.5.1...redis-memory-mcp-v0.5.2) (2026-07-02)


### Bug Fixes

* extra-files paths in release-please-config were repo-root-relative, not package-relative ([#12](https://github.com/sergesha/claude-essentials/issues/12)) ([10f7c60](https://github.com/sergesha/claude-essentials/commit/10f7c60e956563887867bce293cea99f2343a256))

## [0.5.1](https://github.com/sergesha/claude-essentials/compare/redis-memory-mcp-v0.5.0...redis-memory-mcp-v0.5.1) (2026-07-02)


### Bug Fixes

* plugin-installed redis-memory does not inherit shell env vars ([#10](https://github.com/sergesha/claude-essentials/issues/10)) ([f006e9a](https://github.com/sergesha/claude-essentials/commit/f006e9a41fe7b585b566aebede1cb654cc13b98c))

## 0.5.0 (2026-07-02)


### Features

* add redis-memory-mcp plugin ([2ef32c5](https://github.com/sergesha/claude-essentials/commit/2ef32c5ccd10715bf219dcb8d1ed34ecb70be0a7))

## [0.4.0](https://github.com/sergesha/redis-memory-mcp/compare/v0.3.0...v0.4.0) (2026-07-01)


### Features

* add per-call shared/own scope access alongside NAMESPACE ([#12](https://github.com/sergesha/redis-memory-mcp/issues/12)) ([365cbc0](https://github.com/sergesha/redis-memory-mcp/commit/365cbc0671e6b7e34127464ed47af7e5afaa6003))

## [0.3.0](https://github.com/sergesha/redis-memory-mcp/compare/v0.2.3...v0.3.0) (2026-07-01)


### Features

* add shared deployment mode and NAMESPACE data isolation ([#10](https://github.com/sergesha/redis-memory-mcp/issues/10)) ([c9fc44e](https://github.com/sergesha/redis-memory-mcp/commit/c9fc44eac64b5d71e27a09aa3b768aec74bbfacf))

## [0.2.3](https://github.com/sergesha/redis-memory-mcp/compare/v0.2.2...v0.2.3) (2026-06-20)


### Bug Fixes

* validate memory_id input and cap prefix scan in mem_delete ([#8](https://github.com/sergesha/redis-memory-mcp/issues/8)) ([a4bf4ee](https://github.com/sergesha/redis-memory-mcp/commit/a4bf4ee370ec52b3afdec6130a7dfd1e0227197f))

## [0.2.2](https://github.com/sergesha/redis-memory-mcp/compare/v0.2.1...v0.2.2) (2026-06-18)


### Bug Fixes

* expose full UUID in mem outputs and support short ID in mem_delete ([#6](https://github.com/sergesha/redis-memory-mcp/issues/6)) ([85832ee](https://github.com/sergesha/redis-memory-mcp/commit/85832ee630cc79ca6efb8c811d7d50622ae23740))

## [0.2.1](https://github.com/sergesha/redis-memory-mcp/compare/v0.2.0...v0.2.1) (2026-06-15)


### Bug Fixes

* **start:** map host.docker.internal for Linux Docker Engine ([ce86d0b](https://github.com/sergesha/redis-memory-mcp/commit/ce86d0b513b3ac25d5766cd6be5ea28df0d48dc6))

## [0.2.0](https://github.com/sergesha/redis-memory-mcp/compare/v0.1.1...v0.2.0) (2026-06-13)


### Features

* **start:** install pinned release tags instead of raw main ([cc0ba70](https://github.com/sergesha/redis-memory-mcp/commit/cc0ba703d8d53999dcaa2815521a741c53fef93b))
* **start:** install pinned release tags instead of raw main ([fb50cd7](https://github.com/sergesha/redis-memory-mcp/commit/fb50cd7e0cc71db07c97e169515d9e5c16ff2336))


### Bug Fixes

* RESP3 FT.SEARCH parsing + tag escaping; adopt release-please ([3434916](https://github.com/sergesha/redis-memory-mcp/commit/3434916bbe7bac9498aa62970acfde9fed1f0db1))
* **start:** address PR [#2](https://github.com/sergesha/redis-memory-mcp/issues/2) review (stale comment, image tag, curl timeouts) ([18aa742](https://github.com/sergesha/redis-memory-mcp/commit/18aa742747a91764cf9cf77a8668715030f1df9c))
* **start:** address PR [#2](https://github.com/sergesha/redis-memory-mcp/issues/2) review comments ([e0e1c3f](https://github.com/sergesha/redis-memory-mcp/commit/e0e1c3f6c83dc40420f1b457d7b4a008797ba5cd))
* **start:** harden image tag and bound source download time ([80de337](https://github.com/sergesha/redis-memory-mcp/commit/80de3375fe2339cecb4cdf0a79dd4f0ac372fa4d))

## [0.1.1] - 2026-06-13

### Fixed
- `mem_search` and `mem_list` now parse RESP3 `FT.SEARCH` map replies (as returned by
  redis-py 8), fixing the `Error executing tool mem_search: 0` (`KeyError(0)`) that broke
  semantic search on every query. Legacy RESP2 flat-list replies are still supported.
- Tag values are now backslash-escaped inside `@tags:{...}` filters, fixing the
  `Syntax error … near …` RediSearch parse failure on tags containing hyphens
  (e.g. `a2a-secure-messaging`).

### Changed
- Pinned the `redis` dependency to `>=5.0.0,<9` so a future major release (with another
  RESP/behaviour change) cannot silently break the server again.

## [0.1.0]

### Added
- Initial release: semantic memory (`mem_*`) and key/value (`kv_*`) MCP tools backed by
  Redis Stack (HNSW vector search) and TEI embeddings, with TTL-based auto-expiry.
