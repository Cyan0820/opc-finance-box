FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY . .
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    OPC_FINANCE_DATA_DIR=/var/lib/opc-finance \
    OPC_FINANCE_HOST=0.0.0.0 \
    OPC_FINANCE_PORT=8765

COPY --from=builder /wheels /wheels
RUN python -m pip install /wheels/*.whl \
    && rm -rf /wheels \
    && groupadd --gid 10001 opc-finance \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin opc-finance \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/opc-finance

USER 10001:10001
WORKDIR /var/lib/opc-finance
EXPOSE 8765
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2).read()"]

ENTRYPOINT ["opc-finance-workbench"]
