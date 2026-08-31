"""给 .gitignore 补充前端构建相关 patterns：
- node_modules/        npm 依赖（自动安装）
- frontend/static/dist/        Vite 构建产物（每次 build 重新生成）
- frontend/static/public/      Vite publicDir 复制产物（echarts 等）
- frontend/static/js/_backup_iiife/  IIFE→ESM 迁移时的临时备份（已无价值）
- tests/_smoke_data/           浏览器/uvicorn 临时运行产物

保留：
- package-lock.json     锁文件，commit 保证可重复构建
- vite.config.js        配置
- scripts/_*.py         阶段 1-4 迁移 / 验证脚本（参考用）
"""
from pathlib import Path

p = Path(".gitignore")
src = p.read_text(encoding="utf-8")
addition = """
# === 前端：Vite + npm ===
node_modules/
frontend/static/dist/
frontend/static/public/
frontend/static/js/_backup_iiife/

# === 测试运行产物 ===
tests/_smoke_data/
tests/_node_import.mjs
"""
if "node_modules/" not in src:
    src = src.rstrip() + "\n" + addition
    p.write_text(src, encoding="utf-8")
    print("OK 已追加 .gitignore")
else:
    print("已存在，跳过")
