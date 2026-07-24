ARG PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG TORCH_FIND_LINKS=https://download.pytorch.org/whl/cpu/torch/
ARG TORCH_VERSION=2.13.0+cpu
ARG PIP_TRUSTED_HOST=

FROM ${PYTHON_BASE_IMAGE} AS wheel-builder

ARG PIP_INDEX_URL
ARG TORCH_FIND_LINKS
ARG TORCH_VERSION
ARG PIP_TRUSTED_HOST
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_NO_CACHE_DIR=1
WORKDIR /build

COPY pyproject.toml README.md LICENSE NOTICE CHANGELOG.md ./
COPY apps/api/src ./apps/api/src
COPY openapi ./openapi

RUN python -m pip wheel --wheel-dir /wheels \
    --find-links "${TORCH_FIND_LINKS}" \
    ${PIP_TRUSTED_HOST:+--trusted-host "${PIP_TRUSTED_HOST}"} \
    "torch==${TORCH_VERSION}" \
    '.[all-backends]'


FROM ${PYTHON_BASE_IMAGE} AS runtime

ENV AUTOML_HOST=0.0.0.0 \
    AUTOML_LOG_LEVEL=info \
    AUTOML_PORT=8000 \
    AUTOML_STATE_DIR=/var/lib/automl \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 automl \
    && useradd --uid 10001 --gid automl --no-create-home --shell /usr/sbin/nologin automl \
    && install -d --owner automl --group automl --mode 0700 /var/lib/automl

COPY --from=wheel-builder /wheels /wheels
RUN python -m pip install --no-index --find-links /wheels 'managed-automl-skeleton[all-backends]' \
    && rm -rf /wheels

USER 10001:10001
WORKDIR /var/lib/automl
EXPOSE 8000
VOLUME ["/var/lib/automl"]
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.environ.get('AUTOML_PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/readyz', timeout=3).read()" || exit 1

ENTRYPOINT ["automl-api"]


FROM runtime AS production

ENV AUTOML_DEPLOYMENT_PROFILE=production

RUN python -m automl_api.production


FROM runtime AS default

ENV AUTOML_DEPLOYMENT_PROFILE=partner-preview
