# Docker installation

The release target is Linux ARM64 on a native ARM64 Docker host. Image delivery
requires a published, anonymously verified digest for the selected Atlas release.
The 0.4.0 image and its native pipeline are pending qualification. Use the
release's recorded image digest when available.

## Pull the release image

From the repository root, use the Compose file for the matching plugin release.
Choose one project name for this service, its data and its provider login state.

```sh
atlas_compose_file="$PWD/knowledge-atlas/docker/compose.yaml"
atlas_compose() { docker compose -p atlas -f "$atlas_compose_file" "$@"; }
export ATLAS_IMAGE_DIGEST='sha256:REPLACE_WITH_THE_QUALIFIED_RELEASE_DIGEST'
atlas_compose pull atlas
atlas_compose run --rm --no-deps atlas prepare
```

Compose defaults to the versioned public GHCR tag. With the release digest set,
Docker verifies the content digest on pull. Keep the digest in this deployment's
environment. If the tag, digest or architecture is unavailable, the pull fails.
Choose the explicit build below on native ARM64 or use the [native installation](../docs/installation.md).
Installing the plugin prepares its MCP Python dependencies on first use. Image
acquisition, container startup and MCP attachment are separate operator actions.

## Explicit local build

Use the same Dockerfile as the release job. Select the matching source commit
and build its committed files from a clean snapshot. From the repository root:

```sh
export ATLAS_REVISION="$(git rev-parse HEAD)"
atlas_snapshot="$(mktemp -d)"
git archive "$ATLAS_REVISION" knowledge-atlas | tar -x -C "$atlas_snapshot"
atlas_compose_file="$atlas_snapshot/knowledge-atlas/docker/compose.yaml"
export ATLAS_VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$atlas_snapshot/knowledge-atlas/pyproject.toml")"
atlas_build_file="$atlas_snapshot/knowledge-atlas/docker/compose.build.yaml"
atlas_compose() { docker compose -p atlas -f "$atlas_compose_file" -f "$atlas_build_file" "$@"; }
atlas_compose build atlas
atlas_compose run --rm --no-deps atlas prepare
```

Retain the snapshot while using those Compose files. The build needs Internet
access, disk for native source and build trees, and enough free Docker guest
memory to compile and link the locked Rust graph engine. A shared 8 GiB guest
exhausted memory compiling Lance with one Cargo job. A failed build leaves the
release unqualified. The local tag stays on this Docker host.

