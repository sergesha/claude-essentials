# Local Docker deployment

This path runs the existing Atlas components in one Ubuntu 24.04 ARM64 container.
It uses local image builds and persistent named volumes. Docker is the recommended qualified ARM64 deployment; native installation
remains supported. No service ports are published; MCP attaches through Docker
stdio. The Docker daemon must already be available on the local machine.

The supported initial CPU target is ARM64 on macOS or Linux. Do not enable
Rosetta, force x86 emulation, use privileged mode or mount the Docker socket.
The image includes the qualified toolchain and Codex 0.167.0 / Claude 2.1.319;
provider authentication and runtime preparation are separate from image build.

From the repository root, choose one project name per atlas:

```sh
atlas_compose_file="$PWD/knowledge-atlas/docker/compose.yaml"
atlas_compose() { docker compose -p atlas -f "$atlas_compose_file" "$@"; }
atlas_compose build
atlas_compose run --rm atlas prepare
```

Preparation uses the existing pinned native build and model acquisition recipe.
It needs network access, substantial free disk space and sufficient free Docker
VM memory. The shared 8 GiB qualification VM previously exhausted memory during
compilation/linking; one Cargo job does not guarantee a successful build under
memory pressure. A subsequent fresh preparation passed after temporarily
stopping selected workloads, with about 4.8 GiB available at the start. This is
an observed successful condition, not a guaranteed minimum; inspect actual
memory use and restore any workloads you temporarily stop. Failed builds and
logs remain in the runtime volume. The
existing fresh-directory rule applies; do not erase that volume to hide a
failure. Retained/recovered runtime verification is not a clean-install result.
Startup only validates prepared artifacts; it never silently downloads/builds.

## Explicit stage profiles and native sign-in

Create `/data/profiles.json` with all six stage profiles before startup. For
Codex, this example copies the retained default stage settings and guidance:

```sh
atlas_compose run --rm atlas exec python3 -c 'import json; p=json.load(open("/opt/knowledge-atlas/runtime/model-profiles.json")); p={k:{**v,"provider":"codex"} for k,v in p.items()}; json.dump(p,open("/data/profiles.json","w"),indent=2)'
atlas_compose run --rm atlas exec codex login --device-auth
```

For Claude, use its own explicit models/settings and native subscription login:

```sh
atlas_compose run --rm atlas exec python3 -c 'import json; p=json.load(open("/opt/knowledge-atlas/runtime/model-profiles.json")); p={k:{**v,"provider":"claude","model":"sonnet","effort":"high"} for k,v in p.items()}; json.dump(p,open("/data/profiles.json","w"),indent=2)'
atlas_compose run --rm atlas exec claude auth login --claudeai
```

These are alternative initial configurations; editing profiles affects future
processing/reuse identity. See [provider guidance](../docs/providers.md),
including the qualified Codex update guidance. Sign in only the selected
provider, or both when using mixed stage profiles. Login state lives under
`/auth` in its own volume. Do not copy host credential files, bake credentials
into images or include the auth volume in Atlas exports.

```sh
atlas_compose up -d --wait --wait-timeout 150
atlas_compose logs --tail 50 atlas
atlas_compose exec -T atlas /opt/knowledge-atlas/.venv/bin/python -m knowledge_atlas.cli status
```

## Use the installed plugin

Set the project name in the environment of the native host before launching it:

```sh
export KNOWLEDGE_ATLAS_DOCKER_PROJECT=atlas
codex   # or: claude
```

The installed Knowledge Atlas plugin's existing launcher then calls
`docker compose exec -T` and the same Python MCP entry point inside the running
service. It preserves protocol stdout; component logs stay under `/data/logs`.
Docker is required only when this option is set. Unset it to use native launch.
The container must already be healthy; an absent/stopped project produces a
Docker error rather than creating an unconfigured service implicitly.

