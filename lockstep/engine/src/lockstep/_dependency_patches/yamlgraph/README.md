# yamlgraph 0.5.22 patches

The installer applies and startup verifies both independent patches. Each uses
exact before/after hashes; an existing subgraph-patched installation can receive
the native join patch without rewriting the earlier patch's provenance.

- `0.5.22-subgraph-config.patch` and `manifest.json` retain their original upstream
  issue/comment references and digests.
- `0.5.22-native-join.patch` and `native-join-manifest.json` are a local Lockstep
  compatibility patch. They add explicit list-source edges that call LangGraph's
  native all-source barrier, with schema validation, loop detection, and Mermaid
  rendering. No upstream issue or upstream patch is claimed for these additions;
  the corresponding provenance fields are null.

String-source edges retain their existing semantics. A source list requires
unique ordinary nodes, a single target, and no condition or edge type. Lockstep's
parallel lowerer uses passthrough branch-completion nodes as barrier sources.
