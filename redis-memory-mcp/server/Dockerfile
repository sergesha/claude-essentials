FROM scratch AS pip-wheel
ADD --checksum=sha256:f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13 https://files.pythonhosted.org/packages/07/25/ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/pip-26.3-py3-none-any.whl /pip-26.3-py3-none-any.whl

FROM python:3.12-slim AS base
RUN --mount=from=pip-wheel,source=/pip-26.3-py3-none-any.whl,target=/tmp/pip-26.3-py3-none-any.whl <<'SH'
set -eu
apt-get update
apt-get install -y --no-install-recommends --only-upgrade libzstd1
rm -rf /var/lib/apt/lists/*
python -m pip install --no-deps --no-index --upgrade /tmp/pip-26.3-py3-none-any.whl
python - <<'PY'
import ensurepip
import pathlib
import shutil
import sys

wheel = pathlib.Path("/tmp/pip-26.3-py3-none-any.whl")
module = pathlib.Path(ensurepip.__file__)
bundled = module.parent / "_bundled"
old, = bundled.glob("pip-*.whl")
source = module.read_text()
declaration = f'_PIP_VERSION = "{ensurepip.version()}"'
if source.count(declaration) != 1 or old.name != f"pip-{ensurepip.version()}-py3-none-any.whl":
    sys.exit("unexpected ensurepip source or bootstrap wheel")
shutil.copyfile(wheel, bundled / wheel.name)
module.write_text(source.replace(declaration, '_PIP_VERSION = "26.3"'))
if old.name != wheel.name:
    old.unlink()
PY
test "$(python -m ensurepip --version)" = "pip 26.3"
SH
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
