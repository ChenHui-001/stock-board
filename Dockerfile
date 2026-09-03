# 多阶段构建：Stage 1 用 Node 构建前端，Stage 2 只保留 Python + 构建产物
# 优点：最终镜像不含 node_modules / node 二进制；Python slim 镜像小体积优势保留
# 不使用外部 dockerfile 语法镜像，构建不依赖 registry 拉取语法前端（离线可构建）

# ============================================================ Stage 1：前端构建
FROM node:20-alpine AS frontend-build

WORKDIR /build

# 单独 COPY package*.json 优先利用 Docker 缓存（依赖文件未变则不重跑 npm ci）
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

# 复制 Vite 配置 + 前端源
COPY vite.config.js ./
COPY frontend/ ./frontend/

# 构建（输出到 frontend/static/dist/，含 main.py 已知的 /static/dist/ 前缀）
RUN npm run build


# ============================================================ Stage 2：Python 运行时
FROM python:3.12-slim

# 是否把 AkShare 打进镜像（辅助数据源，依赖 pandas，体积 +~400MB）
ARG WITH_AKSHARE=false

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/app/data

WORKDIR /app

# 时区：容器内交易时段判断依赖东八区
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt requirements-akshare.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_AKSHARE" = "true" ]; then \
         pip install --no-cache-dir -r requirements-akshare.txt; \
       fi

COPY backend/ ./backend/
# 前端源（main.py 在生产模式下不直接读它，但保留以支持快速切回 dev 模式）
COPY frontend/ ./frontend/
# 关键：从 Stage 1 拷入 Vite 构建产物（覆盖 frontend/static/dist/）
# ============================================================
# ⚠️  dist 必须从源码 build，**禁止**从 host 直接 COPY frontend/static/dist
# Stage 1 已通过 `RUN npm run build` 从源码生成 dist，本 Stage 仅从 Stage 1 拷入
# 任何修改 Dockerfile 的 commit 请同步检查此规则未被打破
# ============================================================
COPY --from=frontend-build /build/frontend/static/dist/ ./frontend/static/dist/

# 非 root 运行：安装 gosu（Debian 官方仓库自带，~200KB），创建降权用户。
# 存量 root 属主数据卷的属主修复由 entrypoint 在启动时自动完成（无需手工迁移）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

# 健康检查：/healthz 是纯轻量端点（不触发数据源探测），探测进程内存活即可
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
