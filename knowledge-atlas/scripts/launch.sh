#!/bin/sh
set -eu
if [ "${1:-}" = stop ] && [ -n "${KNOWLEDGE_ATLAS_DOCKER_PROJECT:-}" ]; then
  printf '%s\n' \
    'KNOWLEDGE_ATLAS_DOCKER_PROJECT no longer selects a deployment.' \
    'For Docker, run docker stop with the exact container name.' \
    'For native, unset KNOWLEDGE_ATLAS_DOCKER_PROJECT and run this launcher with stop --config PATH.' >&2
  exit 1
fi
atlas_plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$atlas_plugin_root/src${PYTHONPATH:+:$PYTHONPATH}"
if [ "$#" -eq 0 ]; then set -- mcp; fi
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
