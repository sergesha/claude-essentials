# Native model providers

The provider seam retains PoC9's two purposes: a native Wiki MCP tool-using agent and structured Utopia/admission calls. `runtime/wiki-agent-codex.py`, `runtime/relation-model-codex.py` and `runtime/model-profiles.json` retain the PoC9 transport and explicit stage profiles. The wrapper supplies explicit executable selection, stage identity and durable per-call evidence; it does not replace native Wiki editing or Utopia parsing.

Profiles select `provider`, `model`, `effort` and `guidance` independently for `wiki`, `open`, `phrase`, `proposal`, `review_extension` and `grounded_answer`. The supplied defaults belong to Codex only. Changing one stage changes that stage's reuse identity without changing unrelated profiles. Optional Wiki task guidance is delivered to the native MCP agent alongside the unchanged upstream guide; it does not edit that guide. The caller's host does not choose a provider implicitly.

Codex uses its existing native authentication. Atlas does not copy login credentials. Calls record the actual CLI version, adapter hash and effective stage profile; failures retain stdout/stderr and request evidence without automatic model retries. Native system/user messages retain PoC9's instruction-file adaptation. Wiki calls retain its guide/search/read/create/edit/append/lint tool loop and explicit completion status.

The installed CLI inspected during implementation was Codex 0.167.0; its `exec --help` exposes ephemeral execution, ignored user configuration, JSON events and output schemas. See the [official Codex execution configuration documentation](https://learn.chatgpt.com/docs/github-action#configure-codex-exec). CLI inspection and deterministic transport tests do not establish authenticated native execution or host/platform qualification.

Claude Code uses ordinary authenticated `claude -p` with an explicit per-call MCP configuration and its own stage profiles. It never probes Codex. Native structured messages preserve their system/user roles; The Atlas Wiki MCP server registers the native handlers, removes tools outside the authoring allowlist, and then starts its stdio session. Both hosts see `guide`, `search`, `read`, `create`, `edit`, `append`, and `lint`. The guide remains the pinned upstream text and can mention capabilities available in other deployments. Built-in tools and unrelated MCP configuration are disabled. It does not use `--bare`, which would change normal authentication behavior.

The inspected Claude Code version is 2.1.319. See the [official CLI reference](https://code.claude.com/docs/en/cli-reference). Configure each selected stage explicitly, for example `{"provider":"claude","model":"sonnet","effort":"high","guidance":""}`; do not reuse Codex model names. Per-call evidence retains stream events, stderr, effective profile and configuration. Deterministic transport checks establish only interface behavior. See [verification.md](verification.md) for the separately recorded native provider and lifecycle results and remaining platform prerequisites. ACP remains deferred; full cross-platform qualification is not claimed.

## Consistent entity naming during updates

The retained native parser rejects incomplete update extraction, including
duplicate entity rows differing only by capitalization. Keep that check. The
Codex qualification profile appends the following to the retained `open.guidance`
through the existing stage configuration; all other stage settings remain
unchanged. The original PoC9 defaults are preserved.

```text
List each entity once, including when a heading or table capitalizes the same name differently. Use one consistent spelling for that entity in e and statement participants. Preserve verbatim quotes. Do not merge distinct referents or discard statements, conditions, uncertainty or source evidence to achieve uniqueness.
```

Changing guidance changes the extraction reuse identity. The retained failed
response remains evidence; this configuration is not an automatic retry or a
post-processing merge of model output.

## Extraction quality fixture

Atlas retains the full Markdown of each Wiki page in Git. The extraction passage omits link-only directory tables and standalone links in named navigation sections such as Map, Related, and See also. Factual tables, prose with links, and fenced examples stay in the passage. Statement payloads from new extractions carry a `modality` list for native mood qualifiers. The native resolver keeps these statements outside factual relation bindings.

Run `python -m pytest -q tests/test_extraction_quality.py` for the controlled PETG fixture. It checks four navigation fragments, five evidence fragments, both native mood representations and reuse after a navigation-only edit. This measures passage selection and representation. General model extraction quality requires separately labeled sources and live evaluations.

The extraction trace includes `cache_reason` for each page. Reuse requires matching filtered input, model/adapter profile, native parser and codec contract, and a complete retained response. The earlier PETG audit recorded five model pages and zero reused pages on its second ingestion. The historical trace leaves the reason for those misses unknown.
