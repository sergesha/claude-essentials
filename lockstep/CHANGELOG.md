# Changelog

## Unreleased

### Features

- Add a Codex plugin adapter, shared policy hooks, and Codex one-shot/fractal runner support while preserving Claude Code compatibility.

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
