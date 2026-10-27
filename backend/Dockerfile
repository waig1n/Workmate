FROM python:3.12-slim

WORKDIR /app

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_DEFAULT_TIMEOUT=120
COPY scripts/release/configure_package_sources.py /tmp/configure_package_sources.py
RUN python /tmp/configure_package_sources.py --pip-index-url "${PIP_INDEX_URL}" \
    && rm /tmp/configure_package_sources.py

COPY backend/requirements.lock ./requirements.lock
# Keep third-party dependencies cached across application/shared-package edits.
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install \
    --retries 8 \
    --timeout "${PIP_DEFAULT_TIMEOUT}" \
    --index-url "${PIP_INDEX_URL}" \
    --require-hashes -r requirements.lock
COPY packages/contracts /packages/contracts
COPY packages/product /packages/product
COPY backend/pyproject.toml backend/LICENSE backend/NOTICE ./
COPY backend/app ./app
COPY backend/scripts ./scripts
# Build isolation also downloads hatchling; reuse pip's cache outside the image.
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install --no-deps \
    --index-url "${PIP_INDEX_URL}" \
    /packages/contracts /packages/product .
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./

# The API startup command resolves Alembic independently of source cwd.
ENV WORKMATE_ALEMBIC_CONFIG=/app/alembic.ini

EXPOSE 8080

CMD ["workmate-start-product-api"]
