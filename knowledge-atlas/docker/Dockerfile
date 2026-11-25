FROM node:24.22.0-bookworm-slim@sha256:91acba91b842bec0bf7ea6f5e09c644c7ccad72c4996ed9616d861d1c7cd60d4 AS node
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM ubuntu:24.04@sha256:2198e4c65076e9cb0b3d695219df667c0e3b0072da64ca3c2648aca17dcecaf5 AS system
ENV DEBIAN_FRONTEND=noninteractive UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git python3 \
    libarchive13t64 libedit2 libgmp10 libicu74 libpcre2-8-0 libreadline8t64 libssl3t64 \
    libunwind8 libuuid1 libyaml-0-2 zlib1g && rm -rf /var/lib/apt/lists/*

FROM system AS builder
RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential m4 cmake ninja-build \
    pkg-config protobuf-compiler libprotobuf-dev libssl-dev libclang-dev libgmp-dev libreadline-dev \
    zlib1g-dev libarchive-dev libpcre2-dev libyaml-dev libedit-dev libunwind-dev libicu-dev uuid-dev \
    unzip xz-utils && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH="/opt/cargo/bin:${PATH}"
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh && \
    sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.99.1 && rm /tmp/rustup-init.sh
RUN uv python install 3.12.14
WORKDIR /opt/knowledge-atlas
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY runtime ./runtime
COPY native ./native
COPY scripts ./scripts
COPY docker ./docker
RUN uv sync --locked --no-dev --python 3.12.14
ARG ATLAS_REVISION
ARG ATLAS_VERSION
RUN .venv/bin/python -c "from importlib.metadata import version; from knowledge_atlas import __version__; import os; assert os.environ['ATLAS_VERSION'] == __version__ == version('knowledge-atlas')" && \
    .venv/bin/python docker/build-seed.py && chmod -R go-w,a+rX /opt/atlas-seed /opt/atlas-distribution /opt/uv/python

FROM system
WORKDIR /opt/knowledge-atlas
COPY --from=builder /opt/uv/python /opt/uv/python
COPY --from=builder /opt/knowledge-atlas /opt/knowledge-atlas
COPY --from=builder /opt/atlas-seed /opt/atlas-seed
COPY --from=builder /opt/atlas-distribution /usr/share/doc/knowledge-atlas
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/LICENSE /usr/share/doc/nodejs/LICENSE
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    dpkg-query -W -f='${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\n' > /usr/share/doc/knowledge-atlas/debian-packages.tsv && \
    install -d -m 0700 -o ubuntu -g ubuntu /auth /runtime /data && \
    for repo in wiki terminusdb vectorlink; do git config --system --add safe.directory "/opt/atlas-seed/$repo"; done
ARG ATLAS_REVISION
ARG ATLAS_VERSION
LABEL org.opencontainers.image.source="https://github.com/sergesha/claude-essentials" \
      org.opencontainers.image.revision="$ATLAS_REVISION" \
      org.opencontainers.image.version="$ATLAS_VERSION"
ENV HOME=/auth PYTHONPATH=/opt/knowledge-atlas/src KNOWLEDGE_ATLAS_CONFIG=/data/service-config.json \
    PATH="/runtime/providers/codex/node_modules/.bin:/runtime/providers/claude/node_modules/.bin:${PATH}"
USER ubuntu
RUN .venv/bin/python -c "from pathlib import Path; from knowledge_atlas.runtime import _validate_runtime,RuntimeConfig; _validate_runtime(RuntimeConfig(Path('/opt/atlas-seed')), check_model=False)" && \
    /opt/atlas-seed/python/bin/python -c "import onnxruntime, tokenizers, numpy" && \
    /opt/atlas-seed/native-tools/swipl/bin/swipl --version && \
    echo '{"filename":"fixture.md","text":"Image build check"}' | /opt/atlas-seed/extract/target/debug/poc9-extract prepare >/dev/null && npm --version && test ! -e /opt/atlas-seed/model
ENTRYPOINT ["/opt/knowledge-atlas/.venv/bin/python", "/opt/knowledge-atlas/docker/entrypoint.py"]
CMD ["start"]
