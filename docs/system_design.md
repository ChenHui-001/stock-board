# 热点追踪模块核心逻辑重设计 — 架构方案（Bob/高见远）

## 1. 总体方案（逐项算法设计）

### P0-4 去重指纹加固（双条件合并，_merge 内部实现）
- 条件A（精确）：沿用现有 `_title_fp` 前 24 字 md5 指纹，相等即合并。
- 条件B（相似）：改写式标题兜底。相似度算法选 **字符 bigram Dice 系数**（不用编辑距离：O(L²)/对在大文本上浪费，Dice 对中文短标题区分度足够且 O(L)/对）。全量 O(n²) 在 200 条 = 2 万对可控，再加优化：按「首 8 字」分桶，仅桶内两两比较。
- 阈值：`HEAT_SIM_TH = 0.62`（偏保守，宁可漏合不误合）；设为 >1 即等效关闭条件B（回滚开关）。
- 合并保留 ts 最新，被合并条数记入 `item["dups"]`（仅记录，不进热度公式）。
- 伪代码：
```
merge(items):
  # 条件A
  best = {}  # fp -> item(ts最大, dups累计)
  for it: fp=_title_fp(title); 更新 best[fp]
  remain = [i for i in items if 未被A合并]
  # 条件B：按 first8(title) 分桶，桶内 Dice>=0.62 合并进 best 中相似项
  return sorted(按ts倒序)[:HOTSPOT_LIMIT]
```

### P0-1 标签匹配引擎（_extract_item_tags 重写）
- 选型：**最长/全量多模式子串匹配**，不引分词库（词典仅 50 概念 × 数关键词，纯 str.find 扫描，≤200 条毫秒级）。
- 词典结构改造（hotspot_ai.py）：`_SECTORS` 列表 → `_SECTOR_DICT: dict[str, dict]`：
  `{"keywords": [...], "parent": "新能源"|None, "generic": {"机器人": True, "AI": True, ...}}`，generic 短词仅在**标题命中**才计分（泛词门槛）。
- 标题权重：标题命中 ×3，摘要命中 ×1（`HEAT_TITLE_W=3`）。同一概念取标题+摘要双命中取大。
- 父子去重：命中子概念（光伏）时，父概念（新能源）本次不计数，除非父有独立核心词（非继承）命中。
- 标签级情绪（规则邻近词表）：命中词位置取前后 `HEAT_SENT_WINDOW=12` 字窗口，窗口内利好词 +1 / 利空词 -1；>0 利好、<0 利空、=0 或混合 → 中性。预留 LLM 扩展点：独立函数 `_tag_sentiment(title, summary, kw, ctx) -> str`，签名稳定，本期规则实现，未来可整函数替换为 LLM 批量判定。
- 输出结构：`{"name", "sentiment", "score", "hit", "src"}`。

### P0-2 + P0-3 + P1-3 发酵强度分与斜率趋势（_compute_sector_heat 重写）
```
fresh(t)   = 2 ** (-(now_ts - t) / HEAT_HALF_LIFE_S)      # 半衰期 600s(10min)
w_item     = tag.score × fresh × HEAT_EMO_W[sentiment]    # 情绪: 利好1.2/中性1.0/利空0.8
heat(S)    = Σ w_item  (窗口内)
heat_norm  = 100 × heat / max(heat over all sectors)      # 相对归一，抗绝对值漂移
slope      = 窗口均分 K=4 子窗，各子窗 Σw_item = w_k，
             对 log(w_k+1) 做最小二乘斜率，再除以全表最大|斜率|归一到 [-1,1]
trend      = slope>=0.15 → up; <=-0.15 → down; 否则 flat
rank_score = heat_norm × (1 + HEAT_SLOPE_W×slope)         # HEAT_SLOPE_W=0.5，置0退化为纯热度
排序       = rank_score 降序
```
信源权重 `HEAT_SOURCE_W`（dict，缺省 1.0）：财联社 1.3 / 华尔街见闻 1.2 / 新浪·东财 1.0 / 其余 0.8，乘进 w_item。全部常量集中 hotspot.py 顶部 `HEAT_*` 区。

### P1-1 相关股行情信号（hotspot_ai._resolve_stocks）
解析出真实代码后，批量取 provider 行情快照（点击触发 + 600s 缓存，不在 60s 热路径）：
`rank = kw_hits×2 + clamp(pct, -5, 10)×0.8 + clamp(vol_ratio, 0, 5)×0.6`；行情失败降级原「命中数+检索序」排序，字段缺省安全兜底。

### P1-2 梯队个股下钻
复用 `value_screener._fetch_board_flow`，在 value_screener 新增公开包装 `board_flow_leaders(limit=50) -> list[dict]`（内部做 "-" → None 清洗），异步拉取失败静默跳过。结果挂 `meta.leaders`：`[{board, code, name, pct_chg, main_net, leader_name, leader_code, leader_pct}]`。前端 chip 下钻按板块名与 leaders.board 做**包含匹配**，无匹配则隐藏下钻入口。

### P0-5 前端（只动视觉与布局，交互流程不变）
chips 按 trend 三组分区渲染（发酵/持平/退潮，数据来自 sector_heat[].trend）；热度条宽度用 heat_norm、附 slope 箭头与色阶（up 红 / flat 灰 / down 绿）；fresh 改三档（≤2min 亮 / ≤10min 中 / 更早 暗，按 ts 与 now 差）；移动端 ≤480px 断点收紧 chip 尺寸与两行布局。所有新 meta 字段用 `??` 兜底，兼容旧缓存。

## 2. 数据结构与接口变更

