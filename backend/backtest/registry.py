"""策略注册表：新策略只需实现 `run(params, on_progress)` 并在此登记。

`run` 契约：
    输入  params      —— 前端按 PARAMS_SCHEMA 渲染的表单值（dict）
          on_progress —— 回调 (pct: float 0~1, stage: str)，用于前端进度条
    输出  dict，键：
          summary      —— 事件级汇总（喂给看板 KPI 卡）
          full_summary —— 完整汇总（落盘 summary.json）
          tables       —— 前端要渲染的表格 dict
          trades_csv   —— 临时目录下的事件明细 CSV 路径（由 store 搬走）
          report_html  —— 临时目录下的看板 HTML 路径（由 store 搬走）
          meta         —— 看板抬头（策略名 / 标的 / 区间 / 备注）
"""
from __future__ import annotations

from typing import Any, Callable, Coroutine

from . import compare_strategy, intraday_strategy, score_strategy

Runner = Callable[[dict[str, Any], Callable[[float, str], None]],
                  Coroutine[Any, Any, dict[str, Any]]]


class Strategy:
    def __init__(
        self,
        strategy_id: str,
        name: str,
        desc: str,
        kind: str,
        runner: Runner,
        schema: list[dict[str, Any]],
        limits: str = "",
    ) -> None:
        self.id = strategy_id
        self.name = name
        self.desc = desc
        self.kind = kind              # event_study / strategy
        self.runner = runner
        self.schema = schema
        self.limits = limits          # 数据不可得等口径披露

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "desc": self.desc,
            "kind": self.kind,
            "schema": self.schema,
            "limits": self.limits,
        }


STRATEGIES: list[Strategy] = [
    Strategy(
        strategy_id=score_strategy.STRATEGY_ID,
        name=score_strategy.STRATEGY_NAME,
        desc=(
            "验证 AI 分析「四维评分 → 加仓/观望/减仓/清仓」的三道阈值是否真的有区分度。"
            "逐日复现生产规则引擎的技术面 + 基本面评分，信号日收盘分档，"
            "次日开盘买入、持有 1/3/5/10 日后收盘卖出，按档位统计收益与胜率；"
            "同时按分数四分位切档，检验评分本身（而非阈值切点）是否有区分度。"
        ),
        kind="event_study",
        runner=score_strategy.run,
        schema=score_strategy.PARAMS_SCHEMA,
        limits=(
            "资金面与消息面历史数据不可得（接口仅当日快照 / 无法无偏重构），"
            "回测版评分 = 技术面 + 基本面，两维置零。"
        ),
    ),
    Strategy(
        strategy_id=intraday_strategy.STRATEGY_ID,
        name=intraday_strategy.STRATEGY_NAME,
        desc=(
            "直接调用生产代码 _intraday_score 逐日打分，统计每个盘口信号"
            "（盘中位置×涨跌、量比、振幅、换手）触发后次日涨跌的方向命中率，"
            "与「全样本次日上涨率」基线对比，给出权重校准建议。"
        ),
        kind="event_study",
        runner=intraday_strategy.run,
        schema=intraday_strategy.PARAMS_SCHEMA,
        limits="用日线近似盘中快照（收盘时点），生产环境是盘中实时信号，存在时点偏差。",
    ),
    Strategy(
        strategy_id=compare_strategy.STRATEGY_ID,
        name=compare_strategy.STRATEGY_NAME,
        desc=(
            "用真实 5 分钟线构造盘中 14:00 快照，与同一交易日的收盘快照对比："
            "打分方向翻转率、各信号在两个时点的命中率差异、是否需要盘中回调权重。"
            "分钟线优先东财，限流回退腾讯翻页。"
        ),
        kind="event_study",
        runner=compare_strategy.run,
        schema=compare_strategy.PARAMS_SCHEMA,
        limits="分钟线历史长度受限（东财单只约 21 个交易日），各信号盘中样本通常远少于日线回测。",
    ),
]

_BY_ID: dict[str, Strategy] = {s.id: s for s in STRATEGIES}


def get(strategy_id: str) -> Strategy | None:
    return _BY_ID.get(strategy_id)


def all_strategies() -> list[dict[str, Any]]:
    return [s.to_dict() for s in STRATEGIES]
