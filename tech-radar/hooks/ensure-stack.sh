#!/bin/sh
# Idempotent bring-up of the tech-radar stack (called from SessionStart).
# Invariant: $CLAUDE_PLUGIN_ROOT is a read-only source; everything mutable
# lives ONLY in $CLAUDE_PLUGIN_DATA (survives claude plugin install/update).
#
# Strategies chosen by TECH_RADAR_STACK (auto|quadlet|compose|external,
# default auto). external leaves a premanaged backend completely untouched.
#   quadlet — podman + a reachable systemd user manager. Units rendered into
#             ~/.config/containers/systemd/, so systemd supervises the stack:
#             restart on failure, start on boot (with linger), and every start
#             re-creates the container networking — no compose-era stale
#             port-forwarder to go dead behind a formally-"Up" container.
#   compose — any docker CLI with compose (macOS Docker Desktop, Docker CE).
set -u

# A premanaged backend must remain a true no-op: do not require host-provided
# plugin paths, create logs/config, probe tools, or inspect fixed-name containers.
[ "${TECH_RADAR_STACK:-auto}" = external ] && exit 0

verify_searxng_json() {
  runtime=$1
  attempt=1
  while [ "$attempt" -le 15 ]; do
    "$runtime" exec -i tech-radar-searxng python - <<'PY'
import json
import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen(
        "http://127.0.0.1:8080/search?format=json", timeout=2
    ).close()
except urllib.error.HTTPError as error:
    if error.code == 403:
        raise SystemExit(3)
    if error.code == 400:
        try:
            payload = json.load(error)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise SystemExit(1)
        if payload.get("error") == "No query":
            raise SystemExit(0)
except OSError:
    pass
raise SystemExit(1)
PY
    result=$?
    if [ "$result" -eq 0 ]; then
      echo "SearXNG JSON API ready"
      return 0
    fi
    if [ "$result" -eq 3 ]; then
      echo "SearXNG JSON API disabled (HTTP 403)" >&2
      return 1
    fi
    [ "$attempt" -eq 15 ] || sleep 1
    attempt=$((attempt + 1))
  done
  echo "SearXNG JSON API did not become ready after 15 attempts" >&2
  return 1
}

# Claude and Codex keep plugin data under different host-owned directories.
# This explicit cross-host override lets both clients reuse one existing mount.
if [ -n "${TECH_RADAR_DATA_DIR:-}" ]; then
  CLAUDE_PLUGIN_DATA="$TECH_RADAR_DATA_DIR"
  export CLAUDE_PLUGIN_DATA
fi
: "${CLAUDE_PLUGIN_ROOT:?}" "${CLAUDE_PLUGIN_DATA:?}"
mkdir -p "$CLAUDE_PLUGIN_DATA/searxng"
# crude rotation: an unbounded append-log grows forever
[ "$(wc -c <"$CLAUDE_PLUGIN_DATA/hook.log" 2>/dev/null || echo 0)" -gt 65536 ] \
  && mv -f "$CLAUDE_PLUGIN_DATA/hook.log" "$CLAUDE_PLUGIN_DATA/hook.log.1"
exec >>"$CLAUDE_PLUGIN_DATA/hook.log" 2>&1
echo "--- $(date -u +%FT%TZ) session-start, plugin root: $CLAUDE_PLUGIN_ROOT"

# 1. Config cache -> data (the plugin version is the source of truth).
config_changed=no
for config in settings.yml limiter.toml; do
  source_config="$CLAUDE_PLUGIN_ROOT/searxng/$config"
  data_config="$CLAUDE_PLUGIN_DATA/searxng/$config"
  if ! cmp -s "$source_config" "$data_config" 2>/dev/null; then
    cp -f "$source_config" "$data_config" || exit 1
    config_changed=yes
  fi
done

# 2. Pick the strategy. systemctl --user needs XDG_RUNTIME_DIR, which
#    non-login invocations (sudo -u, nohup'd hooks) may lack.
mode="${TECH_RADAR_STACK:-auto}"
if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
  XDG_RUNTIME_DIR="/run/user/$(id -u)"; export XDG_RUNTIME_DIR
fi
if [ "$mode" = auto ]; then
  if command -v podman >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    mode=quadlet
  else
    mode=compose
  fi
fi
echo "stack strategy: $mode"

