#!/bin/sh
set -eu
atlas_plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case ${1:-mcp} in
  view|prepare-launcher) ;;
  *)
    if [ -n "${KNOWLEDGE_ATLAS_DOCKER_PROJECT:-}" ]; then
      exec sh "$atlas_plugin_root/scripts/docker-launch.sh" "$@"
    fi ;;
esac
export PYTHONPATH="$atlas_plugin_root/src${PYTHONPATH:+:$PYTHONPATH}"
if [ "$#" -eq 0 ]; then set -- mcp; fi
atlas_cache_root=${XDG_CACHE_HOME:-$HOME/.cache}/knowledge-atlas
atlas_plugin_version="0.3.2" # x-release-please-version
export UV_PROJECT_ENVIRONMENT="$atlas_cache_root/python-$atlas_plugin_version"
if [ "${1:-}" = prepare-launcher ]; then
  exec uv sync --project "$atlas_plugin_root" --locked --no-dev --python 3.12
fi
if [ -n "${KNOWLEDGE_ATLAS_PYTHON:-}" ]; then
  exec "$KNOWLEDGE_ATLAS_PYTHON" -m knowledge_atlas.cli "$@"
fi
for atlas_python in "$UV_PROJECT_ENVIRONMENT/bin/python" "$atlas_plugin_root/.venv/bin/python"; do
  if [ -x "$atlas_python" ]; then
    exec "$atlas_python" -m knowledge_atlas.cli "$@"
  fi
done
if command -v knowledge-atlas >/dev/null 2>&1; then
  exec knowledge-atlas "$@"
fi
echo "Knowledge Atlas dependencies are missing. Run: sh \"$atlas_plugin_root/scripts/launch.sh\" prepare-launcher" >&2
exit 1
