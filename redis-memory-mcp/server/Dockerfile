FROM python:3.12-slim AS base
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
CMD ["redis-memory-mcp"]

FROM base AS embeddings
RUN pip install --no-cache-dir '.[embeddings]'
ENV EMBED_HOST=0.0.0.0 EMBED_PORT=80 HF_HOME=/data
CMD ["redis-memory-embeddings"]

FROM base AS mcp
CMD ["redis-memory-mcp"]