**schema（全部加字段、默认值兜底，前端需同步消费▲）**
- `meta.sector_heat[]`：新增 `heat:float`、`heat_norm:float(0-100)`、`slope:float(-1~1)`；保留 total/bull/bear/neutral/trend/recent/older（trend 语义改为斜率阈值）。▲前端 chips/热度条。
- `items[].tags[]`：新增 `score:float`、`hit:str`、`src:str`；`sentiment` 变为标签级情绪。▲前端可选展示。
- `items[]`：新增 `dups:int(默认0)`。▲可选显示。
- `meta.leaders: list`（P1-2，可为空数组）。▲前端下钻面板。
- schemas.py 走 `_AllowExtra` 松散基类，无需改 response_model 结构，仅补字段注释。

**函数签名（均不破坏调用方）**
- `_merge(items) -> list`（内部双条件）；`_title_fp` 保留；新增 `_title_similar(a,b)->float`。
- `_extract_item_tags(title, summary) -> list[dict]`（签名不变，返回加字段）。
- `_compute_sector_heat(items, minutes, now_ts) -> list[dict]`（签名不变）。
- hotspot_ai：`_SECTORS` → `_SECTOR_DICT` + 兼容适配 `_extract_keywords`；新增 `_tag_sentiment`；`_resolve_stocks` 内部加重排。
- value_screener：新增公开 `board_flow_leaders(limit=50)`；hotspot.py `_load` 增加一次调用并入 meta。

## 3. 文件列表与改动规模

| 文件 | 改动点 | 规模 |
|---|---|---|
| backend/hotspot.py | HEAT_* 常量区、_merge 双条件、_extract_item_tags 重写、_compute_sector_heat 重写、_load 接 leaders | ~200 行 |
| backend/hotspot_ai.py | _SECTOR_DICT 词典结构化（parent/generic 标注）、_tag_sentiment 邻近词表、_resolve_stocks 行情重排 | ~130 行 |
| backend/value_screener.py | board_flow_leaders 公开包装 | ~15 行 |
| backend/schemas.py、api.py | 字段注释，无结构变更 | ~10 行 |
| frontend/static/js/page-hotspot.js | chips 趋势分组、热度条 slope、fresh 三档、下钻面板 | +80/-20 |
| frontend/static/css/app.css | 分组配色、移动端断点 | ~60 行 |
| tests/test_hotspot.py | 更新受影响用例 + 新增（去重相似、泛词门槛、父子去重、衰减/斜率、leaders） | ~120 行 |
| scripts/frontend_smoke.mjs | chips 分组/热度条/下钻断言 | +8 断言 |

## 4. 任务列表（≤5，后端先行）

- **T01 热点核心算法**（P0）：backend/hotspot.py、backend/hotspot_ai.py、backend/utils.py（复用清洗工具）。依赖：无。验收：单测覆盖双条件去重/泛词门槛/父子去重/衰减斜率，纯内存计算单次 <50ms。
- **T02 相关股与下钻**（P1）：backend/hotspot_ai.py、backend/value_screener.py、backend/api.py。依赖：T01。验收：_resolve_stocks 引入行情信号且失败可降级；/api/hotspot meta.leaders 正常下发（含 "-" 清洗）。
- **T03 测试同步**：tests/test_hotspot.py、tests/test_helpers.py、tests/conftest.py。依赖：T01、T02。验收：pytest 全量 ≥117 passed 且新增用例通过。
- **T04 前端 UI**（P0-5）：frontend/static/js/page-hotspot.js、frontend/static/css/app.css、scripts/frontend_smoke.mjs。依赖：T01（schema）、T03。验收：chips 三组分区、斜率热度条、fresh 三档、移动端断点，交互流程不变。
- **T05 集成回归**：scripts/frontend_smoke.mjs、tests/ 全量、docs/system_design.md 回填。依赖：T04。验收：冒烟 25 项基线 + 新增断言全绿，pytest 全绿。

## 5. 共享知识

- 情绪枚举沿用 `"利好|利空|中性"`；趋势枚举沿用 `"up|flat|down"`，不新增取值。
- 常量前缀：hotspot.py 一律 `HEAT_`（HALF_LIFE_S=600、TITLE_W=3、SOURCE_W、EMO_W、SLOPE_K=4、SLOPE_W=0.5、TREND_TH=0.15、SIM_TH=0.62、SENT_WINDOW=12）；hotspot_ai.py 一律 `SECTOR_`。调参只改常量区。
- schema 只加不删不改名；新字段必须有默认值；前端消费全部 `??` 兜底。
- 新逻辑不新增任何外部接口调用进热度计算热路径（leaders 与相关股行情均在缓存/点击路径）。

## 6. 待明确事项（给主理人）

1. 信源权重表按常见 6 源给了默认值（财联社 1.3 / 华尔街见闻 1.2 / 新浪·东财 1.0 / 其余 0.8），工程师以 `_FEEDS` 实际源名为准，上线后回调。
2. leaders 下钻用「板块名包含匹配」，词典概念名与 push2 板块名差异大的 chip 将无下钻数据（前端隐藏入口），是否接受？
3. _resolve_stocks 引入行情信号意味着每次 analyze（点击触发）可能多一次行情批量调用（600s 缓存），默认做、失败降级——确认无异议。

## 7. 风险与回滚

1. 相似度合并误并系列报道 → 阈值 0.62 保守 + `dups` 可观测；回滚：`HEAT_SIM_TH>1` 关闭条件B。
2. 权重默认值效果待验证 → 全部集中常量区；`HEAT_SLOPE_W=0` 即退化为原纯热度排序，一键回退排序语义。
3. 前端消费新 meta 字段 → 全部 `??` 兜底，字段缺失自动回退旧渲染路径，接口回滚无前端故障。
