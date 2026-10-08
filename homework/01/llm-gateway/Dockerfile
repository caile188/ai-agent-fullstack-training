# syntax=docker/dockerfile:1
# ---- builder：在独立 venv 中装依赖，便于精简拷贝到运行镜像 ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# 先装依赖（利用层缓存）：仅需 pyproject，无需源码
COPY pyproject.toml ./
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# 安装运行期依赖（不含 dev 测试依赖）
RUN pip install --upgrade pip && pip install .

# ---- runtime：最小镜像 + 非 root 运行 ----
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    GATEWAY_CONFIG=/app/gateway.yaml

# 容器内非特权用户
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# 依赖 venv
COPY --from=builder /opt/venv /opt/venv
# 应用代码、迁移脚本、默认配置
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser gateway.yaml ./gateway.yaml

# 持久化 SQLite 的目录（默认 data/gateway.db）
RUN mkdir -p /app/data && chown -R appuser:appuser /app/data
VOLUME ["/app/data"]

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/', timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
