# Knowledge Atlas

Reviewed, versioned knowledge for Codex and Claude Code. Reuses PoC9's native
Wiki/Utopia pipeline, canonical Git packages, publication-selected graph/search,
knowledge application and portable historical vectors. The plugin adds durable
operations and shared authenticated HTTP, Python and MCP interfaces.

The **0.1.0** backend has passing native runtime and full lifecycle checks with both
Codex and Claude Code on macOS ARM64 and Ubuntu 24.04 ARM64. Local Docker
deployment also passed fresh runtime preparation, both provider lifecycles and
restore/search checks. See the verification report for evidence and limits.
Windows and ACP are outside this version; no external image release is claimed.

Version **0.2.0** adds the authenticated localhost [viewer](docs/viewer.md),
publication-bound questions and explicit clarification review. Docker is the
recommended qualified ARM64 deployment; native remains supported. Viewer-specific
verification is recorded separately from the established backend qualification.

Start with [installation](docs/installation.md), then [provider configuration](docs/providers.md)
and [API usage](docs/api.md). See [operations and review](docs/operations.md),
[package composition](docs/composition.md), [archives](docs/archives.md) and
[verification limits](docs/verification.md).

PoC source identities and adaptations are recorded in
`runtime/source-manifest.json`. Exact source, model and patch pins are packaged;
data, model weights, native builds and host credentials stay outside the plugin.
Redis, PostgreSQL and a full Utopia server are not dependencies of this backend.

## Install the plugin

Claude Code:

```text
/plugin marketplace add sergesha/claude-essentials
/plugin install knowledge-atlas@claude-essentials
```

Codex:

```sh
codex plugin marketplace add sergesha/claude-essentials
codex plugin add knowledge-atlas@claude-essentials
```

Plugin installation registers the skill and MCP launcher; it does not download
models, prepare TerminusDB, or authenticate your processing provider. Complete
[Docker setup](docker/README.md) or [native setup](docs/installation.md), set the
external configuration, and restart the host to use it. Keep credentials and
knowledge data outside the plugin. For later updates, use the repository's
[plugin update procedure](../docs/plugin-updates.md).

## Development

Use Python 3.12.14 and `uv sync --locked`, then `uv run pytest`. Native checks are
explicitly configured; skipped checks are not platform qualification. Run
`uv run knowledge-atlas --help` for service and runtime commands.

## License

Knowledge Atlas uses the repository-wide MIT License. The bundled [LICENSE](LICENSE) is a copy for standalone distribution. Third-party components retain their own licenses and notices.