if [ "$mode" = quadlet ]; then
  # 3a. Render unit templates; rewrite only on change so an unchanged stack is
  #     never restarted by a mere session start.
  unitdir="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
  mkdir -p "$unitdir"
  changed=no
  for u in tech-radar-searxng.container tech-radar-cache.container; do
    tmp=$(mktemp)
    sed "s|@DATA_DIR@|$CLAUDE_PLUGIN_DATA|g" "$CLAUDE_PLUGIN_ROOT/quadlet/$u" >"$tmp"
    if cmp -s "$tmp" "$unitdir/$u" 2>/dev/null; then
      rm -f "$tmp"
    else
      mv -f "$tmp" "$unitdir/$u" && chmod 0644 "$unitdir/$u"
      changed=yes
    fi
  done

  # 4a. Compose-era migration: containers created by podman-compose hold the
  #     names (plus a pod and a network) the quadlet services need. A container
  #     carrying a compose project label is never quadlet-managed — remove
  #     exactly those, and the now-empty pod/network with them.
  for c in tech-radar-searxng tech-radar-cache; do
    proj=$(podman inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null) || proj=""
    if [ -n "$proj" ]; then
      echo "removing compose-era container $c (project=$proj)"
      podman rm -f "$c"
    fi
  done
  podman pod exists pod_tech-radar 2>/dev/null && podman pod rm -f pod_tech-radar
  podman network exists tech-radar_default 2>/dev/null && podman network rm tech-radar_default

  # 5a. daemon-reload regenerates services from the unit files; start is a
  #     no-op for running services, restart picks up changed units.
  systemctl --user daemon-reload
  if [ "$changed" = yes ]; then
    echo "unit files changed, restarting services"
    systemctl --user restart tech-radar-searxng.service tech-radar-cache.service || exit 1
  elif [ "$config_changed" = yes ]; then
    echo "SearXNG config changed, restarting SearXNG"
    systemctl --user restart tech-radar-searxng.service || exit 1
    systemctl --user start tech-radar-cache.service || exit 1
  else
    systemctl --user start tech-radar-searxng.service tech-radar-cache.service || exit 1
  fi
  runtime=podman
else
  # 3b. Migration/drift — recreate the stack when the running state does not
  #     match this compose. Drift is ANY of:
  #       - searxng exists and its /etc/searxng mount source != data dir
  #       - either container exists with compose project label != "tech-radar"
  #       - cache exists while searxng does not (half-dead stack; up -d would
  #         hit a container-name conflict with the foreign-project cache)
  #     Losing tech-radar-cache is harmless (search cache only; redis-memory
  #     state lives in a separate backend). Concurrent session starts racing
  #     through this branch self-heal (double rm/up converges) — accepted.
  want="$CLAUDE_PLUGIN_DATA/searxng"
  have=$(docker inspect tech-radar-searxng --format \
    '{{range .Mounts}}{{if eq .Destination "/etc/searxng"}}{{.Source}}{{end}}{{end}}' 2>/dev/null) || have=""
  proj_s=$(docker inspect tech-radar-searxng --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null) || proj_s=""
  proj_c=$(docker inspect tech-radar-cache   --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null) || proj_c=""
  if docker inspect tech-radar-cache >/dev/null 2>&1; then cache_exists=yes; else cache_exists=no; fi
  drift=no
  [ -n "$have" ] && [ "$have" != "$want" ] && drift=yes
  [ -n "$proj_s" ] && [ "$proj_s" != "tech-radar" ] && drift=yes
  [ -n "$proj_c" ] && [ "$proj_c" != "tech-radar" ] && drift=yes
  [ "$cache_exists" = yes ] && [ -z "$have" ] && drift=yes
  if [ "$drift" = yes ]; then
    echo "stack drift detected (mount=$have proj=$proj_s/$proj_c), recreating"
    docker rm -f tech-radar-searxng tech-radar-cache || true
  fi

  # 4b. up -d is idempotent; docker start is the provider-independent safety net:
  #     starts stopped containers, no-op for running ones (podman-compose 1.3.0's
  #     up -d is unreliable with pre-existing containers)
  docker compose -f "$CLAUDE_PLUGIN_ROOT/docker-compose.yaml" -p tech-radar up -d || true
  docker start tech-radar-searxng tech-radar-cache || exit 1
  if [ "$config_changed" = yes ]; then
    echo "SearXNG config changed, restarting SearXNG"
    docker restart tech-radar-searxng || exit 1
  fi
  runtime=docker
fi

# 5. Verify the configured JSON response without making an engine request.
verify_searxng_json "$runtime"
