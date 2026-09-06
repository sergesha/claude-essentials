# Continuous Learning

Capture unexpected tool/API/workflow behavior while working, then promote open
findings into the project's versioned skills, docs or commands. Redis holds open
findings; git holds processed knowledge. The same skill and SessionStart reminder
are used by Claude Code and Codex.

## Install

Configure [redis-memory](../redis-memory-mcp/README.md) first, with a **per-project
`NAMESPACE`**. Keep the existing namespace when upgrading: changing it changes
which findings the client can see. This plugin does not start its own memory
server or migrate stored findings.

### Claude Code

```bash
claude plugin marketplace add sergesha/claude-essentials
claude plugin install redis-memory@claude-essentials \
  --config mode=shared \
  --config redis_url=redis://host.docker.internal:6379/0 \
  --config embed_url=http://host.docker.internal:8081 \
  --config namespace=my-project
claude plugin install continuous-learning@claude-essentials
```

The shared-mode example assumes an existing backend. Use your existing Redis
configuration instead if it differs; retain credentials in the protected client
configuration, never in repository files or pasted command arguments. The
marketplace also declares redis-memory as a Claude dependency, but its default
installation does not select your project namespace.

### Codex

```bash
codex plugin marketplace add sergesha/claude-essentials
codex plugin add redis-memory@claude-essentials
codex plugin add continuous-learning@claude-essentials
```

Before starting Codex, make the existing backend configuration available in the
Codex process environment, for example:

```bash
export REDIS_MEMORY_MCP_MODE=shared
export REDIS_URL=redis://host.docker.internal:6379/0
export EMBED_URL=http://host.docker.internal:8081
export NAMESPACE=my-project
codex
```

Install redis-memory explicitly in Codex; this plugin does not automatically
install dependencies or change their settings. Review and trust the plugin hook
through Codex `/hooks`, then start a new session. An untrusted or disabled hook
cannot provide the automatic startup reminder.

## Use and lifecycle

| Operation | Claude Code | Codex |
| --- | --- | --- |
| Capture a surprise / task-end `learn:` checkpoint | SessionStart guidance | Same SessionStart guidance |
| Propose promotion | `/learn` | `$continuous-learning promote` |
| Preview only | `/learn --dry-run` | `$continuous-learning promote --dry-run` |

Promotion reads findings tagged `continuous-learning`, reviews the project and
git history, and presents a complete changeset for approval. Nothing is applied
or committed before approval. Dry-run also leaves findings untouched. After an
approved promotion, processed findings (including rejected ones) are deleted;
no replacement "resolved" records are saved.

The automatic part is an instruction injected at SessionStart, not a background
collector or a Stop-hook enforcer. The agent performs capture and checkpoints
using that instruction. Memory unavailability remains best-effort and does not
block the main task. These are the existing behavior and limitations on both
clients; manually invoking the skill is not a substitute for enabling the hook.

For cross-client reuse, use the same Redis database and namespace. Learned project
documentation is reused from git; unprocessed findings remain in the same Redis
namespace and retain their existing TTL. No new storage format or policy is added.

## Upgrade

```bash
# Codex
codex plugin marketplace upgrade claude-essentials
codex plugin add continuous-learning@claude-essentials

# Claude Code
claude plugin marketplace update claude-essentials
claude plugin update continuous-learning@claude-essentials
```

Restart the client and retain the existing Redis connection and namespace. Upgrade
redis-memory separately if needed using its documented upgrade commands.
