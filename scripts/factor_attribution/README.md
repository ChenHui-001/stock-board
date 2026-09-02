# 因子归因脚本集

生产环境 `backend/analysis/rule_engine.py` 的因子权重与阈值**全部来自本目录脚本的实证结论**。
此前这些脚本散落在 `tmp/`（已被 `.gitignore` 排除），意味着权重**没有可复现路径** —— 换一台机器即失传。
本目录将其纳入版本控制，并改造为路径无关（不再硬编码 `E:\project\股票看板`）。

## 运行方式

所有脚本路径自解析（`ROOT = Path(__file__).resolve().parents[2]`），**换机器、换盘符都不需要改代码**。

```bash
# 在项目根目录执行（依赖已按 requirements.txt 装好）
python scripts/factor_attribution/factor_attrib.py
```

产物统一写入同目录 `results/`，不污染 `tmp/`。

## 脚本与产物

| 脚本 | 作用 | 主要产物 |
|------|------|----------|
| `factor_attrib.py` | **入口脚本**。把 `tech_score` 拆成 8 个独立子因子，逐项算 fwd5 分组收益 / 胜率 / Spearman IC；再做相关矩阵（查共线）、方向冲突检验、剔除实验 | `factor_events.csv`*、`factor_corr.csv`、`factor_report.csv`、`combo_spread.csv`、`threshold_bucket.csv`、`yearly_spread.csv` |
| `factor_flip.py` | 决定性检验：把动量因子方向取反，看区分度是否改善（A/B/C/D/E 五组对照） | `flip_events.csv`*、`flip_spread.csv`、`flip_yearly.csv` |
| `factor_oos.py` | 样本外检验：训练期（2023-09~2024-12）定方向与去共线，测试期（2025-01 起）只套用不拟合 | `oos_ic_train.csv`、`oos_quintile.csv`、`oos_spread.csv`、`oos_yearly.csv` |
| `factor_stability.py` | IC 稳定性：按半年分期逐因子算 IC，量化符号翻转频率 —— 回答「样本内显著、样本外失效」的根因 | `ic_stability.csv` |
| `factor_final.py` | 最终检验：改用「方向一致性」（训练期 3/3 期 IC 同号）而非 IC 大小筛选因子，严格样本外验证 | `final_quintile.csv`、`final_spread.csv` |
| `optimize_v2.py` | 降权噪音因子 + 提升唯一 7/7 稳定为正的 MA20 乖离修正因子，验证四档分布是否均衡 | `opt_events.csv`*、`opt_quantile.csv`、`opt_threshold.csv` |
| `calibrate_threshold.py` | 阈值重新标定：因子降权后原 28/5/-22 失效（技术面量级 ±78 → 约 ±40）。训练期网格搜索，测试期只套用 | `calib_events.csv`*、`calib_candidates.csv`、`calib_test.csv` |
| `make_report.py` | 汇总出图：自包含 HTML 看板 + Markdown 报告，补算收益分布偏度与四档形态 | `因子归因报告.html`、`因子归因报告.md`、`report_dist.csv`、`report_ic.csv` |

\* 标记为**大体积原始事件表**（单文件 0.4~1.2MB），未入库。它们可由脚本重跑还原，
仓库只保留 22 个**结论级**产物（均已提交）。若需本地查看事件明细，跑一遍对应脚本即可。

## 依赖顺序

```
factor_attrib.py  ──┬─→ factor_flip.py ─→ factor_oos.py ─→ factor_stability.py ─→ factor_final.py
                    │                                                                    │
                    │                                          optimize_v2.py ───────────┤
                    │                                                                    │
                    └─→ make_report.py（读取 factor_events.csv，需先跑 factor_attrib.py）  │
                                                                 calibrate_threshold.py ←┘
```

单跑任意一个脚本不会崩（数据自带缓存），但结论会缺上下文；建议按上表顺序全跑一遍。

## 核心结论（生产权重的依据）

1. **趋势跟随类因子 IC 系统性为负**：20日涨跌 IC=-0.042、均线排列 -0.025、斜率 -0.018、突破 1/7 期为正。
2. **唯一 7/7 期稳定为正的是 MA20 乖离修正**（IC=+0.066），但原权重只有 ±4 分 → 已提升至 ±12。
3. **「均线三兄弟」高度共线**（站上 / 排列 / 斜率，Spearman 平均相关 0.715）→ 同一趋势信号被重复计入三次，已降权。
4. **反向也是过拟合**：样本外检验证明把动量反向后 t=-1.13，无效。对不确定性诚实的处理是**降权**而非反向。
5. **右偏陷阱**：加仓档均值 +0.379% 但中位数 −0.109% —— 少数极端盈利拉高均值，已据此收敛置信度与档位分布。

## 注意事项

- 评分口径与生产 `rule_engine.py` **完全一致**：脚本直接 import `FACTOR_WEIGHTS` / `_damp` / `_round_half_away`，不另写一份，避免口径漂移。
- 回测约定：信号在 bar `i` 收盘产生，bar `i+1` 开盘买入，持有 N 日后收盘卖出；基本面按 `pub_date <= signal_date` 过滤，无前视。
- 数据复用 `backend/backtest/engine` 的缓存（10 只 A 股 × 800 根前复权日线 + 财报）。
