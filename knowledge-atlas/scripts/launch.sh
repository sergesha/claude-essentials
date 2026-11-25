#!/bin/sh
set -eu
if [ "$#" -eq 0 ]; then set -- mcp; fi
case "$1" in
  mcp|view|serve|status|stop)
    atlas_stale=""
    if [ "${KNOWLEDGE_ATLAS_DEFAULT_PROJECT+x}" ]; then
      atlas_stale=" KNOWLEDGE_ATLAS_DEFAULT_PROJECT"
    fi
    if [ "${KNOWLEDGE_ATLAS_DOCKER_PROJECT+x}" ]; then
      atlas_stale="$atlas_stale KNOWLEDGE_ATLAS_DOCKER_PROJECT"
    fi
    atlas_settings="${XDG_CONFIG_HOME:-$HOME/.config}/knowledge-atlas/mcp.json"
    if [ -e "$atlas_settings" ] || [ -L "$atlas_settings" ]; then
      atlas_stale="$atlas_stale $atlas_settings"
    fi
    if [ -n "$atlas_stale" ]; then
      printf '%s\n' "Remove the obsolete deployment settings:$atlas_stale." \
        'For Docker MCP, call atlas.connect with payload.container_name.' \
        'To stop a Docker service, run docker stop with the exact container name.' >&2
      exit 1
    fi
    ;;
esac
atlas_plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$atlas_plugin_root/src${PYTHONPATH:+:$PYTHONPATH}"
atlas_cache_root=${XDG_CACHE_HOME:-$HOME/.cache}/knowledge-atlas
atlas_plugin_version="0.3.4" # x-release-please-version
export UV_PROJECT_ENVIRONMENT="$atlas_cache_root/python-$atlas_plugin_version"
atlas_python="$UV_PROJECT_ENVIRONMENT/bin/python"
prepare_launcher() {
  uv sync --project "$atlas_plugin_root" --locked --no-dev --python 3.12 >&2
  test -x "$atlas_python"
  touch "$UV_PROJECT_ENVIRONMENT/.launcher-ready"
}
if [ "${1:-}" = prepare-launcher ]; then
  prepare_launcher
  exit 0
fi
if [ -n "${KNOWLEDGE_ATLAS_PYTHON:-}" ]; then
  exec "$KNOWLEDGE_ATLAS_PYTHON" -m knowledge_atlas.cli "$@"
fi
if [ -x "$atlas_python" ] && [ -f "$UV_PROJECT_ENVIRONMENT/.launcher-ready" ]; then
  exec "$atlas_python" -m knowledge_atlas.cli "$@"
fi
if [ -x "$atlas_plugin_root/.venv/bin/python" ]; then
  exec "$atlas_plugin_root/.venv/bin/python" -m knowledge_atlas.cli "$@"
fi
if command -v knowledge-atlas >/dev/null 2>&1; then
  exec knowledge-atlas "$@"
fi
if [ "$1" = mcp ]; then
  if command -v uv >/dev/null 2>&1; then
    prepare_launcher
    exec "$atlas_python" -m knowledge_atlas.cli "$@"
  fi
  echo "Knowledge Atlas needs uv on PATH for its first MCP launch." >&2
  exit 1
fi
if [ -x "$atlas_python" ] && [ -x "$UV_PROJECT_ENVIRONMENT/bin/knowledge-atlas" ]; then
  exec "$atlas_python" -m knowledge_atlas.cli "$@"
fi
echo "Knowledge Atlas dependencies are missing. Run: sh \"$atlas_plugin_root/scripts/launch.sh\" prepare-launcher" >&2
exit 1
