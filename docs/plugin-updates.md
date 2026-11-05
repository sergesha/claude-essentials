# Update claude-essentials plugins

Use this procedure for plugin updates in Claude Code and Codex, including
`can't open file .../scripts/code_intel.py` after an update. Updating the
installation and applying it to an open session are separate steps.

## Select the installed components

List installed plugins before updating:

```bash
codex plugin list --marketplace claude-essentials --json
claude plugin list --json
```

For Claude Code, select only IDs ending in `@claude-essentials` and retain each
installation's scope. Update the selected installed plugins, not every available
marketplace entry. Keep enabled/disabled choices, credentials, namespaces,
backend pins and data directories. Code Intel installation/update includes
provisioning its two CLI tools and required extensions, preserving mise version
requests. Data migrations and other plugins' separately installed backends
require their own authorization.

## Claude Code

Refresh the catalog once, then update each selected plugin in its existing scope.
For example, for a user-scoped Code Intel installation:

```bash
claude plugin marketplace update claude-essentials
claude plugin update code-intel@claude-essentials --scope user
```

After the batch, enter `/reload-plugins` directly in each open interactive Claude
Code session. A shell command in another terminal does not reload those sessions.
Check the reload summary and any errors. If the client leaves changes pending
because of prompt-cache invalidation, explain the next-request cost before using
`/reload-plugins --force`.

Non-interactive reload requires Claude Code 2.1.260 or later and does not reconnect
plugin MCP servers; those need a new session. Plugin monitors also require a
session restart. On unsupported clients, restart and resume the conversation.
See [Claude Code activation](https://code.claude.com/docs/en/discover-plugins#apply-plugin-changes-without-restarting)
and [plugin paths](https://code.claude.com/docs/en/plugins-reference#environment-variables).

## Codex

When the client exposes a confirmed connection to the App Server owning the
current session, use its `marketplace/upgrade` request with
`{"marketplaceName":"claude-essentials"}`. This is a client protocol operation,
not a slash command or shell command. In Codex 0.154.0 its handler refreshes
loaded hook runtimes, clears plugin/skill caches, and requests MCP refresh after
successful marketplace upgrades. A separate newly launched App Server cannot
refresh another server's sessions. Verify support in the running version.

If that connection is unavailable, use the CLI fallback from a terminal:

```bash
codex plugin marketplace upgrade claude-essentials --json
codex plugin list --marketplace claude-essentials --json
```

Inspect upgrade errors and installed versions. If a selected installed plugin
still needs installation from the refreshed catalog, use
`codex plugin add <plugin>@claude-essentials` and verify it again. Then restart
the affected client and resume the conversation before using updated plugins.
Do not continue a batch of agent tool calls through hooks bound to removed files;
report the installed state and pending restart if they block execution.

The standalone CLI upgrade in 0.154.0 does not call the live server's hook refresh.
Do not invent `/reload-plugins` for Codex, guess a control socket, or treat
`skills/list forceReload` or MCP-only reload as a complete plugin reload.
See the [App Server protocol](https://developers.openai.com/codex/app-server)
and the 0.154.0 [CLI handler](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/cli/src/marketplace_cmd.rs)
and [server handler](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/app-server/src/request_processors/marketplace_processor.rs).

## Complete Code Intel tool provisioning

After updating Code Intel, resolve `scripts/code_intel.py` inside the installed
version reported by the client and run `python3 /absolute/installed/path/scripts/code_intel.py ensure-tools`.
This step installs/upgrades the tools and verifies igraph in CRG's own Python
environment. Do not treat a plugin version bump as proof of tool readiness.
The new `SessionStart` hook runs the same operation on activation. Both clients
share its success receipt, so successful provisioning is not repeated for the
same release and unchanged tool environment. Failed provisioning is reported and
retried; it does not mark the update complete. Package managers are not installed
automatically, and loaded MCP servers still need reload/restart.

## Verify installation and activation separately

Report each updated plugin's installed version from the host inventory and compare
it with the selected upstream release/catalog. A successful command, cached
directory, or skill-file read alone does not prove activation in an open session.

After reload or restart, inspect the host's loaded component paths and reload
results where available. Exercise one relevant non-destructive hook or MCP
operation when the plugin provides it; check that it uses the current installation
and no longer references the removed path. For a skills-only plugin, verify the
session discovers its updated entrypoint. If evidence is unavailable, say
**installed; session activation unverified** rather than claiming completion.

A missing-installation notice is recovery guidance, not evidence of activation.
Do not recreate old version paths or delete caches to hide the symptom. Existing
sessions with older hook commands may still need one restart to load a guarded
launcher. Standalone skill copies are separate installations: identify and update
them explicitly, preserving a rollback copy and avoiding duplicate plugin skills.
