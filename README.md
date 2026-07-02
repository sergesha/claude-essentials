# claude-essentials

A Claude Code marketplace for small, reusable, infra-flavored plugins — things useful across
unrelated projects, not tied to any one domain.

This repository is a Claude Code marketplace (`.claude-plugin/marketplace.json`). Each plugin it
lists is versioned and released independently (see [Versioning](#versioning)).

## Plugins

| Plugin | What it does |
|---|---|
| [`continuous-learning`](continuous-learning/) | Capture runtime surprises as they happen, periodically promote them into a project's own versioned skills/docs/commands. |

## Install

```
/plugin marketplace add sergesha/claude-essentials
/plugin install continuous-learning@claude-essentials
```

Or auto-enable per project in `.claude/settings.json`:

```json
{
  "enabledPlugins": { "continuous-learning@claude-essentials": true },
  "extraKnownMarketplaces": {
    "claude-essentials": { "source": { "source": "github", "repo": "sergesha/claude-essentials" }, "autoUpdate": true }
  }
}
```

`continuous-learning` depends on the [`redis-memory`](https://github.com/sergesha/redis-memory-mcp)
plugin (`mem_save`/`mem_list`/`mem_search`/`mem_delete` tools) — declared in this marketplace's
`dependencies`, so installing it also pulls in `redis-memory-mcp`.

## Versioning

Each plugin is its own [release-please](https://github.com/googleapis/release-please) package,
tagged independently (`<plugin>-vX.Y.Z`) with its own CHANGELOG. A change to one plugin does not
bump another's version. Config: `release-please-config.json`, state:
`.release-please-manifest.json`.
