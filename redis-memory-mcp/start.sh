#!/bin/bash
# redis-memory-mcp — self-installing start script
# All setup output goes to stderr; only MCP server uses stdout (JSON-RPC)
set -e

REPO="sergesha/claude-essentials"
# redis-memory-mcp is one of several independently-versioned packages in $REPO
# (a monorepo, since v0.5.0) -- its tags are prefixed "redis-memory-mcp-v...",
# never bare "v...", so release lookups must filter by that prefix rather
# than take the repo's single "latest release" (ambiguous with other packages).
TAG_PREFIX="redis-memory-mcp-v"
REF="${REDIS_MEMORY_MCP_REF:-}"
WORK_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/redis-memory-mcp"
REF_FILE="$WORK_DIR/.installed-ref"
mkdir -p "$WORK_DIR"

log() { echo "🧠 redis-memory-mcp: $*" >&2; }

latest_release() {
  # Latest published release tag matching $TAG_PREFIX; empty on any failure
  # (network, rate limit, no matching release). Bounded timeouts so an
  # offline/slow host fails fast into the fallback chain instead of hanging.
  # Releases list is newest-first, so the first match is the latest.
  curl -fsSL --connect-timeout 5 --max-time 10 "https://api.github.com/repos/$REPO/releases" 2>/dev/null \
    | grep -m1 "\"tag_name\": *\"$TAG_PREFIX" \
    | sed -E 's/.*"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/'
}

# Resolve which ref to install, in order of preference (no version is ever hardcoded here):
#   1. explicit REDIS_MEMORY_MCP_REF (e.g. redis-memory-mcp-v0.5.0, or "main" to track dev)
#   2. latest published release tag matching $TAG_PREFIX (GitHub API)
#   3. the ref already installed here — a transient API failure must not change the version
#   4. "main" as a last resort on a cold, offline first run
if [ -z "$REF" ]; then
  REF="$(latest_release || true)"
  if [ -z "$REF" ] && [ -f "$REF_FILE" ]; then
    REF="$(cat "$REF_FILE")"
    log "Release lookup failed; reusing installed ref."
  fi
  if [ -z "$REF" ]; then
    REF="main"
    log "No release found and nothing installed yet; falling back to 'main'."
  fi
fi
log "Using ref: $REF"

# MODE selects whether this invocation owns its own Redis+TEI backend
# (dedicated, default — current behavior) or connects to one already running
# elsewhere (shared — e.g. one backend started once by a "fleet" user, reused
# by every other agent/user on the same host via REDIS_URL/EMBED_URL pointed
# at it). shared refuses to guess a backend: REDIS_URL and EMBED_URL must
# both be set explicitly, or this exits rather than silently start a second,
# unshared local instance.
MODE="${REDIS_MEMORY_MCP_MODE:-dedicated}"
case "$MODE" in
  dedicated|shared) ;;
  *)
    log "Invalid REDIS_MEMORY_MCP_MODE '$MODE' (expected 'dedicated' or 'shared')."
    exit 1
    ;;
esac
if [ "$MODE" = "shared" ] && { [ -z "${REDIS_URL:-}" ] || [ -z "${EMBED_URL:-}" ]; }; then
  log "REDIS_MEMORY_MCP_MODE=shared requires REDIS_URL and EMBED_URL to point at the shared backend."
  exit 1
fi

RAW_URL="https://raw.githubusercontent.com/$REPO/$REF/redis-memory-mcp"
# Docker tags must match [A-Za-z0-9_][A-Za-z0-9_.-]{0,127}: allowed chars only,
# a leading alnum/underscore, and <=128 chars. A ref may contain '/' (branch
# names), other punctuation, or a leading '.'/'-', so sanitize for the tag while
# RAW_URL keeps the real ref.
IMAGE_TAG="$(printf '%s' "$REF" | tr -c 'A-Za-z0-9_.-' '-')"
case "$IMAGE_TAG" in [!A-Za-z0-9_]*) IMAGE_TAG="ref-$IMAGE_TAG" ;; esac
IMAGE_TAG="$(printf '%s' "$IMAGE_TAG" | cut -c1-128)"
IMAGE="redis-memory-mcp:$IMAGE_TAG"
COMPOSE_FILE="$WORK_DIR/docker-compose.yaml"
SERVER_DIR="$WORK_DIR/server"

# ── 1. (Re)download pinned sources when the installed ref changes ─────────────
INSTALLED_REF="$(cat "$REF_FILE" 2>/dev/null || true)"
if [ "$INSTALLED_REF" != "$REF" ] || [ ! -f "$COMPOSE_FILE" ] || [ ! -d "$SERVER_DIR" ]; then
  log "Downloading sources for $REF..."
  curl -fsSL --connect-timeout 10 --max-time 60 "$RAW_URL/docker-compose.yaml" -o "$COMPOSE_FILE"
  mkdir -p "$SERVER_DIR"
  for f in memory_mcp.py Dockerfile pyproject.toml; do
    curl -fsSL --connect-timeout 10 --max-time 60 "$RAW_URL/server/$f" -o "$SERVER_DIR/$f"
  done
  echo "$REF" > "$REF_FILE"
fi

# ── 2-3. Start Redis + TEI and wait for Redis — dedicated mode only ───────────
# In shared mode, the backend is owned and started elsewhere; connecting to
# someone else's REDIS_URL/EMBED_URL is all this invocation should do.
if [ "$MODE" = "dedicated" ]; then
  log "Starting infrastructure..."
  docker compose -f "$COMPOSE_FILE" up -d redis embeddings redis-init >/dev/null 2>&1

  log "Waiting for Redis..."
  until docker exec redis-stack redis-cli ping >/dev/null 2>&1; do sleep 1; done
else
  log "MODE=shared: skipping local infra bring-up, connecting to REDIS_URL/EMBED_URL from environment."
fi

# ── 4. Build the version-tagged MCP server image if not present ───────────────
# Tagging the image per ref means switching REF rebuilds instead of reusing stale layers.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  log "Building $IMAGE..."
  docker build -t "$IMAGE" "$SERVER_DIR" >&2
fi

log "Ready ($REF)."

# ── 5. Launch MCP server — only this writes to stdout ─────────────────────────
# --add-host maps host.docker.internal to the host gateway. On Docker Desktop
# (macOS/Windows) it already resolves; on Linux Docker Engine it does not unless
# mapped explicitly, so the server couldn't reach Redis/TEI on the host there.
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

exec docker run --rm -i \
  --add-host=host.docker.internal:host-gateway \
  "${NETWORK_ARGS[@]}" \
  "${SOCKET_MOUNT_ARGS[@]}" \
  -e "REDIS_URL=${REDIS_URL:-redis://host.docker.internal:6379/0}" \
  -e "EMBED_URL=${EMBED_URL:-http://host.docker.internal:8081}" \
  -e "EMBED_SOCKET=${EMBED_SOCKET:-}" \
  -e "INDEX_NAME=${INDEX_NAME:-idx:memories}" \
  -e "NAMESPACE=${NAMESPACE:-}" \
  "$IMAGE"
