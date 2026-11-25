# Changelog

## [0.4.0](https://github.com/sergesha/claude-essentials/compare/knowledge-atlas-v0.3.4...knowledge-atlas-v0.4.0) (2026-11-25)


### Features

* **knowledge-atlas:** configure reviewed child layouts atomically ([6ee5ab1](https://github.com/sergesha/claude-essentials/commit/6ee5ab1e36681048d1a032d2a4212bc340b7e6f0))
* **knowledge-atlas:** deliver multi-project Atlas 0.4.0 and verified ARM64 images ([6b37417](https://github.com/sergesha/claude-essentials/commit/6b3741716537c2650f96433e799c8db9f22b3907))
* **knowledge-atlas:** isolate projects by registered UUID ([4eb334a](https://github.com/sergesha/claude-essentials/commit/4eb334a1fde17f3e26c9ebe53c57feeb3293283b))
* **knowledge-atlas:** pin MCP connections to exact services ([16d4551](https://github.com/sergesha/claude-essentials/commit/16d4551b69d23a276b08f7c5e000d5fc3abcab6a))
* **knowledge-atlas:** qualify and publish release-specific ARM64 images ([4d41dfc](https://github.com/sergesha/claude-essentials/commit/4d41dfc6e7bfaf76106462cc73e339a3092b7ec2))
* **knowledge-atlas:** retain composition receipts in portable archives ([ceabce6](https://github.com/sergesha/claude-essentials/commit/ceabce6652335547058ad8989ebce00f9d874959))
* **knowledge-atlas:** seed Docker installations from a prebuilt runtime ([208d82e](https://github.com/sergesha/claude-essentials/commit/208d82ee800af2270a9016e655ef444d1a131558))
* **knowledge-atlas:** share native startup and adopt retained projects ([a4d13a4](https://github.com/sergesha/claude-essentials/commit/a4d13a47539c96edff2ce72c85f3d0038205a0e0))
* **knowledge-atlas:** switch viewer projects in one window ([210b91f](https://github.com/sergesha/claude-essentials/commit/210b91fb2e979ba4173f2db8cd00f7929cdb9817))


### Bug Fixes

* **ci:** use config-based Docker image IDs in security jobs ([9484c7b](https://github.com/sergesha/claude-essentials/commit/9484c7b5b5038c55386a568059b4fe3d23f999fc))
* **knowledge-atlas:** bind release scan and push to exact identities ([b648593](https://github.com/sergesha/claude-essentials/commit/b6485936efc8d1875032c57ad53f39ad415406b7))
* **knowledge-atlas:** compare native publication timestamps by instant ([5161f8f](https://github.com/sergesha/claude-essentials/commit/5161f8f3ccc45e0946604cf5b31c040bff014f6f))
* **knowledge-atlas:** diagnose legacy Docker launcher settings ([adc030e](https://github.com/sergesha/claude-essentials/commit/adc030e2b02cfc2c5406a8ef45e479394f8a2e27))
* **knowledge-atlas:** enforce project read and create ownership ([c7e549d](https://github.com/sergesha/claude-essentials/commit/c7e549d34308973fe27f3230cfa67f31c6575ea3))
* **knowledge-atlas:** honor filesystem-equivalent reservations ([4600e37](https://github.com/sergesha/claude-essentials/commit/4600e37c476da9dcc9ad55c27abcb95024f5dc9f))
* **knowledge-atlas:** isolate creates from symlink-loop registrations ([025462e](https://github.com/sergesha/claude-essentials/commit/025462e83f4a56d01a7f52241ac78a86814a33f8))
* **knowledge-atlas:** isolate creates from unreadable registrations ([e902453](https://github.com/sergesha/claude-essentials/commit/e902453415e0a94fe28053be852f42aff584aa10))
* **knowledge-atlas:** isolate image smoke and ship tokenizer attribution ([f346734](https://github.com/sergesha/claude-essentials/commit/f346734b9cbd66728efa0c2eb8872a8a04aa31fb))
* **knowledge-atlas:** preserve retained state and recover interrupted admissions ([4eb0f68](https://github.com/sergesha/claude-essentials/commit/4eb0f687f40a459076917975da7ce648a896836c))
* **knowledge-atlas:** preserve viewer drafts and release rejected retries ([11647b5](https://github.com/sergesha/claude-essentials/commit/11647b55b03ba564bf26d39428dc65fdd4b6ae09))
* **knowledge-atlas:** protect viewer text after a rejected replay ([3c4470c](https://github.com/sergesha/claude-essentials/commit/3c4470c68b8aef24f076a995dd1835849cf6e9d2))
* **knowledge-atlas:** reject obsolete retained source and extraction data ([c7d13ea](https://github.com/sergesha/claude-essentials/commit/c7d13eaa36704d1d9c1689b60323640df5ae77ba))
* **knowledge-atlas:** require current profile and attachment contracts ([5ee4a73](https://github.com/sergesha/claude-essentials/commit/5ee4a7334fe1de1eb7c2f1514e6989bb33b1544d))
* **knowledge-atlas:** require exact request and publication provenance ([e2b8419](https://github.com/sergesha/claude-essentials/commit/e2b84193e6ba17ee198134bdc26577f561551216))
* **knowledge-atlas:** retain and resolve receipts in their owning repositories ([673af70](https://github.com/sergesha/claude-essentials/commit/673af7014eca3f5e8b59d34e6333ea189dfd5290))
* **knowledge-atlas:** retain upstream profile provenance ([ae9e5e1](https://github.com/sergesha/claude-essentials/commit/ae9e5e159136d1d058bd305fc13505408b2ce710))
* **knowledge-atlas:** skip completed adoption during startup ([937c101](https://github.com/sergesha/claude-essentials/commit/937c101ed0eaacbed415c3ac005f7c96d3544abf))
* **knowledge-atlas:** validate setup inputs and reclaim interrupted downloads ([75fd5b2](https://github.com/sergesha/claude-essentials/commit/75fd5b291233c1db74abce5b1d70d76b91e31997))
* **knowledge-atlas:** verify legacy mounts and roll back cache adoption ([e3ec243](https://github.com/sergesha/claude-essentials/commit/e3ec24386d6575f5567457847dd498bc178ff350))
* **knowledge-atlas:** verify retained wrappers and native dispatch ownership ([492236d](https://github.com/sergesha/claude-essentials/commit/492236da36745b59273ca68c14ba819f516f6133))
* **knowledge-atlas:** verify root publication ownership during restore ([b6d460f](https://github.com/sergesha/claude-essentials/commit/b6d460fd8a42965985940f34ccf65ddfeb7781b7))

## [0.3.4](https://github.com/sergesha/claude-essentials/compare/knowledge-atlas-v0.3.3...knowledge-atlas-v0.3.4) (2026-11-24)


### Bug Fixes

* **knowledge-atlas:** require an atlas target before native MCP open ([#152](https://github.com/sergesha/claude-essentials/issues/152)) ([f4009d8](https://github.com/sergesha/claude-essentials/commit/f4009d8c0c6764f487dcc8a97f66d106fdd72ec3))

## [0.3.3](https://github.com/sergesha/claude-essentials/compare/knowledge-atlas-v0.3.2...knowledge-atlas-v0.3.3) (2026-11-24)


### Bug Fixes

* **knowledge-atlas:** open configured Compose instances from MCP ([#150](https://github.com/sergesha/claude-essentials/issues/150)) ([cddc3af](https://github.com/sergesha/claude-essentials/commit/cddc3af8fb1e3fe3d71b9ef76a1c7a74eb9b9274))

## [0.3.2](https://github.com/sergesha/claude-essentials/compare/knowledge-atlas-v0.3.1...knowledge-atlas-v0.3.2) (2026-11-22)


### Bug Fixes

* **knowledge-atlas:** recover publication history, bound restore staging and reuse shared evidence ([#148](https://github.com/sergesha/claude-essentials/issues/148)) ([fa7aeaa](https://github.com/sergesha/claude-essentials/commit/fa7aeaa35b94bfd2d783e4d425e7d1536c259c9c))

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
