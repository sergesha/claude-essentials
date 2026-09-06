#!/bin/bash
# smoke.sh — local regression test for the tech-radar container stack.
# Proves the chown trap is dead and the session hook is idempotent.
# Requires a docker CLI with compose (macOS: Docker Desktop — its default
# file sharing covers /private/var/folders where mktemp lands; a podman
# machine does not). Cleans up after itself.
set -euo pipefail
cd "$(dirname "$0")/.."
export CLAUDE_PLUGIN_ROOT="$PWD"
CLAUDE_PLUGIN_DATA="$(mktemp -d)"
export CLAUDE_PLUGIN_DATA
# Container sections run the compose strategy explicitly: it is the only one
# testable in an isolated way (the quadlet path installs into the user's real
# ~/.config/containers/systemd/ — never touch that from a smoke test).
export TECH_RADAR_STACK=compose

cleanup() { docker rm -f tech-radar-searxng tech-radar-cache >/dev/null 2>&1 || true; }
trap cleanup EXIT

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

echo "== 0. quadlet templates render and pass the generator dry-run"
qdir="$(mktemp -d)"
for u in quadlet/*.container; do
  sed "s|@DATA_DIR@|$CLAUDE_PLUGIN_DATA|g" "$u" >"$qdir/$(basename "$u")"
done
grep -rq '@DATA_DIR@' "$qdir" && fail "unrendered @DATA_DIR@ left in a unit"
quadlet_bin=""
for q in /usr/libexec/podman/quadlet /usr/lib/podman/quadlet; do
  [ -x "$q" ] && quadlet_bin="$q" && break
done
if [ -n "$quadlet_bin" ]; then
  QUADLET_UNIT_DIRS="$qdir" "$quadlet_bin" -dryrun -user >/dev/null \
    || fail "quadlet -dryrun rejected the rendered units"
else
  echo "   (no quadlet binary on this host — render check only)"
fi
rm -rf "$qdir"

echo "== 1. first hook run creates the stack"
sh hooks/ensure-stack.sh || fail "hook exited non-zero (see $CLAUDE_PLUGIN_DATA/hook.log)"
id_s1=$(docker inspect tech-radar-searxng --format '{{.Id}}') || fail "searxng not created"
id_c1=$(docker inspect tech-radar-cache --format '{{.Id}}') || fail "cache not created"

echo "== 2. searxng answers JSON on 8888"
curl -fsS --retry 15 --retry-delay 3 --retry-all-errors \
  'http://127.0.0.1:8888/search?q=test&format=json' | head -c 200 | grep -q '{' \
  || fail "searxng did not answer JSON"

echo "== 3. the chown trap is dead (provider-independent)"
docker inspect tech-radar-searxng \
  --format '{{range .Mounts}}{{if eq .Destination "/etc/searxng"}}{{.RW}}{{end}}{{end}}' \
  | grep -qx false || fail "/etc/searxng mount is not read-only"
if docker exec tech-radar-searxng touch /etc/searxng/.smoke-w 2>/dev/null; then
  fail "container could WRITE into /etc/searxng (must be ro)"
fi
if [ "$(uname)" = Linux ]; then
  owner=$(stat -c %u "$CLAUDE_PLUGIN_DATA/searxng/settings.yml")
  [ "$owner" = "$(id -u)" ] || fail "settings.yml owner drifted to $owner (chown trap!)"
fi

echo "== 4. second hook run is idempotent (no recreation)"
sh hooks/ensure-stack.sh || fail "second hook run exited non-zero"
[ "$(docker inspect tech-radar-searxng --format '{{.Id}}')" = "$id_s1" ] || fail "searxng recreated"
[ "$(docker inspect tech-radar-cache --format '{{.Id}}')" = "$id_c1" ] || fail "cache recreated"

echo "SMOKE OK (hook log: $CLAUDE_PLUGIN_DATA/hook.log)"
