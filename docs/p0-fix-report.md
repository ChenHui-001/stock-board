# P0 修复报告

日期：2026-09-02 · 基线 commit `41559fd` 之前 · 测试 68 → **89 passed**

本报告记录 `docs/project-analysis.md` 里 6 条 P0 的修复过程、验证证据与遗留决策。

---

## 一、总览

| # | 问题 | 修复位置 | 验证方式 | 结果 |
|---|---|---|---|---|
| 1 | K 线单点失败拖垮整页 | `backend/service.py:_kline` | 运行时探针：模拟 K 线源宕机 | 6/6 |
| 2 | 同步 sqlite3 阻塞事件循环 | `backend/storage.py` + 4 处调用点 | 全量测试 + 调用点静态核对 | 通过 |
| 3 | DB 无 schema 迁移机制 | `backend/storage.py:run_migrations` | 8 条新测试 | 通过 |
| 4 | 回测轮询跨页泄漏 | `page-backtest.js` + `app.js` | Node 探针（假 DOM + 假 fetch） | 11/11 |
| 5 | 归因标定脚本未入库 | `scripts/factor_attribution/` | 真实 import 验证路径解析 | 8/8 |
| 6 | LLM 串行故障转移无超时 | `backend/llm.py` + `config.py` | 运行时探针 | 5/5 |

---

## 二、逐项说明

### P0-1　K 线单点失败拖垮整页

7 路并发取数里，只有 `quote` 和 `_kline` 会在失败时抛异常，其余 5 路都有兜底。
结果就是 K 线源一挂，整个详情页 503，哪怕行情/资金/两融全部正常——降级粒度不一致。

**修复**：给 `_kline` 的 `cached_pack` 补 `empty={"bars": [], "source": ""}`，与 `_kline_min` / `flow` 对齐。
同时在 `sources` 里新增 `errors` 字段，把「取数失败且无历史数据可回退」的源上报给前端。

**关键点**：`errors` 的筛选条件是 `pack.get("error") and not pack.get("stale")`。
stale 回退的情况**不进** errors——那时有旧数据可展示，前端已知「数据过期」，不算故障。

**验证**：`cached_pack` 三条分支全覆盖（无 empty 且无 stale → 抛；有 empty → 返回空包；有 stale → 回退旧数据）。

### P0-2　同步 sqlite3 阻塞事件循环

**先更正原报告的判断**：原报告称「async 路径 34 处调用、需要换连接模型」。实测发现：
- `storage.py:_connect()` **早已**传 `check_same_thread=False`，跨线程复用单连接本来就安全；
- 模块级 `_lock` 已把所有 DB 操作串行化；
- `backtest/score_strategy.py` 已有 `_to_thread` 先例。

所以**不需要 aiosqlite、不需要每线程一连接**，`asyncio.to_thread` 薄封装即可。

**做了三件事**：
1. 11 个 `a_*` 异步封装（`a_list_watchlist` / `a_watched_codes` / `a_save_report` …），同步原函数全部保留；
2. 消除 N+1 读：`api_deps.py:_mark_value_watched` 原在双层循环里逐行 `is_watched()`，70 次独立查询 + 70 次抢锁 → 改为一次 `watched_codes()` + 集合判定；
3. 消除 N+1 写：`service.py` 循环内逐行 `update_meta()`（同一行还可能触发两次）→ 循环中只收集，循环后 `update_meta_batch` 一次 `executemany` 落库。

**死锁分析**：`_lock` 只在 worker 线程内部由同步函数短暂持有，拿到锁后一路跑完即释放，中间无 `await`。
已静态核对全模块，**没有任何函数二次取锁**。`a_*` 封装只包同步函数，绝不包异步逻辑。

**遗留决策**：还剩 13 处同步调用（`llmcfg` 5 / `scorecfg` 3 / `valuecfg` 3 / `main.py` 2），**决定不继续改**，理由见第五节。

### P0-3　DB 无 schema 迁移机制

引入 SQLite 标准的 `PRAGMA user_version` + 有序迁移清单 `MIGRATIONS`。
`_init()` 建出的三张表 = version 1；存量老库 `user_version=0` 时直接盖章，不必补建。

