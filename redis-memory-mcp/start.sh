#!/bin/bash
# Release delivery uses prebuilt, security-qualified GHCR images. stdout is MCP only.
set -eo pipefail
REPO="sergesha/claude-essentials"
TAG_PREFIX="redis-memory-mcp-v"
REF="${REDIS_MEMORY_MCP_REF:-}"
WORK_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/redis-memory-mcp"
REF_FILE="$WORK_DIR/.installed-ref"
mkdir -p "$WORK_DIR"
log() { echo "🧠 redis-memory-mcp: $*" >&2; }
latest_release() {
  curl -fsSL --connect-timeout 5 --max-time 10 "https://api.github.com/repos/$REPO/releases" 2>/dev/null \
    | grep -m1 "\"tag_name\": *\"$TAG_PREFIX" \
    | sed -E 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/'
}
if [ -z "$REF" ]; then
  REF="$(latest_release || true)"
  if [ -z "$REF" ] && [ -f "$REF_FILE" ]; then
    REF="$(cat "$REF_FILE")"
    log "Release lookup failed; reusing installed ref."
  fi
fi
if [[ ! "$REF" =~ ^redis-memory-mcp-v([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  log "A published redis-memory-mcp-vX.Y.Z release is required. For development use docker-compose.build.yaml explicitly."
  exit 1
fi
# Preserve explicit historical pins: those releases predate registry delivery.
if (( 10#${BASH_REMATCH[1]} == 0 && 10#${BASH_REMATCH[2]} < 12 )); then
  log "Legacy $REF uses its original source-build launcher."
  rm -f "$WORK_DIR/.compose-ref"
  legacy="$(mktemp)"
  trap 'rm -f "$legacy"' EXIT
  curl -fsSL --connect-timeout 10 --max-time 60 "https://raw.githubusercontent.com/$REPO/$REF/redis-memory-mcp/start.sh" -o "$legacy"
  REDIS_MEMORY_MCP_REF="$REF" bash "$legacy"
  exit $?
fi
VERSION="${REF#$TAG_PREFIX}"
MODE="${REDIS_MEMORY_MCP_MODE:-dedicated}"
case "$MODE" in dedicated|shared) ;; *) log "Invalid REDIS_MEMORY_MCP_MODE: expected dedicated or shared."; exit 1;; esac
if [ "$MODE" = shared ] && { [ -z "${REDIS_URL:-}" ] || [ -z "${EMBED_URL:-}" ]; }; then
  log "Shared mode requires REDIS_URL and EMBED_URL."
  exit 1
fi
IMAGE="ghcr.io/sergesha/redis-memory-mcp:$VERSION"
export REDIS_MEMORY_MCP_IMAGE="$IMAGE"
export REDIS_MEMORY_EMBEDDINGS_IMAGE="ghcr.io/sergesha/redis-memory-embeddings:$VERSION"
# Version tags are immutable. A cached release works without contacting GHCR.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  log "Pulling $IMAGE..."
  docker pull "$IMAGE" >&2
fi
# Bind this invocation to the pulled registry digest, not a mutable local tag.
IMAGE="$(docker image inspect --format '{{index .RepoDigests 0}}' "$IMAGE")"
if [[ ! "$IMAGE" =~ ^ghcr.io/sergesha/redis-memory-mcp@sha256:[a-f0-9]{64}$ ]]; then
  log "The MCP image has no valid registry digest. Pull the selected release again."
  exit 1
fi
COMPOSE_FILE="$WORK_DIR/docker-compose.yaml"
if [ "$MODE" = dedicated ]; then
  INSTALLED_REF="$(cat "$WORK_DIR/.compose-ref" 2>/dev/null || true)"
  if [ "$INSTALLED_REF" != "$REF" ] || [ ! -f "$COMPOSE_FILE" ]; then
    temporary="$(mktemp "$WORK_DIR/compose.XXXXXX")"
    trap 'rm -f "$temporary"' EXIT
    curl -fsSL --connect-timeout 10 --max-time 60 "https://raw.githubusercontent.com/$REPO/$REF/redis-memory-mcp/docker-compose.yaml" -o "$temporary"
    mv "$temporary" "$COMPOSE_FILE"
    trap - EXIT
  fi
  compose=(docker compose -f "$COMPOSE_FILE")
  if [ -f "$WORK_DIR/docker-compose.local.yaml" ]; then
    compose+=(-f "$WORK_DIR/docker-compose.local.yaml")
  fi
  # The final override selects this release even if an old local override named a
  # locally built image. All other operator settings and relative mounts survive.
  release_file="$WORK_DIR/docker-compose.release.yaml"
  temporary="$(mktemp "$WORK_DIR/release.XXXXXX.yaml")"
  trap 'rm -f "$temporary"' EXIT
  printf 'services:\n  embeddings:\n    image: %s\n' "$REDIS_MEMORY_EMBEDDINGS_IMAGE" > "$temporary"
  mv "$temporary" "$release_file"
  trap - EXIT
  compose+=(-f "$release_file")
  "${compose[@]}" up -d --no-build --pull missing --wait --wait-timeout 180 redis embeddings >&2
else
  log "Shared mode: using the existing backend."
fi
# Dedicated configuration is refreshed independently of shared bridge activation.
printf '%s\n' "$REF" > "$REF_FILE"
if [ "$MODE" = dedicated ]; then printf '%s\n' "$REF" > "$WORK_DIR/.compose-ref"; fi
log "Ready ($REF)."

# ── 5. Launch MCP server — only this writes to stdout ─────────────────────────
# --add-host maps host.docker.internal to the host gateway. On Docker Desktop
# (macOS/Windows) it already resolves; on Linux Docker Engine it does not unless
# mapped explicitly, so the server couldn't reach Redis/Gemma on the host there.
# That gateway address only reaches host-published ports, though -- a backend
# bound to 127.0.0.1 (loopback-only, e.g. for host-level isolation) is not
# reachable through it from a different container's network namespace, no
# matter what URL/env points at it. REDIS_MEMORY_MCP_NETWORK sidesteps that:
# when set, this container joins that (pre-existing) network directly instead
# of relying on the host gateway, so it can reach a same-network peer by
# container name/IP even when that peer publishes no host port at all. Unset
# by default -- existing dedicated/shared setups using host.docker.internal
# are unaffected.
NETWORK_ARGS=()
if [ -n "${REDIS_MEMORY_MCP_NETWORK:-}" ]; then
  NETWORK_ARGS=(--network "$REDIS_MEMORY_MCP_NETWORK")
fi

# REDIS_MEMORY_MCP_SOCKET_DIR sidesteps the same host.docker.internal
# limitation for Unix sockets: when set, that host directory (expected to
# contain a Redis Unix socket, and optionally an EMBED_SOCKET file/socket
# proxying the embeddings endpoint -- e.g. via a socat sidecar) is bind-mounted
# into this container at the identical path, and the container joins the
# socket owner's group (--group-add keep-groups) so it can actually open a
# group-gated (mode 770) socket that belongs to a different host user than
# whichever user launched this container. REDIS_URL can then be a unix://
# path under that directory (redis-py's `from_url` supports this natively,
# no server code change needed); EMBED_SOCKET (below) is what lets the
# embeddings HTTP client use the same mechanism, since a bare http:// URL
# has no unix-socket equivalent.
SOCKET_MOUNT_ARGS=()
if [ -n "${REDIS_MEMORY_MCP_SOCKET_DIR:-}" ]; then
  SOCKET_MOUNT_ARGS=(-v "${REDIS_MEMORY_MCP_SOCKET_DIR}:${REDIS_MEMORY_MCP_SOCKET_DIR}" --group-add keep-groups)
fi

# Defaults are applied HERE, by export, so the run below can pass every
# variable by NAME. That is not a style choice: with ACLs (0.8.0) REDIS_URL
# carries the namespace password, and `-e NAME=value` writes that value into
# this process's argv. /proc/<pid>/cmdline is world-readable on a stock Linux
# host, so every other local user -- and anything that prints a process tree,
# `systemctl status` and `ps` included -- reads the password in clear text.
# `-e NAME` makes docker/podman copy the value out of this process's
# environment instead, and /proc/<pid>/environ is 0400, owner only.
# Behaviour is identical: an exported-but-empty variable is still passed as an
# empty string (verified against podman), which is what the old explicit
# `-e "EMBED_SOCKET="` did.
export REDIS_URL="${REDIS_URL:-redis://host.docker.internal:6379/0}"
export EMBED_URL="${EMBED_URL:-http://host.docker.internal:8082}"
export EMBED_SOCKET="${EMBED_SOCKET:-}"
export INDEX_NAME="${INDEX_NAME:-idx:memories}"
export NAMESPACE="${NAMESPACE:-}"
export CHUNK_MAX_TOKENS="${CHUNK_MAX_TOKENS:-256}"
export EMBED_DIMENSION="${EMBED_DIMENSION:-256}"

exec docker run --rm -i \
  --add-host=host.docker.internal:host-gateway \
  "${NETWORK_ARGS[@]}" \
  "${SOCKET_MOUNT_ARGS[@]}" \
  -e REDIS_URL \
  -e EMBED_URL \
  -e EMBED_SOCKET \
  -e INDEX_NAME \
  -e NAMESPACE \
  -e CHUNK_MAX_TOKENS \
  -e EMBED_DIMENSION \
  "$IMAGE"
