# 股票看板 · 项目与代码健康度评估

> 评估日期：2026-09-02
> 评估方式：四路并行只读调研（后端 / 前端 / 数据层+分析引擎 / 测试与质量），关键结论由主理人二次实测复核
> 实测基线：`pytest tests/` **60 passed / 17.04s**（含 Python 路径修复，见附录 A）

---

## 一、项目全貌

| 维度 | 数据 |
|---|---|
| 定位 | A 股自选股监控 + AI 分析 Web 应用（个人短线交易者自用） |
| 后端 | Python / FastAPI 0.115 + Pydantic v2 + httpx 异步 + SQLite，**49 文件 / 17011 行** |
| 前端 | 原生 JS + ECharts，**活跃 13 文件 / 7172 行**（`_backup_iiife/` 另有 6 文件 6093 行已 gitignore） |
| 测试 | 18 文件 / 3836 行 / **60 用例，全绿 17s** |
| 一次性脚本 | `scripts/` 38 个（37 个带 `_` 前缀，已入库）、`tmp/` 110 文件（已 gitignore） |
| 构建 | **Vite 双模**：生产走 `frontend/static/dist`（161KB 单包），开发走裸 ESM |
| 部署 | Docker 多阶段构建 + docker-compose（命名卷 / 日志上限 / 内存上限 / 时区均已配） |

### 请求链路（`GET /api/stock/{code}`）

```
api.py:384  参数校验（\d{6}）
  └─ service.py:757  stock_detail()
       └─ 7 路 create_task 并发 → asyncio.wait(FIRST_EXCEPTION)  service.py:773
            quote / kline / kline_min / flow / margin / financials / boards
            └─ providers/ 多源竞速（eastmoney > ths > tencent > sina > akshare）
                 主机节流 → Throttled 快速换源 → Registry 熔断(BREAK_AFTER=3/COOLDOWN=60s)
       └─ indicators: build_ma / ATR14 / support_resistance / summarize_flow / build_status
  └─ 返回裸 dict（无 response_model）
```

---

## 二、做得好的地方（值得保留的设计）

1. **回测的前视偏差防护到位** —— 这是 A 股回测最容易踩的两个坑，都做对了：
   - 信号在 bar `i` 收盘计算，**买入价取 `i+1` 的 open**（`backtest/score_strategy.py:267`）
   - 基本面按**披露日** `pub_date <= signal_date` 过滤，而非报告期（`score_strategy.py:187`）

2. **数据源弹性设计真实有效，不是伪降级** —— 三级防护：主机节流+自适应间隔 → `Throttled` 快速失败换源（403/429/456/503）→ Registry 熔断。akshare 用 `asyncio.to_thread` 正确规避阻塞。全项目 `requests`/`time.sleep` **0 处**。

3. **缓存层质量高** —— `cache.py` 有 TTL + **单飞**（per-key `asyncio.Lock` + waiters 计数 + double-check）+ 定期清扫 + 容量上限 4096。这是自研缓存里少见做对单飞的。

4. **`cached_pack` 双层兜底** —— 成功写 `stale:{key}`（TTL 24h），失败回退旧值并标 `stale=True`，前端可感知（`service.py:41-62`）。数据过期有 `status="delayed"` 透传。

5. **注释以「为什么」为主** —— `smoke_test.py:1-21`（重构历史）、`conftest.py:18-28`（为何用 mkdtemp）、`Dockerfile:39-43`（明令禁止从 host COPY dist）、`e2e_check.py:10-13`（GBK 导致 print 崩溃）。`TODO/FIXME` 全后端仅 4 处。

6. **`FACTOR_WEIGHTS` 的降权有实证支撑** —— 上一轮因子归因回测后，把 IC 稳定为负的 5 个因子压到 0.35、唯一 7/7 期为正的 MA20 乖离提权到 2.50，**降权而非反向**（样本外 t=-2.25 证伪了动量取反）。注释里写清了依据，不是拍脑袋调参。

7. **前端 XSS 防护良好** —— 62 处 `innerHTML` 绝大多数是清空操作，HTML 构造统一走 `U.el()`（createElement + textContent）；`U.safeUrl` 拦 `javascript:`/`data:`；未见 `${}` 拼进 `on*` 内联事件。

