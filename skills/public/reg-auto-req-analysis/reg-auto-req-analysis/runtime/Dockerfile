ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

FROM ${PYTHON_IMAGE} AS wheel-builder

COPY runtime/requirements-attachments.lock /tmp/runtime/
RUN python -m pip download --disable-pip-version-check --no-cache-dir \
        --dest /opt/wheelhouse \
        --require-hashes --no-deps \
        --requirement /tmp/runtime/requirements-attachments.lock \
    && sha256sum /opt/wheelhouse/* > /opt/wheelhouse.sha256

FROM ${PYTHON_IMAGE}

ARG LIBREOFFICE_VERSION=4:7.4.7-1+deb12u13
ARG RUNTIME_IMAGE_ID=reg-auto-req-analysis-office:0.1.0

ENV AUTO_REQ_RUNTIME_IMAGE=${RUNTIME_IMAGE_ID} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-calc=${LIBREOFFICE_VERSION} \
        libreoffice-writer=${LIBREOFFICE_VERSION} \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/runtime-manifest
COPY --from=wheel-builder /opt/wheelhouse /opt/wheelhouse
COPY --from=wheel-builder /opt/wheelhouse.sha256 /opt/runtime-manifest/wheelhouse.sha256
COPY runtime/requirements-attachments.txt runtime/requirements-attachments.lock /opt/runtime-manifest/
RUN cd /opt \
    && sha256sum --check /opt/runtime-manifest/wheelhouse.sha256 \
    && python -m pip install --no-index --no-cache-dir \
        --find-links /opt/wheelhouse \
        --require-hashes --no-deps \
        --requirement /opt/runtime-manifest/requirements-attachments.lock \
    && python -m pip check \
    && python -m pip freeze --all > /opt/runtime-manifest/python-packages.txt \
    && dpkg-query -W libreoffice-core libreoffice-writer libreoffice-calc \
        > /opt/runtime-manifest/system-packages.txt \
    && rm -rf /opt/wheelhouse

COPY _lib/tfs/attachment_converter.py /opt/reg-auto-req-analysis/_lib/tfs/attachment_converter.py
COPY runtime/smoke_test.py /opt/reg-auto-req-analysis/runtime/smoke_test.py
RUN python /opt/reg-auto-req-analysis/_lib/tfs/attachment_converter.py --precheck \
    && python /opt/reg-auto-req-analysis/runtime/smoke_test.py

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin auto-req \
    && mkdir -p /work \
    && chown auto-req:auto-req /work

USER auto-req
WORKDIR /work
ENTRYPOINT ["python", "/opt/reg-auto-req-analysis/_lib/tfs/attachment_converter.py"]
