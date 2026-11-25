# Installation

The [Docker guide](../docker/README.md) describes the versioned GHCR pull and
the explicit, same-source ARM64 local build. Check that the selected release
has a qualified image digest before using its pull command. The 0.4.0 image
is pending qualification.

## Native installation without Docker

On supported macOS or Linux, install uv, the native build prerequisites below,
and the vendor CLI for each selected provider. From the package directory run
`uv sync --locked` and use the resulting `.venv/bin/knowledge-atlas` command
(or activate `.venv` for the examples below). Native preparation, component
startup and MCP operation run directly on the host.

The runtime recipe packages PoC9's retained native build: Wiki-LLM's local SQLite/MCP mode, the unchanged Utopia extraction library through its existing Rust wrapper, patched TerminusDB/embedded VectorLink/Lance, and the exact Gemma Q4 artifacts. It does not require Redis, PostgreSQL or the full Utopia server. Source revisions, patches and acquisition provenance are in `runtime/components.json`, `runtime/patches/manifest.json` and `runtime/source-manifest.json`.

Create a configuration outside the plugin:

```json
{"runtime_root": "/absolute/path/to/new-runtime"}
```

Then run in the locked package environment:

```sh
knowledge-atlas runtime prepare --config /absolute/path/to/runtime-config.json
knowledge-atlas runtime check --config /absolute/path/to/runtime-config.json
```

Preparation requires Git, Cargo/Rust, C/C++ tools, CMake, Ninja, make, m4, protoc with the standard Protobuf definitions, pkg-config and uv on PATH, plus the upstream native libraries required by SWI-Prolog and the Rust build. Missing tools fail before acquisition; missing native libraries fail with the retained build log. No system package manager is run. PoC9's observed macOS toolchain was Rust 1.99.1, protoc 36.2, CMake 4.0.3, Ninja 1.11.1.4 and SWI-Prolog 10.0.2. A recorded version is not evidence of a completed build on another target.

On Linux ARM64, the TerminusDB build uses one code-generation unit for the locked `lance-bitpacking` crate to address the observed compiler memory failure. This leaves other targets, source pins and the retained single-job/debug settings unchanged. The effective Cargo command is retained in `prepare.log`.

Preparation clones exact source revisions, checks and applies retained patches, builds SWI-Prolog from its verified archive, builds TerminusDB and the unchanged extraction wrapper, downloads hash-verified model files, and creates a separate Python environment. Preserve `prepare.log` after a failure. A nonempty unrecognized destination is not overwritten. Repeating preparation of a completed runtime validates it; it does not silently rebuild or replace it.

The manifest records build artifact hashes, actual source inventories, toolchain versions and Python dependencies. Validation checks the model/profile pins, loaded native library, Wiki assets and prepared environment. Validation does not demonstrate semantic quality, a successful model call, or historical search equivalence; those require native qualification.

Keep atlas data and service configuration outside the plugin and runtime installation. The recommended custom Docker image packages the same qualified native components; see [the Docker guide](../docker/README.md). Upstream images alone do not supply PoC9's retained patches or establish equivalent behavior. A hosted Wiki/full-Utopia container remains a different deployment.

## Plugin service and host launch

Install `uv` and Python 3.12, then install this plugin through your host's local
marketplace workflow. Claude Code and Codex start the bundled `scripts/launch.sh`
automatically. On the first MCP connection the script uses `uv sync --locked`
to install the dependencies from `uv.lock` in a version-specific environment
under `${XDG_CACHE_HOME:-~/.cache}/knowledge-atlas`. Later connections reuse
that environment. An interrupted setup is retried on the next connection.
The first connection after a version update prepares that version's environment.