三条边界处理得比较扎实：
- **幂等**：只跑版本号 > 当前 `user_version` 的迁移；
- **失败不留残**：单条迁移失败立即 `rollback` 并抛错，`user_version` 不动，下次启动重试同一条；
- **版本回退只告警不动数据**：库比代码新时（`current > target`）只 `log.warning`，不硬跑——用户数据比 schema 版本号重要。

`MIGRATIONS` 里的注释示例正是 `ALTER TABLE watchlist ADD COLUMN cost_price REAL`，
**持仓成本录入的入口由此打通**。

### P0-4　回测轮询跨页泄漏 + 污染当前页

`setInterval(pollOnce, 1500)` 的 `stopPolling()` 只在内部调用，`PageBacktest` 没有 destroy 钩子。
切页后轮询继续跑，回测完成时还会把结果渲染进**已被替换的 `#view`**。

**修复分两层**：
- `app.js:route()` 加通用卸载机制：页面模块只要导出 `destroy`，离开时自动调用；
- `page-backtest.js` 加 `active` 标志 + `destroy()`，并在 `pollOnce` 的 `await` 前后各插一次检查
  （`await` 期间用户可能已经切走，回调回来时 `#view` 已被别的页面整块替换）。

**验证**：Node 探针 11/11，含最关键的 B 组——模拟「回测在飞时切走页面」，
断言卸载后定时器停止、`#view` 零写入、不再弹 toast、不再拉历史/明细。

### P0-5　归因标定脚本未入库

生产 `rule_engine.py` 的因子权重与阈值全部来自 `tmp/` 下的归因脚本，而 `tmp/` 被 gitignore
→ **权重没有可复现路径，换机器即失传**。

**修复**：8 个脚本迁入 `scripts/factor_attribution/`，硬编码的 `ROOT = Path(r"E:\project\股票看板")`
改成 `Path(__file__).resolve().parents[2]`，产物目录 `OUT_DIR = SCRIPT_DIR / "results"` 随脚本走。

**验证不是读文本，而是真实 import 每个模块**再断言它解析出的 `ROOT/SCRIPT_DIR/OUT_DIR`——8/8 通过。
（所有脚本都有 `if __name__ == "__main__"` 守卫，import 不会触发重活。）

22 个结论级 CSV + 归因报告入库；4 个大体积原始事件表（`factor_events.csv` 762KB 等）排除，
它们可由脚本重跑还原。附 `README.md` 说明复现顺序与核心结论。

### P0-6　LLM 串行故障转移无外层超时

4 个档案串行遍历，单档案最坏跑满 120s，最坏 **8 分钟**挂起，而 `analyze()` 没有外层超时。

**修复**：新增 `LLM_TOTAL_TIMEOUT`（默认 180s = 单档案 120s + 约 60s 换源机会），
用 `asyncio.wait_for` 包住整个故障转移流程；超时转抛 `LLMError`（而非裸的 `TimeoutError`），
让调用方 `analysis/__init__.py` 既有的 `except llm.LLMError` 降级分支直接接住。

`_clamp_llm_total_timeout` 保证总预算不低于单档案超时——否则连第一个档案都会被提前掐断，故障转移形同虚设。

**验证**：挂起场景从最坏 240s 掐到 **0.31s**；断言抛出的是 `LLMError` 且消息含「总预算…耗尽」；
同时验证正常故障转移没被破坏（第一个档案失败仍能切到第二个）。

---

## 三、额外发现并修复：静默失效的测试钩子

**这不在原 P0 清单里，但危害比多数 P0 更大。**

`tests/conftest.py` 有个 `pytest_collection_modifyitems` 钩子，用来给异步用例自动补 `asyncio` 标记，
判据写成 `co_flags & 0x100`。但：

```
CO_COROUTINE          = 0x80    ← 判断异步函数应该用这个
CO_ITERABLE_COROUTINE = 0x100   ← 实际用的是这个
async def 的 co_flags = 0x83
⇒ 0x83 & 0x100 = 0，判据恒为假
```

**这个钩子从写下那天起就没生效过。** 后果是异步用例**不报错、不执行，直接被跳过**，
表现为「测试数量莫名变少」且没有任何告警。这种静默失败比显式报错危险得多。

**修法**：没有去改那个常量，而是**删掉整个钩子**，改用 `pytest.ini` 的 `asyncio_mode = auto`
让 pytest-asyncio 官方接管——不维护手写位运算判据。顺带配了
`asyncio_default_fixture_loop_scope = function`，消掉弃用告警。

