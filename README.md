# claude-essentials

A plugin marketplace for **Claude Code and Codex**: knowledge management, persistent
memory, code exploration, planning, workflow execution, learning and technology news.

Choose the plugins you need. Each has its own setup guide, dependencies and release cycle.

## Plugins

| Plugin | Use it to | Documentation |
| --- | --- | --- |
| `knowledge-atlas` | Build reviewed, versioned knowledge with a local wiki, cited answers, an evidence graph and reusable knowledge packages. | [Guide](knowledge-atlas/README.md) · [Docker setup](knowledge-atlas/docker/README.md) |
| `redis-memory` | Remember facts and settings across agent sessions using semantic search and key-value storage. | [Guide and setup](redis-memory-mcp/README.md) |
| `code-intel` | Explore symbols, call paths and change impact through CodeGraph and code-review-graph, with shared setup and indexing hooks. | [Setup and operations](code-intel/skills/code-intel/SKILL.md) |
| `speciflow` | Coordinate backlog, specifications and execution tasks across Backlog.md, OpenSpec and Beads, with Superpowers and Grill supporting the work. | [Workflow guide](speciflow/skills/speciflow/SKILL.md) · [Setup](speciflow/skills/speciflow/references/installation.md) |
| `lockstep` | Run durable, declarative workflows that advance only after validating submitted evidence. | [Guide and setup](lockstep/README.md) |
| `continuous-learning` | Capture unexpected tool and workflow behavior, then turn reviewed findings into a project's versioned documentation and skills. | [Guide and setup](continuous-learning/README.md) |
| `tech-radar` | Collect technology news through SearXNG, retain summaries in Redis memory and generate reports and dashboards. | [Guide and setup](tech-radar/README.md) |

## Install

Add the marketplace once, then install a selected plugin. Replace `<plugin>` with
a name from the table above.

### Claude Code

Run inside Claude Code:

```text
/plugin marketplace add sergesha/claude-essentials
/plugin install <plugin>@claude-essentials
```

### Codex

Run in a terminal:

```text
codex plugin marketplace add sergesha/claude-essentials
codex plugin add <plugin>@claude-essentials
```

Follow the selected plugin's guide for prerequisites, backend setup, credentials
and hook activation. Installing a plugin does not necessarily configure its
services or dependencies; those steps can differ between hosts.

## Updating plugins

Follow the shared [plugin update procedure](docs/plugin-updates.md) for Claude
Code and Codex. It covers installed-plugin selection, applying updates to open
sessions, stale hook paths, and separate installation/activation checks.

## Versioning

Plugins are released independently through
[release-please](https://github.com/googleapis/release-please), with tags of the
form `<plugin>-vX.Y.Z` and a changelog in each plugin directory. See
[GitHub Releases](https://github.com/sergesha/claude-essentials/releases) for
published versions. A change to one plugin does not bump another's version.

Marketplace catalogs: [Claude Code](.claude-plugin/marketplace.json) ·
[Codex](.agents/plugins/marketplace.json). Release configuration:
[packages](release-please-config.json) · [versions](.release-please-manifest.json).

## License

Project-owned code and documentation are licensed under the [MIT License](LICENSE). Third-party components retain their own licenses and notices.
