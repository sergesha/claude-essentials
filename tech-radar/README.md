# Tech Radar Plugin

Claude Code plugin for monitoring tech news across the web.

## Skills

- **configure-topic** — interactively create topic configurations with probe searches
- **collect-news** — collect news, generate summaries, build HTML dashboard

## Requirements

- A container runtime for SearXNG + Dragonfly cache, one of:
  - **Podman with a systemd user manager** (preferred) — the SessionStart hook
    installs Quadlet units into `~/.config/containers/systemd/`, so systemd
    supervises the stack: restart on failure, start on boot (enable linger)
  - **any Docker-compatible CLI with compose** (macOS Docker Desktop, Docker CE) —
    fallback, managed via `plugin/docker-compose.yaml`
  - strategy is auto-detected; override with `TECH_RADAR_STACK=quadlet|compose`
- the `redis-memory` plugin from this marketplace (declared dependency) — provides the redis-memory-mcp tools this plugin's skills call

## Local Use

```bash
cd tech-radar
claude
```

Plugin auto-loads via `.claude/settings.json` with `autoload: true`.
Slash commands: `/tech-radar:configure-topic`, `/tech-radar:collect-news`.

## Remote Install

Add marketplace to Claude Code settings:

```json
{
  "extraKnownMarketplaces": {
    "tech-radar": {
      "source": {
        "source": "git",
        "url": "https://github.com/sergesha/claude-tech-radar.git"
      }
    }
  }
}
```

Then install via `/install tech-radar`.
