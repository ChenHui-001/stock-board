# 不使用外部 dockerfile 语法镜像，构建不依赖 registry 拉取语法前端（离线可构建）
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
COPY frontend/ ./frontend/
# 根目录脚本：自检回测（check_sources 运行时引用）、对照实验
COPY backtest_intraday.py backtest_compare.py ./

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
