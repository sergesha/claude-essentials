# Docker installation

The release target is Linux ARM64 on a native ARM64 Docker host. Image delivery
requires a published, anonymously verified digest for the selected Atlas release.
The 0.4.0 public image passed anonymous pull and native pipeline qualification.
Use its [recorded digest](../docs/verification.md#atlas-040-public-delivery).

## Pull the release image

From the repository root, use the Compose file for the matching plugin release.
Choose one project name for this service, its data and its provider login state.

```sh
atlas_compose_file="$PWD/knowledge-atlas/docker/compose.yaml"
atlas_compose() { docker compose -p atlas -f "$atlas_compose_file" "$@"; }
export ATLAS_IMAGE_DIGEST='sha256:b8e5f1d2e7c02ffa2a22ce7f5812642f681af3a97385c9e3d32177e60d0b5653'
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
ships the exact patches and supplemental Lance, Tantivy/Snowball and Tokenizers notices under
`/opt/knowledge-atlas/runtime/patches` and `/opt/knowledge-atlas/docker/licenses`.
Wheel licenses and third-party notices remain with the installed packages. Public
release qualification also needs the actual image's linked libraries and SWI
extensions checked against their notice and corresponding-source requirements.

## Rebuild the graph with modified GMP or Rug

**The native graph library uses Rug 1.19.2 and statically links GNU GMP 6.2.1
through gmp-mpfr-sys 1.5.3.** SWI-Prolog also uses the system GMP shared
library. To change the GMP code embedded in `terminusdb/src/rust/librust.so`,
rebuild the Rust graph library. Rug and gmp-mpfr-sys are distributed under
LGPL-3.0-or-later. Their source archives contain the LGPL and GPL texts.
The bundled GMP source includes its own licensing terms in `gmp-6.2.1-c/README`
and `COPYING*`.

On a native Linux ARM64 builder with the prerequisites for the explicit local
build above, start from the exact image and its matching repository revision.
Set `atlas_image` to the local image ID or a previously pulled qualified digest.
From the Atlas Git repository root:

```sh
set -eu
atlas_recipient="$(mktemp -d)"
atlas_revision="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$atlas_image")"
git worktree add --detach "$atlas_recipient/tree" "$atlas_revision"
cd "$atlas_recipient/tree"
atlas_src="$PWD/knowledge-atlas/docker/recipient"
mkdir -p "$atlas_src" "$atlas_recipient/materials"
docker run --rm --network none --entrypoint tar "$atlas_image" \
  -C /usr/share/doc/knowledge-atlas -cf - cargo-inventory.json corresponding-source |
  tar -xf - -C "$atlas_recipient/materials"
python3 - "$atlas_recipient/materials" <<'PY'
import hashlib, json, sys
from pathlib import Path
materials = Path(sys.argv[1])
inventory = json.loads((materials / 'cargo-inventory.json').read_text())
for name in ('gmp-mpfr-sys-1.5.3', 'rug-1.19.2'):
    entry, = (v for k, v in inventory.items() if k.endswith('/' + name))
    archive = materials / 'corresponding-source' / (name + '.crate')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != entry['sha256']:
        raise SystemExit(f'Archive checksum mismatch: {name}')
PY
tar -xf "$atlas_recipient/materials/corresponding-source/gmp-mpfr-sys-1.5.3.crate" -C "$atlas_src"
tar -xf "$atlas_recipient/materials/corresponding-source/rug-1.19.2.crate" -C "$atlas_src"
```

Edit the GMP sources in `$atlas_src/gmp-mpfr-sys-1.5.3/gmp-6.2.1-c` or the
Rust sources in `$atlas_src/rug-1.19.2`. For compatible modifications, keep
the crate names, versions and dependency declarations. Cargo can then resolve
these local sources against the distributed lockfile. Disable reuse of earlier
native library builds in the crate's private cache. [Cargo path overrides](https://doc.rust-lang.org/cargo/reference/overriding-dependencies.html#paths-overrides)

```sh
cat > "$atlas_src/cargo-config.toml" <<'TOML'
paths = ["/opt/knowledge-atlas/docker/recipient/gmp-mpfr-sys-1.5.3", "/opt/knowledge-atlas/docker/recipient/rug-1.19.2"]

[env]
GMP_MPFR_SYS_CACHE = { value = "_", force = true }
TOML
```

In this private copy of `knowledge-atlas/docker/Dockerfile`, insert the following
instruction immediately after `COPY docker ./docker` in the builder stage.
Cargo reads this configuration from an ancestor of both native workspaces.

```dockerfile
COPY docker/recipient/cargo-config.toml /opt/.cargo/config.toml
```

Commit the changes on the private checkout, archive that exact source, and
build a separately tagged image. The original Dockerfile's locked Cargo
build, Atlas patches and seed-manifest generation now run with the selected
library sources.

```sh
git add -- knowledge-atlas/docker/Dockerfile
git add -f -- knowledge-atlas/docker/recipient
git commit -m "Build the Atlas graph with recipient library changes"
export ATLAS_REVISION="$(git rev-parse HEAD)"
export ATLAS_VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' knowledge-atlas/pyproject.toml)"
mkdir "$atlas_recipient/snapshot"
git archive "$ATLAS_REVISION" knowledge-atlas | tar -x -C "$atlas_recipient/snapshot"
cd "$atlas_recipient/snapshot/knowledge-atlas"
docker build --platform linux/arm64 --build-arg ATLAS_REVISION --build-arg ATLAS_VERSION \
  -t knowledge-atlas:recipient-gmp -f docker/Dockerfile .
