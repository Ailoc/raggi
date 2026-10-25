# 多副本形态的构建入口。
#
# 前端产物必须先构建（dist 不进仓库）：`cd web && npm run build`。
# 单机自用不需要这个镜像；它服务于"API 多副本 + 共享缓存"的部署形态。
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RAG_DATA_DIR=/data

WORKDIR /srv/raggi

# 依赖层单独缓存：改代码不必重装依赖
COPY pyproject.toml README.md ./
COPY rag ./rag
RUN pip install --no-cache-dir ".[scale]"

# 前端产物由 CI/构建机产出后拷入（本镜像不装 Node）
COPY web/dist ./web/dist

RUN mkdir -p /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).status==200 else 1)"

# 进程数由部署编排决定（每个容器一套嵌入索引与 SQLite 连接）
CMD ["rag", "serve", "--host", "0.0.0.0", "--port", "8000"]
