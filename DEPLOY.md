# 股票看板 · 服务器部署手册

基于 Docker Compose 部署，**全部配置走环境变量**（无 `.env` 文件、无 healthcheck）。
compose 文件已固定项目名 `stock-board`，在任意目录（含中文目录名）均可直接执行。

---

## 目录

1. [前置要求](#1-前置要求)
2. [准备部署文件](#2-准备部署文件)
3. [首次部署](#3-首次部署)
4. [配置说明（环境变量）](#4-配置说明环境变量)
5. [日常维护](#5-日常维护)
6. [升级](#6-升级)
7. [备份与恢复](#7-备份与恢复)
8. [回滚](#8-回滚)
9. [故障排查](#9-故障排查)
10. [反向代理（可选）](#10-反向代理可选)

---

## 1. 前置要求

| 组件 | 版本要求 |
| --- | --- |
| Docker Engine | ≥ 24 |
| Docker Compose | ≥ 2.20（`docker compose` 子命令形式，非旧版 `docker-compose`） |

验证：

```bash
docker version --format '{{.Server.Version}}'
docker compose version
```

服务器需能访问外网（东方财富/腾讯/新浪等行情接口、Docker Hub）。

---

## 2. 准备部署文件

部署只需要以下文件/目录（其他如 `data/`、`shots/`、文档等无需上传）：

```
stock-board/
├── backend/                # Python 后端
├── frontend/               # 前端静态资源
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-akshare.txt
```

上传到服务器（示例路径 `/opt/stock-board`，目录名随意，建议用英文避免歧义）：

```bash
scp -r backend frontend Dockerfile docker-compose.yml requirements.txt requirements-akshare.txt user@server:/opt/stock-board/
```

---

## 3. 首次部署

在项目目录下执行：

```bash
cd /opt/stock-board
docker compose up -d --build
```

- 默认监听宿主机 **8000** 端口，浏览器访问 `http://服务器IP:8000`。
- 需要自定义端口、配置 AI Key 等，见下节。

验证是否就绪（无 healthcheck，直接探测接口）：

```bash
curl -s http://127.0.0.1:8000/healthz   # 返回 {"ok":true} 即正常
curl -s http://127.0.0.1:8000/api/meta  # 查看数据源与 AI 引擎状态
```

---

## 4. 配置说明（环境变量）

所有配置项都定义在 `docker-compose.yml` 的 `environment:` 中，**带默认值**。
部署时用 shell 环境变量覆盖，不传则用默认值：

```bash
HOST_PORT=8898 LLM_API_KEY=sk-xxx docker compose up -d --build
```

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_PORT` | `8000` | 宿主机对外端口（不要用 `PORT`，部分终端会注入该变量导致随机端口） |
| `LLM_ENABLED` | `true` | 是否启用 LLM 分析 |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容服务地址（DeepSeek/通义/Kimi/智谱/Ollama 均可） |
| `LLM_MODEL` | `deepseek-chat` | 模型名 |
| `LLM_API_KEY` | 空 | 不填则 AI 分析自动降级为内置规则引擎 |
| `LLM_JSON_MODE` | `true` | 不兼容 `response_format` 的端点设 `false` |
| `LLM_TIMEOUT` / `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` | `120` / `4000` / `0.25` | 请求超时（秒）/ 最大 token/温度。主分析生成最多 4000 token 的 JSON，DeepSeek 盘中高峰常需 60s 以上；代码层保底 ≥90s，过小的旧配置（如 45s）自动提到下限 |
| `LLM_THINKING_MAX_TOKENS` | `8192` | 思考类模型（deepseek-reasoner 等）输出配额；思考过程占用配额导致正文为空时自动放大到此值重试 |
| `PROVIDER_ORDER` | `eastmoney,ths,tencent,sina,akshare` | 行情数据源故障转移顺序（同花顺 K 线为网页数据文件，东财不可用时优先回退） |
| `ENABLE_AKSHARE` | `true` | 启用 AkShare 兜底（需镜像以 `WITH_AKSHARE=true` 构建） |
| `HTTP_TIMEOUT` / `HTTP_RETRY` | `6` / `2` | 数据源请求超时（秒）/ 重试次数 |
| `QUOTE_TTL_OPEN` / `QUOTE_TTL_CLOSED` | `2.5` / `60` | 行情缓存（盘中秒 / 盘后秒） |
| `HISTORY_TTL_OPEN` / `HISTORY_TTL_CLOSED` | `120` / `900` | K线/资金/两融缓存（盘中 / 盘后） |
| `SEARCH_TTL` / `HOT_TTL` | `300` / `60` | 搜索 / 热门榜缓存（秒） |
| `KLINE_LIMIT` / `FLOW_DAYS` / `MARGIN_DAYS` | `260` / `30` / `30` | K线根数 / 资金流向天数 / 两融天数 |

**不需要 AkShare** 时减小镜像约 400MB：把 `docker-compose.yml` 里构建参数 `WITH_AKSHARE` 改为 `"false"`，并设 `ENABLE_AKSHARE=false`。

**AI 配置第二种方式**：部署后打开页面顶栏 ⚙ 设置，可界面配置厂商/模型/密钥（存入数据卷，重启不丢），优先级高于环境变量。

---

## 5. 日常维护

```bash
docker compose ps          # 查看容器状态与端口
docker compose logs -f     # 跟踪日志（Ctrl+C 退出）
docker compose logs --tail=100 stock-board   # 最近 100 行
docker compose restart     # 重启（不重建）
docker compose down        # 停止（数据卷保留）
```

常用端口探测：

```bash
curl -s http://127.0.0.1:8000/healthz
```

**数据源健康自检**（逐源实测行情/K线/资金/两融，报告新鲜度与限流状态）：

```bash
# 在项目目录（有源码时）
python backend/check_sources.py                 # 默认 4 只样本股
python backend/check_sources.py --code 600000   # 指定单只股票
python backend/check_sources.py --json          # JSON 输出（脚本/监控用）
```

退出码：`0`=至少一个行情源可用，`1`=全部行情源不可用，`2`=参数错误。
每次输出会标注各源 K 线/资金流最新日期、行情日期是否滞后（已判延迟的数据会在页面显示「数据更新延迟」）、被限流的主机与冷却剩余时间，以及**盘口信号近期命中率**（样本股快速回测：总分分桶单调性 + 各信号命中率与校准建议）。
页面顶栏 ⚙ 旁的数据源角标 →「立即自检」同样会展示该回测区块。

**盘口分项离线回测**（独立完整版：验证 AI 分析里盘中位置/量比/振幅/换手信号对次日涨跌的预测能力，输出命中率与权重校准建议）：

```bash
python backtest_intraday.py                          # 默认 12 只样本股 × 250 根日线
python backtest_intraday.py --codes 600000,601179 --days 200   # 自定义股票池/天数
python backtest_intraday.py --json report.json       # 额外输出机器可读报告（脚本/监控用）
python backtest_intraday.py --selftest               # 离线自测（不触网，CI 冒烟已覆盖）
```

报告含：总分分桶 vs 次日表现（看评分方向单调性）、各信号（高位强势/冲高回落/放量上攻/交投清淡等）的样本数/命中率/校准建议。用日线近似收盘时点，盘中实时信号强弱可能更强；样本 <50 的信号结论仅作参考。

**盘中 vs 收盘时点对照实验**（验证日线近似校准在盘中实时场景是否需要回调：同一交易日分别用 5 分钟线 14:00 真实快照与收盘快照打分，对比信号方向翻转率与命中率差异）：

```bash
python backtest_compare.py                          # 默认 12 只样本股 × 30 天（分钟线 3 页翻页）
python backtest_compare.py --codes 600000,601179   # 自定义股票池
python backtest_compare.py --json out.json          # 机器可读报告
python backtest_compare.py --selftest               # 离线自测（不触网，CI 冒烟已覆盖）
```

5 分钟线优先东财（单次 1024 根≈21 交易日），限流时自动回退腾讯 mkline 翻页（每页 480 根=10 交易日，默认 3 页）。输出三部分：两时点打分方向一致性/翻转率、各信号收盘 vs 盘中命中率对比、校准回调建议（盘中命中率显著高于收盘的信号说明日线校准低估了盘中信号，建议回调权重）。

---

## 6. 升级

改代码后重新构建并滚动升级（数据不丢）：

```bash
cd /opt/stock-board
docker compose up -d --build
```

如需升级镜像但不想重新构建（配合 CI 推送镜像）：

```bash
docker compose pull && docker compose up -d
```

---

## 7. 备份与恢复

自选股、AI 分析记录、LLM 界面配置全部在命名卷中。
本项目的卷名是 **`stock-board_stock-board-data`**（= compose 项目名 `stock-board` + 卷键 `stock-board-data`）。

**备份**（打包到当前目录，文件名带日期，已验证）：

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v stock-board_stock-board-data:/app/data \
  alpine tar czf - -C /app/data . > board-data-$(date +%Y%m%d).tar.gz
```

**恢复**（把备份解回数据卷后重启）：

```bash
docker compose down
MSYS_NO_PATHCONV=1 docker run --rm -i -v stock-board_stock-board-data:/app/data \
  alpine sh -c "rm -rf /app/data/* && tar xzf - -C /app/data" < board-data-20260701.tar.gz
docker compose up -d
```

> 卷名若对不上（改过项目名等），用 `docker volume ls | grep stock` 确认实际卷名。
> 在 **Windows Git-Bash** 下执行需加 `MSYS_NO_PATHCONV=1` 前缀（否则 `/app/data` 等路径参数会被自动转成 Windows 路径导致失败）；Linux/macOS 服务器不需要。

---

## 8. 回滚

回滚到上一个镜像版本（假设旧版本镜像 tag 为 `stock-board:1.0.0`）：

```bash
docker compose down
docker tag stock-board:1.0.0 stock-board:1.0.0-rollback   # 如需要先保留当前版本
docker compose up -d        # 使用旧镜像（docker-compose.yml 的 image 指向旧 tag 时）
```

若 compose 的 `image:` 与构建产物同名（当前默认 `stock-board:1.0.0`），回滚方式：
用 `git checkout` 恢复上一版代码后重新构建，或保留旧 tag 并临时改 `docker-compose.yml` 的 `image` 指向。

---

## 9. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| `docker compose` 报 project name 错误 | 已通过 compose 文件顶部 `name: stock-board` 解决；若仍报错请升级 Compose ≥ 2.20 |
| 构建卡在拉镜像 / `failed to resolve source metadata` | Docker Hub 网络不通。已移除 Dockerfile 的 `syntax` 外部指令，网络恢复后重试；基础镜像缓存后离线可构建 |
| 页面显示 `--` / 板块缺失 | 数据源被限流，系统会自动切换备用源并缓存兜底；看顶栏副栏的「⚠ 限流中」提示 |
| AI 分析一直走规则引擎 | 未配置 `LLM_API_KEY`，或 ⚙ 设置里配置的密钥无效；用设置弹窗的「测试连接」验证 |
| 端口被占用 | 换 `HOST_PORT` 重新 `docker compose up -d` |
| 自选股丢失 | 检查 `docker compose down -v` 是否误用（`-v` 会删数据卷）；正常 `down` 不丢 |
| 数据源报频控 | 东方财富等公开接口对单 IP 限流，属正常现象；系统有冷却+切换+缓存三重兜底，等待数分钟自动恢复 |

---

## 10. 反向代理（可选）

Nginx 配置示例（域名 + HTTPS 转发到容器）：

```nginx
server {
    listen 443 ssl;
    server_name board.example.com;
    ssl_certificate     /etc/nginx/ssl/board.crt;
    ssl_certificate_key /etc/nginx/ssl/board.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

容器内已启用 `--proxy-headers`，配合反代能正确获取客户端 IP。
