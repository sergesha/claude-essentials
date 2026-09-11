# Changelog

## Unreleased

### Features

- Add a Codex plugin adapter, shared policy hooks, and Codex one-shot/fractal runner support while preserving Claude Code compatibility.

## [0.3.0](https://github.com/sergesha/claude-essentials/compare/lockstep-v0.2.7...lockstep-v0.3.0) (2026-09-11)


### Features

* **lockstep:** add Codex and Claude parity ([b5a8a24](https://github.com/sergesha/claude-essentials/commit/b5a8a24b4e47e3e544412adbfdd365430383fa51))
* **lockstep:** add daily reviewed change workflows ([eb85833](https://github.com/sergesha/claude-essentials/commit/eb85833b74a07619199340e84fb179c5d173b78d))
* **lockstep:** add native workflow DSL and durable runtime ([6a829a2](https://github.com/sergesha/claude-essentials/commit/6a829a2b8599a08e61201bdfdf118c4b7ddc178e))
* **lockstep:** flow enforcement for coding agents — engine, gates and fractal subcalls ([a0cbb2b](https://github.com/sergesha/claude-essentials/commit/a0cbb2b5a3ccc301ea36d8efe6c29e7ddea6cdc9))


### Bug Fixes

* align Claude Code and Codex plugin declarations for full symmetry ([378a46f](https://github.com/sergesha/claude-essentials/commit/378a46fa14610886703c994d10b311bcdea93e9c))
* **authoring:** resolve included graph fragments ([286c847](https://github.com/sergesha/claude-essentials/commit/286c847e467d9b68ea432dbb30318cff569f3274))
* **authoring:** retain temporary ownership handle ([e47d64f](https://github.com/sergesha/claude-essentials/commit/e47d64f7d44582c56fbdc94d087ed926a863756e))
* **code-intel,lockstep:** remove duplicate hooks declaration from Claude plugin.json ([b60549b](https://github.com/sergesha/claude-essentials/commit/b60549bfca6354cddd4f0d9e4c3931705b54b8b5))
* **lockstep:** enforce manual artifacts and restore built-in recipe validation ([#59](https://github.com/sergesha/claude-essentials/issues/59)) ([c75355f](https://github.com/sergesha/claude-essentials/commit/c75355fc3472736588e01a11d321a2c516537a81))
* **lockstep:** remove obsolete runner compatibility ([8299a98](https://github.com/sergesha/claude-essentials/commit/8299a98f919cd5a1157e8732fb45d8e8b8f3134d))
* **lockstep:** repair recipe discovery and runtime diagnostics ([#45](https://github.com/sergesha/claude-essentials/issues/45)) ([15d3f33](https://github.com/sergesha/claude-essentials/commit/15d3f3318ad77e8bc5c84024a9c83abc75bf7afe))
* **lockstep:** restore usable workflow handoffs and lifecycle controls ([#42](https://github.com/sergesha/claude-essentials/issues/42)) ([ee4a826](https://github.com/sergesha/claude-essentials/commit/ee4a826671fe710a67c8a930cc41616384277911))
* **lockstep:** support independent Claude and Codex runners ([ec79b23](https://github.com/sergesha/claude-essentials/commit/ec79b23538864eadff57c0d141dd5706514efdf6))
* **lockstep:** support protected parallel workflows and local checks ([#74](https://github.com/sergesha/claude-essentials/issues/74)) ([50067f7](https://github.com/sergesha/claude-essentials/commit/50067f71c552f3482117f477d1eb11651746fce9))
* **runtime:** accept standard owner Codex homes ([a905097](https://github.com/sergesha/claude-essentials/commit/a905097dffd13d8243e448f94cde7c15a7336452))
* **runtime:** contain post-spawn Codex failures ([8e8f10d](https://github.com/sergesha/claude-essentials/commit/8e8f10d1526b66e63b81d1f33e0496539fcba714))
* **runtime:** recover parallel managed effects ([4972313](https://github.com/sergesha/claude-essentials/commit/49723137ffe81e290fc96d3e54ba4060a35bf534))
* **runtime:** validate protected manual completion ([e298b13](https://github.com/sergesha/claude-essentials/commit/e298b13d33cd21797eb1ce001fc37c7cd78e3bd1))

## [0.2.7](https://github.com/sergesha/claude-essentials/compare/lockstep-v0.2.6...lockstep-v0.2.7) (2026-09-11)


### Bug Fixes

* **code-intel,lockstep:** remove duplicate hooks declaration from Claude plugin.json ([6175376](https://github.com/sergesha/claude-essentials/commit/6175376b85bfaf715588edf63ba5f26339cefb9a))

## [0.2.6](https://github.com/sergesha/claude-essentials/compare/lockstep-v0.2.5...lockstep-v0.2.6) (2026-09-11)


### Bug Fixes

* align Claude Code and Codex plugin declarations for full symmetry ([4cfb43a](https://github.com/sergesha/claude-essentials/commit/4cfb43aa2d03cf058e2eb5df4e14784a52723a24))

## [0.2.5](https://github.com/sergesha/claude-essentials/compare/lockstep-v0.2.4...lockstep-v0.2.5) (2026-09-08)


### Bug Fixes

* **lockstep:** remove obsolete runner compatibility ([9c36ca0](https://github.com/sergesha/claude-essentials/commit/9c36ca001ff1498930e0a43a8c2065e0ce2aa38f))

## [0.2.4](https://github.com/sergesha/claude-essentials/compare/lockstep-v0.2.3...lockstep-v0.2.4) (2026-09-08)


### Bug Fixes

* **lockstep:** support independent Claude and Codex runners ([aebc7c0](https://github.com/sergesha/claude-essentials/commit/aebc7c02060acbd4f1c112bfe3b18eb2c2f7aa0a))

## [0.2.3](https://github.com/sergesha/claude-essentials/compare/lockstep-v0.2.2...lockstep-v0.2.3) (2026-09-06)


### Bug Fixes

* **lockstep:** support protected parallel workflows and local checks ([#74](https://github.com/sergesha/claude-essentials/issues/74)) ([399866c](https://github.com/sergesha/claude-essentials/commit/399866c30fb5b3aa8b4d148b7db03f301ddb12d5))

## [0.2.2](https://github.com/sergesha/claude-essentials/compare/lockstep-v0.2.1...lockstep-v0.2.2) (2026-09-06)


### Bug Fixes

* **lockstep:** enforce manual artifacts and restore built-in recipe validation ([#59](https://github.com/sergesha/claude-essentials/issues/59)) ([67a4141](https://github.com/sergesha/claude-essentials/commit/67a41416640e57bb10cc5d965a6bf19558a48387))

## [0.2.1](https://github.com/sergesha/claude-essentials/compare/lockstep-v0.2.0...lockstep-v0.2.1) (2026-09-05)


### Bug Fixes

* **lockstep:** repair recipe discovery and runtime diagnostics ([#45](https://github.com/sergesha/claude-essentials/issues/45)) ([7f0b5b9](https://github.com/sergesha/claude-essentials/commit/7f0b5b9b6ea5d88e08d2292a092d0a65aec0a581))

## [0.2.0](https://github.com/sergesha/claude-essentials/compare/lockstep-v0.1.0...lockstep-v0.2.0) (2026-09-05)


### Features

* **lockstep:** add Codex and Claude parity ([b5a8a24](https://github.com/sergesha/claude-essentials/commit/b5a8a24b4e47e3e544412adbfdd365430383fa51))
* **lockstep:** add daily reviewed change workflows ([eb85833](https://github.com/sergesha/claude-essentials/commit/eb85833b74a07619199340e84fb179c5d173b78d))
* **lockstep:** add native workflow DSL and durable runtime ([5ffb4df](https://github.com/sergesha/claude-essentials/commit/5ffb4dfe3b028cb623e0132c26474e8ec40f9fe3))


### Bug Fixes

* **authoring:** resolve included graph fragments ([92c7bba](https://github.com/sergesha/claude-essentials/commit/92c7bba7f94c393ea2875f2be916cc88875d1179))
* **authoring:** retain temporary ownership handle ([242d159](https://github.com/sergesha/claude-essentials/commit/242d159bee2514bfba233eaf4566c888656c8f44))
* **lockstep:** restore usable workflow handoffs and lifecycle controls ([#42](https://github.com/sergesha/claude-essentials/issues/42)) ([88b42ff](https://github.com/sergesha/claude-essentials/commit/88b42fffaf762477a528963b40c0fddc6ca36439))
* **runtime:** accept standard owner Codex homes ([2d57b1b](https://github.com/sergesha/claude-essentials/commit/2d57b1b19337663c3c57af71ca1d78e6932528bd))
* **runtime:** contain post-spawn Codex failures ([58d023d](https://github.com/sergesha/claude-essentials/commit/58d023de2efd7b8cb03a3d86bfa7ad82ce5e35c9))
* **runtime:** recover parallel managed effects ([166191c](https://github.com/sergesha/claude-essentials/commit/166191c2f4d664c00572b72f5a01c336e8e0d02d))
* **runtime:** validate protected manual completion ([2db77ad](https://github.com/sergesha/claude-essentials/commit/2db77adc1876276912dce3eee33cd9856582bc86))

## 0.1.0 (2026-08-08)


### Features

* **lockstep:** flow enforcement for coding agents — engine, gates and fractal subcalls ([3544b75](https://github.com/sergesha/claude-essentials/commit/3544b758599c6665b47e41fa4c7df5dee2895295))
