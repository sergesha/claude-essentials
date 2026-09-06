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

Codex support is tracked separately in
[issue #63](https://github.com/sergesha/claude-essentials/issues/63);
this import does not claim Codex compatibility.

## Requirements

- Podman with a systemd user manager, or Docker with Compose.
- A configured `redis-memory` plugin from this marketplace.
- Python 3 (report scripts use only the standard library).

The SessionStart hook prefers Podman Quadlets, falling back to
`docker-compose.yaml`. Override with `TECH_RADAR_STACK=quadlet|compose`.
Mutable container configuration lives in `CLAUDE_PLUGIN_DATA`, not the plugin
source. Keep existing data when replacing an installation from the old
marketplace; do not run both copies concurrently.

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
