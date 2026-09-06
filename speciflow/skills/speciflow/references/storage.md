# Storage preparation

The private `scripts/storage.py` helper is not a public CLI and owns only
`.speciflow-project.json`.

Select the storage base in this order: an explicit base for this invocation;
the nearest ancestor `.speciflow` directory other than the account default;
then `~/.speciflow`.

The project identity is the Git common directory when the project is in Git,
or the supplied non-Git project root otherwise. The resolver returns distinct
`storage_base`, `data_root`, and `metadata_path` values:

| Resolver value | Meaning |
| --- | --- |
| `storage_base` | Shared base selected above |
| `data_root` | `<storage_base>/projects/<project-key>` — this project's data directory |
| `metadata_path` | `<data_root>/.speciflow-project.json` |

The `<data-root>` label in the other references means this returned `data_root`,
not `storage_base`. Append `planning/` or `beads/` directly to it as described in
[operations.md](operations.md); the `projects/<project-key>` segment is already
included. Use the resolver's literal paths in an exact preview, not paths
reconstructed from labels.
When repeating an observed path or identifier, reproduce it verbatim. For a
brief continuation, refer to the unchanged approved preview instead of
shortening its values with ellipses; repeating the full preview or requesting
approval again is unnecessary.
When presenting a derived native root as a literal target, write its complete
path without ellipses. Compact outlines may instead use explicitly symbolic
targets such as `<data-root>/planning`.

The only SpeciFlow metadata is
the returned `metadata_path`, with exactly this
three-field schema:

```json
{"version":1,"project_identity":"<project identity>","project_key":"<project key>"}
```

It exists only to detect a project-identity collision at that path.

Use the helper only while this skill has been explicitly invoked:

```text
storage.py resolve PROJECT [--base BASE]
storage.py init PROJECT [--base BASE]
```

Resolve, show the exact path and metadata, obtain approval, initialize, then
continue to initialization of the selected native owner. Storage preparation
is not a terminal lifecycle.

Never store workflow, process, or native-owner state in SpeciFlow storage.
