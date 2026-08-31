"""改 Dockerfile 为多阶段构建：
  Stage 1 (node:20-alpine):  npm ci && npm run build
  Stage 2 (python:3.12-slim): pip install + copy backend + copy frontend (含 stage1 的 dist/)

最终镜像：
  - 保留原 Python slim 镜像小体积优势
  - 不含 Node / node_modules
  - 含 frontend/static/dist/（Vite 构建产物，含 hash 文件名）
  - main.py 自动检测 dist/，USE_VITE_DIST=True 直接服务
"""
from pathlib import Path

p = Path("Dockerfile")
new = """# 多阶段构建：Stage 1 用 Node 构建前端，Stage 2 只保留 Python + 构建产物
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

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PIP_NO_CACHE_DIR=1 \\
    TZ=Asia/Shanghai \\
    DATA_DIR=/app/data

WORKDIR /app

# 时区：容器内交易时段判断依赖东八区
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY requirements.txt requirements-akshare.txt ./
RUN pip install --no-cache-dir -r requirements.txt \\
    && if [ "$WITH_AKSHARE" = "true" ]; then \\
         pip install --no-cache-dir -r requirements-akshare.txt; \\
       fi

COPY backend/ ./backend/
# 前端源（main.py 在生产模式下不直接读它，但保留以支持快速切回 dev 模式）
COPY frontend/ ./frontend/
# 关键：从 Stage 1 拷入 Vite 构建产物（覆盖 frontend/static/dist/）
COPY --from=frontend-build /build/frontend/static/dist/ ./frontend/static/dist/
# 根目录脚本：自检回测（check_sources 运行时引用）、对照实验
COPY backtest_intraday.py backtest_compare.py ./

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
"""

p.write_text(new, encoding="utf-8")
print("OK 已写多阶段 Dockerfile")