**双向验证**：auto 模式下不带任何标记的 `async def test` 正常执行（PASS）；
切回 strict 模式立刻复现静默跳过（SKIPPED）。

---

## 四、额外发现并修复：详情页定时器泄漏

修复 P0-4 时顺带扫了其他页面，发现 `page-detail.js` 有一处**比回测页更严重**的同类问题：

```js
_detailTimerHandle = setInterval(function () { ... if (remain <= 0.1) tick(true); }, 200);
```

全文只在 `403 / 423 / 424` 三处出现，**没有任何跨页清理路径**。交易时段（`interval_ms > 0`）
进入详情页后，这个 200ms 定时器会**永久运行，每 5 秒打一次行情接口**。
比回测页更糟——回测跑完轮询会自停，这个只要离开页面就一直打到关掉浏览器。

**修复**：加 `_detailActive` 标志 + `destroy()`，并在定时器回调开头和 `tick()` 里都加守卫
（`app.js` 的全局刷新定时器会调 `currentPage().tick()`，未挂载时必须 no-op，否则仍会往已替换的 DOM 里写）。

**验证**：Node 探针 18/18，含 4 条详情页断言，且带「挂载后确实在跑（4 次请求）」的前置断言
——少了这条，「切走后 0 次」可能只是因为定时器压根没启动而假通过。

---

## 五、做过的判断（不只是执行）

1. **P0-2 剩余 13 处不改**。9/11 在用户手动触发的 settings 页（低频），2 处在 `main.py` 启动预热（此时不接请求）。
   真正热的 `llmcfg.fingerprint()` / `scorecfg.fingerprint()` 每次 AI 请求都调且确实读库，
   但它们所在的 `_cached_report()` 是同步函数，改造会波及整个 AI 缓存读取链路。
   **且异步化不是最优解**——指纹只在用户改配置时才变，每次请求重算+读库本身就是浪费，
   正确解法是加缓存（或保存时失效），直接消灭这次查询而非把它挪到线程里。已列为 P1 待办。

2. **`sources.errors` 只保留 4 个源**，刻意不纳入 `boards` 和 `kline_min`：
   - `boards`：板块为空常常是**正常状态**（部分股票本就没有板块归类），进 errors 会误报。
     误报比漏报更糟——漏报只是少个提示，误报会让人以为系统坏了。
     且 `_boards` 返回 `list[Board]` 而非 pack，`error` 在转换时就丢了，接进来要动数据层。
   - `kline_min`：P2-7 的增强项，缺了只是图表降级；且 `_kline` 已占「K线」显示名，会重名。

   → `errors` 的语义定为「**本该有数据、却连旧数据都没有**」，不是「任何没取到的东西」。

3. **接受工程师用 `asyncio.to_thread` 而非指定的专用单线程执行器**。
   残留风险是回测占满默认线程池时 DB 调用排队，但主目标（不阻塞事件循环）已达成，
   这是单人本地看板，不值得为此多一轮往返。

4. **修正了自己的报告**：`docs/project-analysis.md` 原称 P0-2 需「换连接模型、34 处调用」，
   实测 33 处且不需要换连接模型。已按实测更正，避免后续照着错误前提执行。

---

## 六、需要留意

- **`auto-commit-watch.ps1` 用 `git add -u`，会跳过未跟踪文件。**
  本轮的 `docs/`、`scripts/factor_attribution/`、`pytest.ini` 原本全是未跟踪状态，
  直接推送会**整个丢失**。已手动 `git add` 暂存。后续新增目录务必显式 add。
- **watcher 的 5 秒防抖撑不住高强度改动。** 工程师做变异测试（故意改坏代码以证明测试真的会红）时，
  被 watcher 抢跑了 4 次提交。当前 `HEAD` 已验证干净（`empty=` 在 `service.py:679`），
  但后续若还要做破坏性验证，建议先停 watcher。
- **pytest 在沙箱下必须显式给 PYTHONPATH**，否则起不来：
  ```
  PYTHONIOENCODING=utf-8 PYTHONPATH="C:/Users/王/AppData/Roaming/Python/Python312/site-packages" \
    "C:/Program Files/Python312/python.exe" -m pytest tests/ -q
  ```