An explicitly configured `KNOWLEDGE_ATLAS_PYTHON`, a checkout with its own
`.venv`, or a compatible `knowledge-atlas` command on `PATH` can also provide
the interpreter. To prepare the dependencies ahead of time, run
`sh scripts/launch.sh prepare-launcher` from the installed plugin directory.
The Codex registration uses `cwd: "."` resolved by the installed plugin and a
relative launcher path. Codex CLI 0.167.0 passed placeholder strings literally
to `sh` during native qualification, although [OpenAI's desktop MCP packaging guide](https://developers.openai.com/plugins/deploy/desktop-only) documents `${PLUGIN_ROOT}`.
Claude uses `${CLAUDE_PLUGIN_ROOT}`.

For Docker, call `atlas_atlas_connect` with the exact running Docker container
name in `payload.container_name` on a fresh MCP connection. List its registered
projects with `atlas_atlas_list` without arguments, then open a project with its
`atlas_id` UUID. See the [Docker guide](../docker/README.md#use-the-installed-plugin).

When upgrading, clear the old Compose selectors from the host environment and
move any obsolete `${XDG_CONFIG_HOME:-~/.config}/knowledge-atlas/mcp.json` to a
backup location. Entry points that see either old selector variable
(`KNOWLEDGE_ATLAS_DEFAULT_PROJECT` or `KNOWLEDGE_ATLAS_DOCKER_PROJECT`) or the
obsolete file reject the launch. Atlas leaves the file in place.
Stop a Docker deployment with `docker stop EXACT_CONTAINER_NAME`.

For a native service, set `KNOWLEDGE_ATLAS_CONFIG` to an external service JSON
file. Both hosts use the same service configuration:

```json
{
  "service_root": "/absolute/path/to/service",
  "runtime": {"runtime_root": "/absolute/path/to/prepared-runtime"},
  "configuration": {
    "graph": {"url": "http://127.0.0.1:6363"}
  }
}
```

Add a `configuration.profiles` object containing all six stage profiles (the
contents of the profiles JSON). Each stage must name its provider. If you
omit the object, Atlas uses the six shipped Codex profiles. See the provider
guide for stage settings and native CLI authentication. [Provider configuration](providers.md) The graph/embedding runtime must be prepared and running;
service startup alone does not qualify those components. The host used to call a
tool does not override the configured processing provider.

```sh
knowledge-atlas serve --config /absolute/path/to/service.json
knowledge-atlas status --config /absolute/path/to/service.json
knowledge-atlas stop --config /absolute/path/to/service.json
```

A native domain call starts or attaches to the service on demand. `mcp` reserves
stdout for MCP. `serve` runs in
the foreground. Without a configuration file, metadata-only service data defaults
to `${XDG_DATA_HOME:-~/.local/share}/knowledge-atlas/service`; native processing
still requires explicit prepared runtime configuration. Shutdown returns busy
while accepted work is active. Replacing the installed plugin leaves external
atlas/runtime data intact. Stop an incompatible running service before launching
a new version; automatic downgrade or data rewrite is not performed.

## Deployment choices considered

| Component | Native first-version use | Container option and limitation |
| --- | --- | --- |
| TerminusDB + VectorLink/Lance | Pinned source build with retained PoC9 patches | Upstream TerminusDB images exist, but a qualified image must include the exact three adaptations; an upstream tag alone is insufficient. |
| Wiki-LLM | Local SQLite/MCP package | Hosted Wiki containers are a different deployment from this local tool path. |
| Utopia | Unchanged extraction library via the retained Rust wrapper | A full Utopia service adds unrelated server dependencies; it does not replace the selected wrapper. |
| Gemma | Exact Q4 model, tokenizer and qualified runtime | The PoC used a separate embedding container; the optional Atlas Docker image packages this same runtime. |
| Redis / PostgreSQL | Not needed by the selected atlas backend | Their availability as images does not establish a need for them here. |

Native deployment remains available on macOS/Linux alongside the optional
Docker deployment. ACP is deferred; neither ACP nor Docker is required for native
operation with the two supported provider paths.
This decision does not claim native installation is faster or cheaper. Actual
platform/host qualification is recorded separately in [verification](verification.md).

## Starting the prepared components

Use the prepared runtime Python to run `runtime/gemma-service.py` with
`EMBED_MODEL_PATH` pointing to its `model/` directory and `EMBED_PORT` selecting
a free local port. This is the CPU embedding service used by PoC9, with pinned
assets and no Redis dependency. It binds only to loopback and never downloads
weights during startup. Then run `runtime/embedding-provider.py` with `--profile`,
`--cache` (`vectors/` under the configured `service_root`, shared by its projects), `--endpoint` (Gemma URL), and
`--port`. Set `PYTHONPATH` to the installed package's `src/` when using these
source-tree scripts. A fresh native setup can start the bridge first. The service
recognizes its current, integrity-checked cache on startup. Reattach the same
service root to retain project UUIDs, graph bindings and verified vectors.
An unsupported service or cache format stops startup and leaves its contents intact.

Run `runtime/native-runtime.py` with `--runtime`, a separate `--state`, `--profile`,
`--tokenizer`, the bridge URL as `--provider`, and a free `--port`; use `init` once,
then `serve`. Initialization writes a private random administrator key to
`<state>/admin.key`. Set `KNOWLEDGE_ATLAS_GRAPH_KEY_FILE` to its absolute path in
the environment that starts the Atlas service and any direct graph clients.
Back up the key with the graph's `atlas-format.json`, database and VectorLink
state. A matching release requires that complete set on restart. A keyless or
unmarked database stops startup with its original files intact. Rerun `init`
after an interrupted fresh initialization to complete current owned staging.
Wait for HTTP 200 from `/api/v1/health` before publication or indexing.
`/api/info` can respond while the embedded engine is still initializing.
All projects in one service share its bridge cache and have distinct graph
databases. Use separate service and graph storage for independent deployments.

## Linux qualification in Docker

The build-tool environment in `tests/integration/linux/Dockerfile` is an
Ubuntu 24.04 ARM64 qualification harness, not a released Atlas service image.
It retains the existing native runtime preparation and component pins. On an
ARM64 Mac, use native `linux/arm64` containers on the existing Docker backend,
as in the retained PoCs. No Rosetta or backend reconfiguration is needed.

From the package directory:

```sh
docker build --platform linux/arm64 -t knowledge-atlas-linux-qualification:local tests/integration/linux
docker run -d --init --platform linux/arm64 --name knowledge-atlas-linux-qualification --mount type=volume,src=knowledge-atlas-linux-work,dst=/work knowledge-atlas-linux-qualification:local sleep infinity
docker exec knowledge-atlas-linux-qualification mkdir -p /work/repo /work/evidence
git -C .. archive HEAD | docker exec -i knowledge-atlas-linux-qualification tar -x -C /work/repo
docker exec -w /work/repo/knowledge-atlas knowledge-atlas-linux-qualification uv sync --locked
docker exec -w /work/repo/knowledge-atlas knowledge-atlas-linux-qualification uv run pytest -q
```

Use an unused container/volume name for a separate attempt. The archive command
copies committed source only; it excludes host environments and credentials.
Run the test and native build serially to keep qualification resource use bounded. Create
`/work/runtime-config.json` containing `{"runtime_root":"/work/runtime"}`, then
run the existing `knowledge-atlas runtime prepare --config /work/runtime-config.json`
through `uv run` in the same package directory. Runtime logs and assets remain
in the Docker volume. A failed preparation requires another unused runtime
directory, as on the host.

Provider qualification additionally needs actual Linux Codex and Claude binaries
and their native sign-in flows. Installation is not authentication or lifecycle
qualification. The inspected Claude 2.1.319 npm package requires Node >=22 and
launches a bundled native Bun executable; Node alone does not replace that
runtime. Both pinned CLIs start in the native ARM64 container; see
[verification.md](verification.md) for the exact evidence and qualification limits.
Do not put credentials in this Dockerfile or copy them into image layers.

The uv installation follows its [official Docker guide](https://docs.astral.sh/uv/guides/integration/docker/).
For native host sign-in, use the [Codex headless authentication instructions](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)
and [Claude authentication instructions](https://code.claude.com/docs/en/authentication).

## Recommended Docker installation

The versioned pull, explicit local build, pinned model download and vendor login commands are described in [the Docker guide](../docker/README.md). This opt-in path uses persistent volumes and stdio MCP attachment without publishing ports. Native launch remains available; consult the verification report for the actual deployment evidence and remaining prerequisites.