---

## 三、风险清单

### P0（建议优先处理）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| 1 | **K 线单点失败拖垮整页** | `service.py:667` `_kline` 未传 `empty=` | 7 路并发中只有 quote/kline 会抛异常，其余 5 路都有兜底。**K 线源一挂，整个详情页 503**，即使行情/资金/两融全部成功。降级粒度不一致 |
| 2 | **同步 sqlite3 阻塞事件循环** | `storage.py` 全模块，async 路径 **33 处调用** | 全局单连接 + `threading.Lock`，含 N+1 写（`service.py:573/585/825`、`api_deps.py:174/179` 循环内调 DB）。高并发下事件循环阻塞、锁争抢 |
| 3 | **DB 无 schema 迁移机制** | `storage.py:29-53` | 全仓库无 `ALTER TABLE`/`user_version`/migration。给 `watchlist` 加字段（如持仓成本）需手写 ALTER 且无执行入口。好消息：零 `SELECT *`，不会崩，但要同步改 3 处 |
| 4 | **回测轮询跨页泄漏 + 污染当前页** | `page-backtest.js:210, 227-229` | `setInterval(pollOnce, 1500)` 的 `stopPolling` 只在内部调用，`PageBacktest` 无 destroy → 切页后轮询继续；完成后 `renderStrategies/renderForm/renderResult` 写入**已被替换的 `#view`**，会污染当前页面 |
| 5 | **归因标定脚本未入库** | `tmp/factor_attrib.py`、`tmp/calibrate_threshold.py`（`tmp/` 被 gitignore） | 生产权重来自这两个脚本的结论，但**脚本本身没进版本库** → 权重无可复现路径，换机器即失传 |
| 6 | **LLM 串行故障转移无外层超时** | `llm.py:195-201` / `analysis/__init__.py:95` | 4 档案串行遍历，单次 120s，最坏 **4×120s = 8 分钟**挂起，而 `analyze()` 无外层超时保护 |

> **状态（2026-09-02 更新）：以上 6 项 P0 已全部修复并验证，详见 [`p0-fix-report.md`](./p0-fix-report.md)。**
> 修复过程中另有两项计划外发现，量级不低于 P0-4，已一并处理：
> - `tests/conftest.py` 的 `pytest_collection_modifyitems` 钩子用了错误常量（`0x100` 而非 `0x80`）判异步，
>   **导致异步测试长期被静默跳过而不报错**。已删除钩子，改用 `pytest.ini` 的 `asyncio_mode = auto`。
> - `page-detail.js` 的倒计时定时器（`setInterval` 200ms，每 5s 打一次行情接口）无卸载清理，
>   切页后永久存活。比 P0-4 更严重，因为它不会自行终止。已补 `destroy()`。
>
> 遗留：P0-2 的 33 处调用中仍有 13 处未改异步，主要是冷路径；热点 `fingerprint()` 用缓存解决
> 比异步化更划算，列为 P1 待办。

### P1

| # | 问题 | 位置 |
|---|---|---|
| 7 | `providers/__init__.py` 606 行包初始化塞了完整 Registry（熔断/竞速/健康统计），与 `backtest/registry.py` 同名不同物，无法独立测试 | `providers/__init__.py:59-60, 206-211, 339-425` |
| 8 | `value_screener.py` 绕过 service 缓存层重造取数链路（`_fin/_flow/_kline`），且 `except: return []` 静默降级 | `value_screener.py:437-461, 365/414/443` |
| 9 | `rule_based` **438 行单函数**，全项目最长 | `rule_engine.py:420` |
| 10 | DB 连接无关闭钩子，WAL 从不 checkpoint（`data/board.db-wal` 49KB） | `main.py:64-75` |
| 11 | 回测 4 档 vs 生产 3 档口径不一致（`ORDER` 仍有"清仓"） | `score_strategy.py:32` vs `rule_engine.py:27-32` |
| 12 | 东财 `f1/f2/f3` 字段硬编码 54 处，`QUOTE_FIELDS` 只是字段清单**非语义映射表** | `eastmoney.py:39`，含义全靠注释 `:35-41` |
| 13 | `watch_monitor` 205 行 if 链，阈值 `-3/1.5/0.3/2.0/3.0/10/0.8/5e5/1e4` 全硬编码 | `service.py:264-468` |
| 14 | 静默吞异常 **32 处**（无日志且以 pass/return 结束） | 见附表 |
| 15 | `api_deps.py:64-92` vs `:95-116` 26 行近乎逐行复制 | `api_deps.py` |
| 16 | ESM 求值顺序脆弱：`app.js:630` 在模块体尾部执行 `App.start()`，此时被依赖模块（反向 import App 的 3 个页面）尚未求值 | `app.js:6-12, 630` |
| 17 | 防竞态机制 4 套写法，home/value/backtest **无任何守卫** | `page-home.js:607` 等 |

