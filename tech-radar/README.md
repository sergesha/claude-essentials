# Tech Radar

Monitor technology news through SearXNG, persist summaries with Redis memory,
and produce HTML, JSON and YAML reports.

## Install in Claude Code

```text
/plugin marketplace add sergesha/claude-essentials
/plugin install tech-radar@claude-essentials
```

Configure the `redis-memory` dependency using its own installation instructions.
Commands: `/tech-radar:configure-topic`, `/tech-radar:collect-news`,
`/tech-radar:render-dashboard`, and `/tech-radar:show-result`.

## Install in Codex

```bash
codex plugin marketplace add sergesha/claude-essentials
codex plugin add redis-memory@claude-essentials
codex plugin add tech-radar@claude-essentials
```

Redis memory is installed and configured separately; Tech Radar does not bundle
a second Redis MCP server. Review and trust the Tech Radar `SessionStart` hook
through Codex `/hooks`, then start a new session. Native skill hints are
`$configure-topic`, `$collect-news`, `$render-dashboard`, and `$show-result`.

## Requirements

- Node.js with `npx` for the SearXNG MCP bridge.
- A separately configured `redis-memory` plugin from this marketplace. To reuse
  memories across Claude Code and Codex, point both at the same backend and keep
  the same `NAMESPACE`.
- Python 3 (report scripts use only the standard library).
- For the default managed stack: Podman with a systemd user manager, or Docker
  with Compose. For a premanaged stack: reachable SearXNG and cache endpoints.

The SessionStart hook prefers Podman Quadlets, falling back to
`docker-compose.yaml`. Override the managed strategy with
`TECH_RADAR_STACK=quadlet|compose`. Mutable container configuration lives in the
host-provided `CLAUDE_PLUGIN_DATA`, not the plugin source; that default is
unchanged.

Claude Code and Codex normally assign different plugin data directories. When
reusing an existing managed installation, export the same explicit directory
before launching either client, pointing it at the parent of the existing
`searxng/` mount:

```bash
export TECH_RADAR_DATA_DIR=/absolute/path/to/existing/tech-radar-data
```

The hook exports that value as `CLAUDE_PLUGIN_DATA`. It does not migrate data;
using the shared override avoids treating the other client's mount as drift.
Do not run two managed copies concurrently.

For a backend managed outside this plugin on the default local endpoints
(`localhost:8888` and `localhost:6381`), disable all stack lifecycle work in
either client before launch:

```bash
export TECH_RADAR_STACK=external
```

Codex can additionally forward custom MCP endpoints from its process
environment:

```bash
export SEARXNG_URL=http://search.example:8080
export CACHE_URL=redis://cache.example:6379
```

The custom endpoint example is Codex-only; the Claude Code MCP configuration
continues to use the default local endpoints. External mode itself performs no
data-directory, tool, or container action. Configure the separate redis-memory
plugin's `REDIS_URL`, embedding endpoint, and `NAMESPACE` according to its own
documentation.

## Upgrade

```bash
# Codex
codex plugin marketplace upgrade claude-essentials
codex plugin add tech-radar@claude-essentials

# Claude Code
claude plugin marketplace update claude-essentials
claude plugin update tech-radar@claude-essentials
```

Restart the client, retain the existing `TECH_RADAR_DATA_DIR` when applicable,
and upgrade redis-memory separately if needed.

## Verification

Run the non-destructive packaging/report tests from the repository root:
`python3 -m unittest discover -s tech-radar/tests`.

`bash tech-radar/scripts/smoke.sh` (from the repository root) starts containers named `tech-radar-searxng` and
`tech-radar-cache` on ports 8888 and 6381, then removes them. Run it only in an
environment without existing containers or services using those names/ports.

## Release

Independently versioned by the repository release-please configuration,
with tags `tech-radar-vX.Y.Z`. Imported source baseline: 0.5.0,
original commit `a60add16b7bbfb9664eed4d4685d9224baacff22`.