Create or restore the atlas at **`/data/atlas`**. This deployment binds its
embedding cache to that atlas. Use another Compose project for another atlas.
Source, archive and child-package paths supplied to tools are **container paths**.
Source content can be submitted through the normal API. For local files, copy
only the intended inputs, for example:

```sh
atlas_compose exec -T atlas mkdir -p /data/imports /data/exports
atlas_compose cp ./source.txt atlas:/data/imports/source.txt
atlas_compose cp atlas:/data/exports/atlas.zip ./atlas.zip
```

Canonical export/restore remains the existing Atlas API contract. Restore into
an unused atlas path in another project; do not overwrite an existing atlas.
No arbitrary host directory is shared automatically.

Python/HTTP callers execute inside the container, preserving loopback binding.
For example, inspect the shared service with its normal Python client:

```sh
atlas_compose exec -T atlas /opt/knowledge-atlas/.venv/bin/python -c 'from pathlib import Path; from knowledge_atlas.client import AtlasClient; print(AtlasClient(Path("/data/service")).request("/v1/status"))'
```

Other local HTTP programs inside the container obtain the endpoint from
`/data/service/discovery.json` and bearer token from the owner-only
`/data/service/token` file as described in [the API](../docs/api.md). Do not print
or place the token in a URL. There is no host-facing HTTP port in this recipe.

## Browse and review locally

After the atlas exists, launch the [viewer](../docs/viewer.md) from the host:

```sh
sh /absolute/path/to/knowledge-atlas/scripts/launch.sh view --atlas /data/atlas --docker-project atlas
```

Open the full localhost URL printed by that command. Browser pages and questions
use the same backend as the installed Codex and Claude plugins.

## Future versioned GHCR images

This recipe builds a **build-capable image**; runtime preparation still compiles
native components and downloads pinned model artifacts. Publishing that image
alone would not remove preparation. Ready-to-use GHCR images need a prebuilt,
validated runtime, stable interpreter paths, volume initialization/version checks,
version/revision tags and immutable digests, and qualification for each advertised
architecture. Data and credentials must remain external. Public distribution also
requires a complete license/feature inventory for bundled CLIs, VectorLink modules,
Gemma weights and transitive dependencies. No image is published by this increment.

## Stop, replace and retain data

```sh
atlas_compose stop
atlas_compose up -d --wait --wait-timeout 150
# After building a replacement image:
atlas_compose up -d --force-recreate --wait --wait-timeout 150
# Remove the disposable container/network, retaining volumes:
atlas_compose down
```

Do **not** add `-v` when retaining data. The project owns separate `runtime`,
`data` and `auth` volumes. `data` contains canonical Git packages, vectors,
operation history, private service token and durable TerminusDB state. Runtime
replacement never reinitializes an existing native database. Unexpected child
exit fails the deployment and preserves logs; shutdown terminates its children.
Use the Atlas operation/status interfaces to inspect interrupted work before
explicit compatible resume, rather than blindly repeating a mutation.

Docker replacement does not replace backups. Keep exported archives outside
Docker using the documented copy command. Canonical exports do not replace a
full backup of pending work, service evidence, graph state and private auth.
Normal native sign-in can renew provider credentials, but the auth volume must
not be classified as a disposable cache.

## Qualification boundary

See [verification](../docs/verification.md) for actual image, runtime, MCP,
replacement and provider results. A build or healthy container alone is not
full provider/lifecycle qualification. The new deployment's login volume starts
empty; existing login state from the qualification container is not copied.

## Storage ownership and optional host folders

All persistent data uses named volumes by default. A named volume is outside the
container layer, but is not a backup and is not automatically disposable.

