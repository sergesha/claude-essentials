# claude-essentials

A Claude Code marketplace for small, reusable, infra-flavored plugins — things useful across
unrelated projects, not tied to any one domain.

This repository is a Claude Code marketplace (`.claude-plugin/marketplace.json`). Each plugin it
lists is versioned and released independently (see [Versioning](#versioning)).

## Plugins

| Plugin | What it does |
|---|---|
| [`continuous-learning`](continuous-learning/) | Capture runtime surprises as they happen, periodically promote them into a project's own versioned skills/docs/commands. Requires a namespaced `redis-memory` connection — see its own README. |
| [`redis-memory`](redis-memory-mcp/) | Persistent cross-session memory for AI agents — semantic search + KV store with auto-expiry. Moved here from the standalone `sergesha/redis-memory-mcp` repo at v0.5.0. |

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

`continuous-learning` depends on `redis-memory` (`mem_save`/`mem_list`/`mem_search`/`mem_delete`
tools) — both live in this marketplace, declared in `continuous-learning`'s `dependencies`, so
installing it also pulls in `redis-memory`. The auto-pulled dependency installs with
`redis-memory`'s defaults (`mode: dedicated`, no namespace) — since continuous-learning requires
a namespaced connection (see its README), also run `redis-memory`'s own install with `--config`
to configure it, either before or after installing `continuous-learning`.

## Versioning

Each plugin is its own [release-please](https://github.com/googleapis/release-please) package,
tagged independently (`<plugin>-vX.Y.Z`) with its own CHANGELOG. A change to one plugin does not
bump another's version. Config: `release-please-config.json`, state:
`.release-please-manifest.json`.