for atlas_build in "$atlas_image" knowledge-atlas:recipient-gmp; do
  docker run --rm --network none --entrypoint sha256sum "$atlas_build" \
    /opt/atlas-seed/terminusdb/src/rust/librust.so
done
```

Exercise the change using fresh deployment storage. Choose an unused Compose
project and runtime volume. This overlay selects the private image for the
usual prepare, provider login and startup commands.

```sh
cat > "$atlas_recipient/compose.recipient.yaml" <<'YAML'
services:
  atlas:
    image: knowledge-atlas:recipient-gmp
    pull_policy: never
YAML
export ATLAS_RUNTIME_VOLUME=atlas_recipient_gmp_runtime
atlas_compose() { docker compose -p atlas-recipient-gmp -f "$atlas_recipient/snapshot/knowledge-atlas/docker/compose.yaml" -f "$atlas_recipient/compose.recipient.yaml" "$@"; }
atlas_compose run --rm --no-deps atlas prepare
```

Follow the profile and startup steps below and check a graph operation that
exercises your library change. Retain the source revision, original and
modified library hashes, and the observed result. Dependency selection was
checked against the packaged ARM64 graph lockfile. Qualification of a
distinguishable rebuilt library and its behavior remains pending.

## Explicit stage profiles and native sign-in

Without `/data/profiles.json`, preparation and startup use the six shipped Codex
profiles. Sign in to Codex in the deployment:

```sh
atlas_compose run --rm atlas exec codex login --device-auth
```

To use Claude, supply all six stages in `/data/profiles.json` with explicit
providers. Set the profiles before preparation so it installs the selected
provider. Use Claude's native subscription login:

```sh
atlas_compose run --rm atlas exec python3 -c 'import json; p=json.load(open("/opt/knowledge-atlas/runtime/model-profiles.json")); p={k:{**v,"provider":"claude","model":"sonnet","effort":"high"} for k,v in p.items()}; json.dump(p,open("/data/profiles.json","x"),indent=2)'
atlas_compose run --rm atlas exec claude auth login --claudeai
```

The login command acquires the selected vendor's pinned Linux executable via
npm over HTTPS. Its package lock checks the wrapper and platform package
integrity. Codex 0.167.0 and Claude Code 2.1.319 are installed separately under
`/runtime/providers`. Preparation installs the providers selected by the effective
profiles. Retry preparation or the chosen login after resolving registry access.
Provider credentials remain in the private `/auth` volume. [Codex authentication](https://developers.openai.com/codex/auth/) · [Claude Code setup and terms](https://code.claude.com/docs/en/setup)

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

Find the registered UUID with `atlas_atlas_list` and prepare the host viewer
dependencies once. Launch the viewer from the host. [Viewer guide](../docs/viewer.md)

```sh
sh /absolute/path/to/knowledge-atlas/scripts/launch.sh prepare-launcher
sh /absolute/path/to/knowledge-atlas/scripts/launch.sh view --atlas REGISTERED_PROJECT_UUID --container-name atlas-atlas-1
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
| Project Git repositories, sources, accepted outputs, provenance, decisions and publication vectors | Each project directory under `/data`, including a project explicitly created at `/data/atlas` | Durable; preserve actual results |
| Shared embedding cache | `data:/data/service/vectors` | Retain verified cached vectors across restart |
| TerminusDB database | `data:/data/graph` | Durable; do not delete on the assumption that LLM regeneration restores it |
| Pending operations, attempt/read evidence, service identity and logs | `data:/data/service` and other `/data` paths | Preserve pending work and audit evidence |
| Optional provider/model overrides | `data:/data/profiles.json` | Durable, outside image layers |
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

### Reattaching current storage

Drain operations, stop all writers and take the full stopped-state backup
described below. To replace the container, select a matching application image,
the same Compose project name, and the same current data, runtime, auth and
project mounts. Each registered UUID opens its recorded location and graph
database. The shared bridge cache verifies retained vectors before startup.
Keep `/data/graph/admin.key` and `/data/graph/atlas-format.json` with the graph
database and VectorLink state.

A fresh deployment can start with the supplied profiles, prepared runtime and
provider login. It creates an empty service registry. An unmarked database,
previous single-project store, invalid vector cache or graph missing its key
stops startup before Atlas changes credentials or starts components. Keep the
original mounts and stopped backup intact. Initialize a new deployment on
fresh storage to create projects under the coordinated contract.

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

When changing mount locations for a **current-format deployment**, drain active
work, stop all writers and back up the data. Copy the full selected directory,
including hidden Git files and permissions, into the host destination before
changing mounts. An empty bind mount hides existing volume contents. Keep the
original volume until the reattached service and its recovery have been verified.
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