### P2 / P3

| # | 问题 | 位置 |
|---|---|---|
| 18 | `response_model` **0 处**，全返回 `dict[str, Any]`；`Any` 全项目 532 次 | `api.py` 全部路由 |
| 19 | `.gitignore:50` 裸 `index.html` → 会忽略**任意层级** index.html | `.gitignore` |
| 20 | `hsearch:` 缓存 key 未转义用户输入（含 `:` 会错位） | `hotspot_search.py:240` |
| 21 | 缓存空值穿透（`loader()` 返回 None 不写入）+ 清扫只在 `put()` 触发 | `cache.py:116, 43` |
| 22 | 回测缓存无 GC（`backtest_cache/` 只读不删）+ `data/` 5 个测试残留 | `engine.py:104` |
| 23 | 弹窗三套同构实现（`ai.js`/`news.js`/`page-hotspot.js`）；`view()` 重复定义 4 次 | 多处 |
| 24 | 代码归一化逻辑 4 处各写一遍；HTTP 客户端单例 3 处 | `utils.py:79` / `engine.py:183` / `ths.py:33` / `render_dashboard.py:117` |
| 25 | 10 个测试文件复制粘贴相同样板导入头 | `tests/` |
| 26 | 首屏 ECharts 全量 1.03MB 阻塞式加载，而 4 个页面不用图表 | `index.html:111` |
| 27 | `e2e_check.py` 死代码（零引用、依赖未声明、不在 CI） | 根目录 |
| 28 | Docker 无 `HEALTHCHECK`、无 `USER`（root 运行）、`tests/` 不进镜像 | `Dockerfile` / compose |

---

## 四、测试与质量现状

### 实测结果

```
pytest tests/ -q  →  60 passed in 17.04s
```

覆盖映射（用例数 top 6）：`test_backtest`(15) / `test_hotspot`(2) / `test_value_screener`(3) / `test_analysis`(3) / `test_indicators`(3) / `test_providers`(7)。

`backend/smoke_test.py` 只是 47 行 shim（`pytest.main(["tests"])`），**不存在两套测试**，保留入口仅为兼容 watcher 与 CI。

### 三类「测了个寂寞」

1. **路由层靠读源码文本** —— `tests/test_ai_route_order.py:14,40` 用 `read_text()` + 正则匹配源码。断言的是「源码里写了这行」，不是「请求返回正确」。**全项目 0 处 `TestClient`**，HTTP 层无任何行为测试。
2. **恒真断言** —— `tests/test_value_screener.py:406/417` `assert (True), "skipped..."`。离线时**伪装成通过**而非 skip，虚增通过数。这是唯一覆盖 `run_screen` 端到端的用例。
3. **零参数化** —— 全库 `parametrize` **0 次**。`test_indicators.py` 862 行只有 3 个函数（单个函数跨 495 行），新增用例只能继续堆，挂了定位不到是哪组输入。

### 覆盖盲区

| 盲区 | 严重度 |
|---|---|
| **前端 7172 行零自动化测试**（无 `*.test.js`/`*.spec.js`，`package.json` 无 test 脚本） | 🔴 最大敞口。前端是改动最频繁的模块，护栏为零。`e2e_check.py` 是现成 Playwright 骨架但未接入 |
| `backend/api.py`（640 行路由层）仅源码文本测试 | 🔴 |
| 上游边界场景 **0 覆盖**：限流/脏数据/停牌股/新股无 K 线 | 🔴 恰是 A 股数据最容易炸的四类 |
| `indicators.py` 26 个公开函数只测 9 个（未测 `macd_series`/`kdj_series`/`compute_oscillators`/`intraday_state_from_quote` 等） | 🟠 指标算错一行，测试全绿 |
| `aks.py` / `api_deps.py` / `main.py` / `hotspot_search.py` / `news.py` **0 测试** | 🟠 |

