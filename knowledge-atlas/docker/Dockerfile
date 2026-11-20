FROM node:24.22.0-bookworm-slim@sha256:91acba91b842bec0bf7ea6f5e09c644c7ccad72c4996ed9616d861d1c7cd60d4 AS node
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM ubuntu:24.04@sha256:2198e4c65076e9cb0b3d695219df667c0e3b0072da64ca3c2648aca17dcecaf5
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git build-essential m4 cmake ninja-build pkg-config protobuf-compiler libprotobuf-dev libssl-dev libclang-dev libgmp-dev libreadline-dev zlib1g-dev libarchive-dev libpcre2-dev libyaml-dev libedit-dev libunwind-dev libicu-dev uuid-dev python3 unzip xz-utils && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.99.1 && rm /tmp/rustup-init.sh
ENV PATH="/opt/cargo/bin:${PATH}" UV_CACHE_DIR=/tmp/atlas-uv-cache UV_PROJECT_ENVIRONMENT=/work/venv
RUN uv python install 3.12.14
WORKDIR /package

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && npm install -g @openai/codex@0.167.0 @anthropic-ai/claude-code@2.1.319
WORKDIR /opt/knowledge-atlas
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY runtime ./runtime
COPY native ./native
COPY scripts ./scripts
COPY docker ./docker
RUN UV_PROJECT_ENVIRONMENT=/opt/knowledge-atlas/.venv uv sync --locked --no-dev
RUN install -d -m 0700 -o ubuntu -g ubuntu /auth /runtime /data
ENV HOME=/auth UV_CACHE_DIR=/tmp/atlas-user-uv-cache UV_PROJECT_ENVIRONMENT=/opt/knowledge-atlas/.venv PYTHONPATH=/opt/knowledge-atlas/src KNOWLEDGE_ATLAS_CONFIG=/data/service-config.json
USER ubuntu
RUN test -w /auth && test -w /data && test -w /runtime && rustc --version && cargo --version && /opt/knowledge-atlas/.venv/bin/python -c "import knowledge_atlas" && uv python find --no-python-downloads 3.12.14
ENTRYPOINT ["/opt/knowledge-atlas/.venv/bin/python", "/opt/knowledge-atlas/docker/entrypoint.py"]
CMD ["start"]
