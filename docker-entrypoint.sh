#!/bin/sh
# 容器入口：以 root 启动时修复数据卷属主并降权到 appuser 运行。
# 兼容旧部署：存量命名卷（root 属主）在首次用新镜像启动时自动 chown，
# 无需手工迁移。已是非 root 启动（compose user: 覆盖等）则直接执行命令。
set -e
if [ "$(id -u)" = "0" ] && command -v gosu >/dev/null 2>&1; then
    mkdir -p /app/data
    chown -R appuser:appuser /app/data
    exec gosu appuser "$@"
fi
exec "$@"
