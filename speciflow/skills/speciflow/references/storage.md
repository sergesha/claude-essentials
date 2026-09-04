# Storage preparation

The private `scripts/storage.py` helper is not a public CLI and owns only
`.speciflow-project.json`.

Select the storage base in this order: an explicit base for this invocation;
the nearest ancestor `.speciflow` directory other than the account default;
then `~/.speciflow`.

The project identity is the Git common directory when the project is in Git,
or the supplied non-Git project root otherwise. Store project data at
`<base>/projects/<project-key>`.

The only SpeciFlow metadata is
`<base>/projects/<project-key>/.speciflow-project.json`, with exactly this
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
