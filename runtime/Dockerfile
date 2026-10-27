# Keep Bubblewrap on Debian 12: Debian 13's bubblewrap 0.12 uses mount APIs
# unavailable on the supported Ubuntu 20.04 / Linux 5.4 deployment host.
FROM python:3.12-slim-bookworm AS runtime-base

WORKDIR /app

# Configure build and in-sandbox defaults; Compose may override the Python index.
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_DEFAULT_TIMEOUT=120
COPY scripts/release/configure_package_sources.py /tmp/configure_package_sources.py
RUN python /tmp/configure_package_sources.py --pip-index-url "${PIP_INDEX_URL}" \
    && rm /tmp/configure_package_sources.py

# Agent commands run in Bubblewrap namespaces with a cleared environment.
# Certificates and CJK fonts remain available inside read-only /usr mounts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bubblewrap ca-certificates ripgrep fontconfig fonts-noto-cjk \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY runtime/requirements.lock ./requirements.lock
# Install third-party dependencies in a cacheable layer. The real source is
# copied afterwards so ordinary code edits do not download the full AI stack.
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install \
    --retries 8 \
    --timeout "${PIP_DEFAULT_TIMEOUT}" \
    --index-url "${PIP_INDEX_URL}" \
    --require-hashes -r requirements.lock
# Fail the build if a builtin Skill dependency or its Chinese font is unavailable.
RUN python -c "import duckdb, openpyxl, pptx, PIL, matplotlib, yaml; from matplotlib import font_manager; font_manager.findfont('Noto Sans CJK SC', fallback_to_default=False)"
COPY packages/contracts /packages/contracts
COPY packages/product /packages/product
COPY runtime/pyproject.toml runtime/LICENSE runtime/NOTICE ./
COPY runtime/workmate_runtime ./workmate_runtime
# Build isolation also downloads hatchling; reuse pip's cache outside the image.
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --no-deps \
    --index-url "${PIP_INDEX_URL}" \
    /packages/contracts /packages/product .

ENV WORKMATE_WORKSPACE_BASEDIR=/data/workspaces \
    WORKMATE_REME_BASEDIR=/data/reme

EXPOSE 8000

FROM runtime-base AS worker
CMD ["python", "-m", "workmate_runtime.roles.worker"]
