#!/bin/sh
set -eu
atlas_plugin_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
: "${KNOWLEDGE_ATLAS_DOCKER_PROJECT:?Set the existing Compose project name}"
if [ "$#" -eq 0 ]; then set -- mcp; fi
exec docker compose -p "$KNOWLEDGE_ATLAS_DOCKER_PROJECT" -f "$atlas_plugin_root/docker/compose.yaml" exec -T atlas /opt/knowledge-atlas/.venv/bin/python -m knowledge_atlas.cli "$@"