### 工程化

- **Lint/格式化：全项目零配置**（无 ruff/flake8/black/eslint/prettier/pyproject.toml）。`conftest.py:32` 的 `# noqa: E402` 是失效注释。
- **CI 很薄**：`.github/workflows/docker-push.yml` 唯一工作流，只跑 pytest → 推镜像。不跑 lint、不验镜像能起、不测前端。
- **`auto-commit-watch.ps1` 有 5 个真实风险**（详见附录 B），最严重的是 **新文件永不入库**（`--untracked-files=no` + `git add -u`）和 **token 进命令行**。但它确实在拦截：`auto-commit.log` 历史中 `1 failed` 出现 **159 次**，都被正确挡在 commit 之前。

---

## 五、建议的下一步（按性价比排序）

1. **给 `_kline` 补 `empty=` 兜底**（`service.py:667`）—— 一行改动，消除整页 503
2. **把归因标定脚本从 `tmp/` 挪进 `scripts/` 或 `docs/` 并入库** —— 否则权重结论不可复现
3. **修 `page-backtest.js` 轮询泄漏** —— 当前会污染用户正在看的页面
4. **给 `analyze()` 加外层超时** —— 避免 LLM 全挂时挂 8 分钟
5. **引入 DB 迁移机制**（哪怕只是 `PRAGMA user_version` + 版本化 ALTER 列表）—— 加持仓成本字段的硬前提
6. **把 `e2e_check.py` 接进 CI** —— 性价比最高的一件事，能立刻给 7172 行前端代码加护栏
7. **清理**：`_backup_iiife/`（4415 行无用）、`scripts/` 37 个一次性脚本、`.gitignore` 裸 `index.html` 规则

---

## 附录 A：测试环境路径问题（重要）

本项目在本机沙箱中运行 pytest 需要显式指定 user site-packages：

```bash
PYTHONIOENCODING=utf-8 \
PYTHONPATH="C:/Users/王/AppData/Roaming/Python/Python312/site-packages" \
"C:/Program Files/Python312/python.exe" -m pytest tests/ -q
```

原因：沙箱将 `PYTHONPATH` 指向了 WorkBuddy 的 shim 目录，导致 Python 3.12 的 user site 解析异常（实际解析为 `C:\Users\王\Python\Python312\site-packages`，而 pytest 8.3.4 装在 `AppData\Roaming\...`）。不指定则报 `No module named pytest`。

## 附录 B：auto-commit-watch.ps1 风险明细

| 风险 | 位置 | 说明 |
|---|---|---|
| 监听范围失效 | `:31-32` | `$watchPaths`/`$exclExts` 定义后从未使用，实际用全仓库 `git status --porcelain`；改 `docs/` 甚至脚本自身都会触发提交 |
| Token 进命令行 | `:86` | URL 明文拼接 token，可被进程列表/日志捕获 |
| 新文件永不入库 | `:116, :156` | `--untracked-files=no` + `git add -u` → 新建的测试/模块被静默漏掉 |
| 无 pull 直接 push | `:134` | 远端有他人提交即失败（日志中已见 `push failed (exit=128)`） |
| 零人工 review | `:126` | smoke_test 通过即自动推 main，无 PR |

## 附录 C：本次评估的复核记录

调研 agent 的部分结论与主理人实测存在冲突，以下为复核后的**更正**：

| agent 原结论 | 复核结果 |
|---|---|
| 「本机 4 个 Python 环境均未安装 pytest，测试跑不起来」 | ❌ 不成立。pytest 8.3.4 已安装，需显式指定 `PYTHONPATH`（见附录 A）。实测 **60 passed** |
| 「6 个新用例从未留下执行记录，是否通过待确认」 | ✅ 已确认通过，实数 60 而非日志中的 54 |
| 「`package.json`/`vite.config.js` 是死配置」 | ❌ 不成立。Vite 双模是设计良好的生产/开发切换（`main.py:27-30`，dist 于 09-02 14:06 构建） |
