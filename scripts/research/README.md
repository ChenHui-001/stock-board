# 归因与标定研究脚本

本目录存放因子权重归因 / 标定 / 优化的完整经验轨迹脚本（原位于 `tmp/`，因 `tmp/` 被
gitignore 而丢失版本控制，2026-09 按 P0-5 迁移入库）。

这些脚本共同构成 `backend/config.py` 中 `FACTOR_WEIGHTS`（以及阈值标定参数）的
可复现性依据：任何人都能按下面的顺序重跑，得到与当前线上权重一致的结论。

## 运行顺序（pipeline）

| 顺序 | 脚本 | 作用 |
|---|---|---|
| 1 | `factor_attrib.py` | 单因子归因：逐因子拆解收益贡献 |
| 2 | `factor_stability.py` | 因子稳定性检验（跨期 IC / 分箱单调性） |
| 3 | `factor_oos.py` | 样本外（OOS）验证，防过拟合 |
| 4 | `factor_final.py` | 汇总前三步，产出最终因子权重建议 |
| 5 | `factor_flip.py` | 因子方向翻转检验（确认正负号约定） |
| 6 | `calibrate_threshold.py` | 触发阈值标定 |
| 7 | `optimize_v2.py` | 权重组合优化（v2 迭代） |
| 8 | `make_report.py` | 生成归因报告（Markdown） |

脚本间通过 `data/` 下的中间产物衔接，重复运行是幂等的（覆盖式输出）。

## 运行方式

```bash
# 需要依赖 requirements.txt（numpy/pandas 等），并显式设置 PYTHONPATH
PYTHONIOENCODING=utf-8 PYTHONPATH="<site-packages路径>" \
  python scripts/research/factor_attrib.py
```

所有脚本均以 `ROOT = Path(__file__).resolve().parents[2]` 动态推导仓库根，
不再依赖硬编码的本地绝对路径，可在任何 checkout 目录下直接运行。

## 注意

- 脚本为研究用途，不参与线上服务；修改权重前应按顺序重跑并核对结论。
- 输出报告的快照请勿提交到本目录（避免仓库膨胀），归档到本地即可。
