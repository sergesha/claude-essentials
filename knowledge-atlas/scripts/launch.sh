#!/bin/sh
set -eu
atlas_plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -n "${KNOWLEDGE_ATLAS_DOCKER_PROJECT:-}" ] && [ "${1:-mcp}" != view ]; then
  exec sh "$atlas_plugin_root/scripts/docker-launch.sh" "$@"
fi
export PYTHONPATH="$atlas_plugin_root/src${PYTHONPATH:+:$PYTHONPATH}"
if [ "$#" -eq 0 ]; then set -- mcp; fi
if [ -n "${KNOWLEDGE_ATLAS_PYTHON:-}" ]; then
  exec "$KNOWLEDGE_ATLAS_PYTHON" -m knowledge_atlas.cli "$@"
fi
atlas_cache_root=${XDG_CACHE_HOME:-$HOME/.cache}/knowledge-atlas
atlas_plugin_version="0.2.0" # x-release-please-version
export UV_PROJECT_ENVIRONMENT="$atlas_cache_root/python-$atlas_plugin_version"
exec uv run --project "$atlas_plugin_root" --locked --no-dev --python 3.12 python -m knowledge_atlas.cli "$@"