Both image paths run as UID/GID 1000, publish no ports and require no Docker
socket mount, elevated capabilities or emulation. The image contains the
prebuilt Community graph, extractor, patched sources, interpreter and embedding
packages. During prepare it links them at their stable image paths and downloads
the three EmbeddingGemma Q4 files from the pinned ONNX Community revision into
`/runtime/model`. Setup checks each SHA-256. It needs HTTPS access to Hugging
Face and its download hosts. The selected revision was available anonymously.
If access, storage or a hash check fails, inspect the error. Retry a network
interruption with the same volume. A hash mismatch requires investigation and
a fresh runtime volume. Startup validates the completed seed and requires the
matching application version, source revision and runtime manifest. [Pinned hashes](../runtime/gemma-runtime-artifacts.json) · [Model revision](https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX/tree/5090578d9565bb06545b4552f76e6bc2c93e4a66) · [Gemma terms](https://gemma.ai/resources/terms/)

The image build retains component revisions, native hashes, source inventories
and toolchain versions in `/opt/atlas-seed/runtime.json`. The checked-out
Community sources include their licenses and identify the files Atlas patches.
The SWI source archive, Cargo notices, source inventory and corresponding
crate archives are under `/usr/share/doc/knowledge-atlas`. The application
ships the exact patches and supplemental Lance and Tokenizers licenses under
`/opt/knowledge-atlas/runtime/patches` and `/opt/knowledge-atlas/docker/licenses`.
Wheel licenses and third-party notices remain with the installed packages. Public
release qualification also needs the actual image's linked libraries and SWI
extensions checked against their notice and corresponding-source requirements.

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

The login command acquires the selected vendor's pinned Linux executable via
npm over HTTPS. Its package lock checks the wrapper and platform package
integrity. Codex 0.167.0 and Claude Code 2.1.319 are installed separately under
`/runtime/providers`. A prepare with existing profiles also installs all their
selected providers. Retry prepare or the chosen login after resolving registry
access. Provider credentials remain in the private `/auth` volume. [Codex authentication](https://developers.openai.com/codex/auth/) · [Claude Code setup and terms](https://code.claude.com/docs/en/setup)

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

Get your running Docker container's exact name:

```sh
docker ps --format '{{.Names}}'
```

Call `atlas_atlas_connect` first on a fresh MCP connection, substituting that
name for `atlas-atlas-1`:

```json
{"payload":{"container_name":"atlas-atlas-1"}}
```

The call checks the authenticated API and matching version and returns the
pinned container ID and Atlas version. Start the container with the deployment
commands above. Call `atlas_atlas_list` without arguments to see registered
projects, then verify a project with `atlas_atlas_open` and
`{"atlas_id":"REGISTERED_PROJECT_UUID"}`. Pass that UUID to tools such as
`atlas_atlas_status`. Use another MCP connection for another service or a
replacement container.

Create or restore each project at its own unused path on the data volume, for
example `/data/projects/notes` or `/data/projects/library`. The data mount itself
and its `service`, `graph` and `logs` directories are reserved. Select the resulting
UUID for project actions. The service assigns each project its own database on
the shared graph endpoint. All projects use the same embedding bridge and serial
publication dispatcher.
Source, archive and child-package paths supplied to tools are **container paths**.
Source content can be submitted through the normal API. For local files, copy
only the intended inputs, for example:

```sh
atlas_compose exec -T atlas mkdir -p /data/imports /data/exports
atlas_compose cp ./source.txt atlas:/data/imports/source.txt
atlas_compose cp atlas:/data/exports/atlas.zip ./atlas.zip
```

Canonical export/restore uses the existing Atlas API contract. Restore into an
unused destination with a compatible project registration.
No arbitrary host directory is shared automatically.

Python/HTTP callers execute inside the container, preserving loopback binding.
For example, inspect the shared service with its normal Python client:

```sh
atlas_compose exec -T atlas /opt/knowledge-atlas/.venv/bin/python -c 'from pathlib import Path; from knowledge_atlas.client import AtlasClient; print(AtlasClient(Path("/data/service")).request("/v1/status"))'
```

Other local HTTP programs inside the container obtain the endpoint from
`/data/service/discovery.json` and bearer token from the owner-only
`/data/service/token` file. Verify the service on the same connection before sending
the token, following [the local API handshake](../docs/api.md#shared-http-python-and-mcp-interfaces). Do not print
or place the token in a URL. There is no host-facing HTTP port in this recipe.

## Browse and review locally

After the atlas exists, prepare the host viewer dependencies once, then launch
the [viewer](../docs/viewer.md) from the host:

```sh
sh /absolute/path/to/knowledge-atlas/scripts/launch.sh prepare-launcher
sh /absolute/path/to/knowledge-atlas/scripts/launch.sh view --atlas /data/projects/notes --container-name atlas-atlas-1
```

Open the full localhost URL printed by that command. Browser pages and questions
use the same backend as the installed Codex and Claude plugins.

## Stop, replace and retain data

```sh
atlas_compose stop
atlas_compose up -d --wait --wait-timeout 150
# After preparing the selected replacement image and runtime volume:
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
| Project Git repositories, sources, accepted outputs, provenance, decisions and publication vectors | Each project directory under `/data`, including the original `/data/atlas` | Durable; preserve actual results |
| Shared embedding cache | `data:/data/service/vectors` | Retain verified cached vectors across restart |
| TerminusDB database | `data:/data/graph` | Durable; do not delete on the assumption that LLM regeneration restores it |
| Pending operations, attempt/read evidence, service identity and logs | `data:/data/service` and other `/data` paths | Preserve pending work and audit evidence |
| Provider/model configuration | `data:/data/profiles.json` | Durable, outside image layers |
| Provider credentials and provider home | `auth:/auth` | Private durable state; never commit to Git |
| Prepared runtime and downloaded models | `runtime:/runtime` | Image-bound native links, verified model downloads and selected provider installations |
| Export and full-backup copies | Explicit destination outside Docker | Retain independently of Docker storage |

Only verified derived indexes and temporary files are disposable. Do not equate
re-running Wiki/Utopia/LLM extraction with restoring the same accepted version.
The image supplies the pinned Python interpreter under `/opt/uv/python`.
The runtime exposes the image’s Python packages through `/runtime/python`. Replacing the provider login
volume no longer removes the runtime interpreter; retain that volume for login
state. The graph's administrator key lives in `/data/graph/admin.key`. Keep it
private and include it in a full backup of graph state.

### Replacing a single-atlas container

Drain operations, stop all writers and take the full stopped-state backup
described below **before** replacing the application. Include the old image
identity and all mounted atlas directories. Reuse the same Compose project
name, data and auth volumes and bind mounts. A runtime from the previous
release or a different build is rejected. Preserve that volume for rollback
and select an unused runtime volume before preparing the replacement:

```sh
export ATLAS_RUNTIME_VOLUME=atlas_runtime_040
atlas_compose run --rm --no-deps atlas prepare
atlas_compose up -d --wait --wait-timeout 150
```

Keep that runtime volume selected on future commands. At startup the supervisor checks the repository
at `/data/atlas`, its committed UUID, READY receipt and graph ownership before
registering it in place. The old deployment's database stays `admin/atlas`.
New projects receive separate UUID-derived databases. A fresh deployment starts
with an empty inventory.

The upgrade copies verified cache entries and canonical publication vectors
to `/data/service/vectors`. The original cache, Git refs, graph state and
authentication remain on their existing volumes. If adoption fails, keep the
deployment stopped and inspect the startup error. Check that all original mounts,
Git objects, graph ownership and cache integrity match the backup. Correct the
cause and restart using the same volumes. Verify the listed UUID, READY and a
historical publication before retiring the snapshot. To roll back, restore the
complete stopped pre-upgrade snapshot with the matching old application.

### Upgrading a 0.3.0 deployment

Drain accepted operations, stop all writers, and back up the persistent volumes
and any bind mounts. Select the new image using the existing project name.
Earlier images wrote root-owned files. For the original named-volume layout,
transfer ownership once, while the deployment is stopped:

```sh
atlas_compose stop
export ATLAS_RUNTIME_VOLUME=atlas_runtime_040
atlas_compose run --rm --no-deps --user 0 atlas exec find /data /auth -xdev -uid 0 -exec chown -h 1000:1000 '{}' +
atlas_compose run --rm --no-deps atlas prepare
atlas_compose up -d --wait --wait-timeout 150
```

For bind mounts, give UID 1000 access to the intended host paths through their
host ownership or ACLs before starting. Include separately mounted atlas paths.
Startup validates the freshly seeded runtime and rotates the legacy database's admin
password offline. Existing publications, indexes, provider login, and native
packages stay on their original volumes. Confirm that status returns the
expected READY and that an exact historical publication is readable before
retiring the backup.

For a **new deployment**, choose either override below. Keep the same Compose
project name and the same override on subsequent management commands. Required
host directories must already exist and be writable by UID 1000. No mounts expose arbitrary host paths.

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
and `/data/service`; MCP attachment selects the running service by container name.
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
runtime too when avoiding rebuilds is required. Restore into an isolated deployment and verify before replacing the
original. These instructions do not claim a full stopped-state restore has been
qualified; canonical export/restore evidence is documented separately.