| Contents | Default location | Retention |
| --- | --- | --- |
| Atlas Git repositories, sources, accepted outputs, provenance, decisions and vectors | `data:/data/atlas` | Durable; preserve actual results |
| TerminusDB database | `data:/data/graph` | Durable; do not delete on the assumption that LLM regeneration restores it |
| Pending operations, attempt/read evidence, service identity and logs | `data:/data/service` and other `/data` paths | Preserve pending work and audit evidence |
| Provider/model configuration | `data:/data/profiles.json` | Durable, outside image layers |
| Provider credentials and provider home | `auth:/auth` | Private durable state; never commit to Git |
| Prepared runtime and downloaded models | `runtime:/runtime` | Reproducible from pinned inputs, with build/download cost |
| Export and full-backup copies | Explicit destination outside Docker | Retain independently of Docker storage |

Only verified derived indexes and temporary files are disposable. Do not equate
re-running Wiki/Utopia/LLM extraction with restoring the same accepted version.
The prepared Python environment can reference an interpreter under
`/auth/.local/share/uv/python`; retain that dependency alongside `/runtime`.
Preserving only a runtime volume does not prove that it is independently usable.

For a **new deployment**, choose either override below. Keep the same Compose
project name and the same override on subsequent management commands. Required
host directories must already exist. No mounts expose arbitrary host paths.

**All application data on the host**, including Git, TerminusDB and operation
state; runtime and authentication remain in their named volumes:

```sh
mkdir -p "$HOME/knowledge-atlas/data"
export ATLAS_HOST_DATA="$HOME/knowledge-atlas/data"
atlas_compose() { docker compose -p atlas -f "$atlas_compose_file" -f "$(dirname "$atlas_compose_file")/compose.host-data.yaml" "$@"; }
atlas_compose config
# Continue the build/prepare/profile/login/start steps above.
```

**Only the atlas repository on the host**, with graph/service state still in the
named data volume:

```sh
mkdir -p "$HOME/knowledge-atlas/atlas"
export ATLAS_HOST_ATLAS="$HOME/knowledge-atlas/atlas"
atlas_compose() { docker compose -p atlas -f "$atlas_compose_file" -f "$(dirname "$atlas_compose_file")/compose.host-atlas.yaml" "$@"; }
atlas_compose config
```

Select one override, not both. Container paths stay `/data/atlas`, `/data/graph`
and `/data/service`; MCP attachment still selects the running project by name.
Submodules must be materialized within the mounted tree; host paths outside it
are not automatically accessible. Do not run concurrent host and container writers.
Database files and provider credentials do not belong in Git.

For an **existing deployment**, first drain active work, stop all writers, and
back up the original data. Deliberately copy the full selected directory into the
host destination, including hidden Git files and permissions, before changing
mounts. A new empty bind mount hides volume contents; it does not migrate them.
Keep the original volume until the migrated deployment and recovery have been
verified. There is no automatic migration or volume deletion in these overrides.
A bounded macOS/Rancher Desktop host-mount test passed two TerminusDB process
SIGKILL/restart cycles, exact historical graph/vector preservation, an interrupted
transaction with no partial visibility, and a subsequent write. This is process
crash evidence, not a throughput benchmark or host/Docker-VM power-loss guarantee.
See [verification](../docs/verification.md#host-mounted-terminusdb-process-crash-recovery).

## Full durable backup versus canonical export

Use canonical Atlas exports for their documented portable knowledge/history
contract. For deployment recovery including pending operations and TerminusDB,
retain a consistent full copy of `/data` and private `/auth`, plus the Compose
configuration, image/version identity and runtime manifest. Include any nested
host atlas mount separately: backing up only the underlying data volume misses
its mounted contents. Drain operations and stop all writers before filesystem
copying; do not copy a live database directory as a purported consistent backup.
Use an archive tool that preserves hidden files, ownership and permissions; keep
credential-bearing backups private and outside Docker's storage. Preserve the
runtime too when avoiding rebuilds or retaining its interpreter dependency is
required. Restore into an isolated deployment and verify before replacing the
original. These instructions do not claim a full stopped-state restore has been
qualified; canonical export/restore evidence is documented separately.
