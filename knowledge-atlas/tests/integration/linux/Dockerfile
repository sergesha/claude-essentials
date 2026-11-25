FROM ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d AS uv
FROM ubuntu:24.04@sha256:2198e4c65076e9cb0b3d695219df667c0e3b0072da64ca3c2648aca17dcecaf5
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git build-essential m4 cmake ninja-build pkg-config protobuf-compiler libprotobuf-dev libssl-dev libclang-dev libgmp-dev libreadline-dev zlib1g-dev libarchive-dev libpcre2-dev libyaml-dev libedit-dev libunwind-dev libicu-dev uuid-dev python3 unzip xz-utils && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /usr/local/bin/
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain 1.99.1 && rm /tmp/rustup-init.sh
ENV PATH="/root/.cargo/bin:${PATH}" UV_CACHE_DIR=/cache/uv UV_PROJECT_ENVIRONMENT=/work/venv
RUN uv python install 3.12.16
WORKDIR /package
