# Changelog

## [0.6.0](https://github.com/sergesha/claude-essentials/compare/tech-radar-v0.5.0...tech-radar-v0.6.0) (2026-09-06)


### Features

* **tech-radar:** add native Codex integration ([#71](https://github.com/sergesha/claude-essentials/issues/71)) ([c82073b](https://github.com/sergesha/claude-essentials/commit/c82073b42d4ef328a3e5ed23ad7b3e90eebe29a7))
* **tech-radar:** restore plugin in shared marketplace ([#65](https://github.com/sergesha/claude-essentials/issues/65)) ([1aac794](https://github.com/sergesha/claude-essentials/commit/1aac7945466696a101815729508782048df53d63))

## [0.5.0](https://github.com/sergesha/claude-tech-radar/compare/v0.4.0...v0.5.0) (2026-07-23)


### Features

* **plugin:** depend on maintained redis-memory (claude-essentials), drop bundled bridge ([e0aa108](https://github.com/sergesha/claude-tech-radar/commit/e0aa10824c7306086b0f104963e0a542c8527db7))
* **plugin:** depend on the maintained redis-memory plugin, drop the bundled bridge ([98868b3](https://github.com/sergesha/claude-tech-radar/commit/98868b377f9b06f8ac838a16ee0cdb3b4cb2405f))

## [0.4.0](https://github.com/sergesha/claude-tech-radar/compare/v0.3.0...v0.4.0) (2026-07-23)


### Features

* **collect-news:** cross-run seen-URL dedup as step 5 — corpus-wide kv index, corpus_duplicates_skipped stat ([dd366de](https://github.com/sergesha/claude-tech-radar/commit/dd366de0cb682db3f028c042c9cbd8253679fd38))
* **collect-news:** cross-run seen-URL dedup as step 5 (corpus_duplicates_skipped) ([23e9e07](https://github.com/sergesha/claude-tech-radar/commit/23e9e07edbf248bd1858a1d0209aaf2c63ff13c9))

## [0.3.0](https://github.com/sergesha/claude-tech-radar/compare/v0.2.0...v0.3.0) (2026-07-10)


### Features

* supervise the search stack with Quadlet units (compose stays as fallback) ([#6](https://github.com/sergesha/claude-tech-radar/pull/6)) ([b569768](https://github.com/sergesha/claude-tech-radar/commit/b56976800e402f298f717bdaccaf3188d0478ae0))

## [0.2.0](https://github.com/sergesha/claude-tech-radar/compare/v0.1.1...v0.2.0) (2026-07-09)

Hand-released: the version was bumped in-PR, bypassing release-please.
Recorded here retroactively; the manifest was reconciled to 0.2.0 and the
v0.2.0 tag added after the fact.

### Bug Fixes

* mount searxng config read-only from CLAUDE_PLUGIN_DATA ([#4](https://github.com/sergesha/claude-tech-radar/pull/4)) ([c2cdec5](https://github.com/sergesha/claude-tech-radar/commit/c2cdec5))

## [0.1.1](https://github.com/sergesha/claude-tech-radar/compare/v0.1.0...v0.1.1) (2026-07-02)


### Bug Fixes

* **hook:** start the Docker stack non-blocking so first run isn't killed by timeout ([f920929](https://github.com/sergesha/claude-tech-radar/commit/f920929bf3409ec1f7e39b98253edf19acf97e22))
* redis-memory-mcp does not inherit shell env vars ([#2](https://github.com/sergesha/claude-tech-radar/issues/2)) ([20c1e5f](https://github.com/sergesha/claude-tech-radar/commit/20c1e5f8af640333645b39c9c1f8c31084e8bcde))
