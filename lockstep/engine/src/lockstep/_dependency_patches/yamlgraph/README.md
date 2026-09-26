# YAMLGraph 0.6.0 compatibility patches

YAMLGraph 0.6.0 includes the direct-subgraph registration, OTel config injection,
and child-checkpoint isolation fixes from [issue #474](https://github.com/sheikkinen/yamlgraph/issues/474)
([PR #673](https://github.com/sheikkinen/yamlgraph/pull/673)). Those three source
files are no longer patched by Lockstep. PR #676 also validates subgraph modes
and rejects input/output mappings on direct subgraphs.

The installer applies two remaining patches; startup verifies their exact
before/after hashes without modifying the installation:

- `0.6.0-timeout-config.patch` / `manifest.json`: preserve explicit RunnableConfig
  injection through the timeout wrapper, and copy the current context into its
  worker thread. The latter lets a normal YAML Python node call LangGraph's
  `get_config()` with a timeout enabled. Upstream explicitly excluded the timeout
  change from #474. Our earlier timeout patch forwarded arguments but did not
  preserve this context; the new patch covers both paths.
- `0.6.0-native-join.patch` / `native-join-manifest.json`: retain list-source edges
  that call LangGraph's all-source barrier, including schema validation, loop
  detection, and Mermaid rendering. String-source edges retain OR semantics.
  A source list requires unique ordinary nodes, one target, and no condition or
  edge type. Lockstep uses passthrough branch-completion nodes as barrier sources.

The timeout manifest links to [issue #708](https://github.com/sheikkinen/yamlgraph/issues/708)
and records the published issue body's SHA-256. No complete patch attachment was
published, so its upstream patch/comment fields remain null. The native-join
patch remains local and has null upstream provenance fields. The old #474 comment
is historical context, not the provenance of the expanded timeout patch.
The standalone reproduction and published issue text live in
`lockstep/docs/upstream/yamlgraph-timeout-context/`.

Keep `uv run --no-sync` after installation: a sync/reinstall may restore official
unpatched package bytes, which startup deliberately rejects. Dependency bumps
must revalidate the native capability probe and refresh the patch manifests.
