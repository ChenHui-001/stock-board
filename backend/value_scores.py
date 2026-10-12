"""价值投资评分引擎（从 value_screener.py 拆出，体检 M7）。

纯计算模块：输入单股 profile dict，输出各维度评分/信号/建议。
依赖仅 valuecfg（维度满分/总分常量）——不碰网络、缓存、registry，
因此可独立测试。函数名与拆分前完全一致，value_screener 统一 re-export。
"""
from __future__ import annotations

from typing import Any

from . import valuecfg

# ------------------------------------------------------------------ 评分引擎

# ------------------------------------------------------------------ 行业相对估值（P1 修复）

# A 股行业间 PE 中位数差异极大（银行 5~8x vs 半导体 40~80x），PE 绝对档位
# 会把高研发行业的成长股全判「高估」、给低增长行业虚发「低估」信号。
# 方案：按板块名/股票名关键词匹配行业，PE 有效值 = PE / 容差系数 后再套
# 绝对档位——系数 >1 给成长溢价（阈值放宽），<1 收紧（低增长行业的低 PE
# 更接近真实低估）。PEG 本身已按增速归一、PB 行业差异相对小，均不做调整。
_INDUSTRY_PE_TOLERANCE: tuple[tuple[tuple[str, ...], float], ...] = (
    (("半导", "芯片", "集成电路", "消费电子", "元件", "PCB"), 1.8),
    (("软件", "云计算", "人工智能", "信创", "计算机", "数据"), 1.8),
    (("生物", "医药", "医疗", "疫苗", "创新药"), 1.6),
    (("新能源", "锂电", "光伏", "储能", "电力设备", "风电"), 1.5),
    (("军工", "航天", "国防"), 1.4),
    (("银行", "保险", "房地产", "煤炭", "钢铁", "水泥",
      "公用事业", "公路", "港口", "高速公路"), 0.8),
)
_INDUSTRY_PE_TOLERANCE_DEFAULT = 1.0


def _industry_pe_tolerance(board: str = "", name: str = "") -> float:
    """行业 PE 容差系数：board（东财板块名）优先，股票名关键词兜底。

    蓝筹基准池没有板块名（board=""），但「工商银行」「中国人保」等名称
    自带行业词；无法识别的行业回落 1.0（与旧行为一致，不奖不罚）。
    """
    text = f"{board or ''}|{name or ''}"
    for keys, tol in _INDUSTRY_PE_TOLERANCE:
        if any(k in text for k in keys):
            return tol
    return _INDUSTRY_PE_TOLERANCE_DEFAULT


def _financial_score(
    profile_or_fin: "dict[str, Any] | list[dict[str, Any]]",
    fin: "list[dict[str, Any]] | None" = None,
    board_strength: "dict[str, float] | None" = None,
) -> dict[str, Any]:
    """基本面评分（满分 50）：成长 12 + 质量 10 + 估值 18 + 现金流 4 + 行业 6。

    新签名支持估值(PE+PB+PEG)与行业景气评估。旧调用方式
    `_financial_score(fin_list)` 兼容：把 list 当作 fin、profile 视作空。
    """
    # 向后兼容:第一个位置参数可以是 list（旧 API）或 dict（新 API）
    if isinstance(profile_or_fin, list):
        fin = profile_or_fin
        profile: dict[str, Any] = {}
    else:
        profile = profile_or_fin
        if fin is None:
            fin = profile.get("financials") or []
    board_strength = board_strength or {}

    if not fin:
        return {
            "score": 0,
            "detail": "财务数据缺失",
            "completeness": 0,
            "value_metrics": {"pe_band": None, "pb_band": None,
                              "peg_band": None, "ocf_band": None,
                              "industry_band": None, "pe_tol": None},
        }
    latest = fin[0]
    has_valuation = profile.get("pe") is not None or profile.get("pb") is not None

    # -------- 成长 12:净利润/营收同比 + 加速趋势 + 扣非交叉验证
    metrics: dict[str, Any] = {}
    growth_pts = 0
    np_yoys = [f.get("net_profit_yoy") for f in fin[:4] if f.get("net_profit_yoy") is not None]
    rev_yoys = [f.get("revenue_yoy") for f in fin[:4] if f.get("revenue_yoy") is not None]
    if np_yoys:
        latest_np = np_yoys[0]
        if latest_np > 30:
            np_pts = 6
        elif latest_np > 10:
            np_pts = 4
        elif latest_np > 0:
            np_pts = 2
        elif latest_np > -10:
            np_pts = -1
        else:
            np_pts = -3
        if len(np_yoys) >= 2 and np_yoys[0] > np_yoys[1]:
            np_pts += 3  # 加速
        # P2 扣非交叉验证：归母高增但扣非跟不上 → 盈利质量打折。
        # 典型暴雷前兆：卖资产/政府补助/公允价值变动撑起的「高增长」
        ded_yoy = latest.get("net_profit_deduct_yoy")
        if latest_np > 30 and ded_yoy is not None:
            if ded_yoy < 0:
                np_pts = min(np_pts, 0)
                metrics["profit_quality"] = "扣非同比为负，归母高增主要靠非经常性损益"
            elif ded_yoy < 10:
                np_pts = min(np_pts, 2)
                metrics["profit_quality"] = "扣非增速显著低于归母，利润含非经常性损益"
        growth_pts += np_pts
    if rev_yoys:
        latest_rev = rev_yoys[0]
        if latest_rev > 20:
            growth_pts += 3
        elif latest_rev > 5:
            growth_pts += 2
        elif latest_rev > 0:
            growth_pts += 1
        else:
            growth_pts -= 2
    growth_pts = max(0, min(12, growth_pts))

    # -------- 质量 10:ROE / 毛利率 / 负债率
    quality_pts = 0
    roe = latest.get("roe")
    if roe is not None:
        if roe > 15:
            quality_pts += 4
        elif roe > 8:
            quality_pts += 2
        elif roe > 0:
            quality_pts += 1
        else:
            quality_pts -= 1
    gm = latest.get("gross_margin")
    if gm is not None:
        if gm > 40:
            quality_pts += 3
        elif gm > 20:
            quality_pts += 2
        elif gm > 0:
            quality_pts += 1
    dr = latest.get("debt_ratio")
    if dr is not None:
        if dr < 40:
            quality_pts += 3
        elif dr < 60:
            quality_pts += 1
        elif dr > 75:
            quality_pts -= 2

    # P2 财务深度：年报 ROE 趋势（近 5 个完整年度，衡量盈利能力的持续性与方向）
    fy_roes: list[float] = []
    for f in fin:
        p = str(f.get("period") or "")
        if p.endswith("FY") and f.get("roe") is not None:
            fy_roes.append(f["roe"])
        if len(fy_roes) >= 5:
            break
    if len(fy_roes) >= 3:
        if all(v > 10 for v in fy_roes):
            quality_pts += 2
            metrics["roe_trend"] = f"近{len(fy_roes)}年 ROE 均>10%"
        elif len(fy_roes) >= 4 and fy_roes[0] < fy_roes[1] < fy_roes[2]:
            quality_pts -= 2
            metrics["roe_trend"] = "ROE 连续下滑"
        else:
            metrics["roe_trend"] = "平稳"
    quality_pts = max(0, min(10, quality_pts))

    # -------- 估值 18:价值投资核心维度（PE + PB + PEG）----
    # 数据缺失时给中性分（不奖不罚），避免因数据缺失而把整个价值维度清零。
    # 旧实现是 PE/PB 任一缺失 → 该子项 0 分,导致价值维度永远 < 6 分,
    # 同时 VALUE_BUY 走不到(pe_band=None 不在低估档位),与页面"价值投资"定位冲突。
    value_pts = 0
    pe = profile.get("pe")
    if pe is not None and pe > 0:
        # 行业相对估值：PE 换算成行业调整后的有效值再套档位（见 _INDUSTRY_PE_TOLERANCE）
        pe_tol = _industry_pe_tolerance(profile.get("board") or "",
                                        profile.get("name") or "")
        metrics["pe_tol"] = pe_tol
        pe_eff = pe / pe_tol
        if pe_eff < 15:
            metrics["pe_band"] = "深度低估"
            value_pts += 10
        elif pe_eff < 25:
            metrics["pe_band"] = "低估"
            value_pts += 8
        elif pe_eff < 40:
            metrics["pe_band"] = "合理"
            value_pts += 5
        elif pe_eff < 80:
            metrics["pe_band"] = "偏高"
            value_pts += 2
        else:
            metrics["pe_band"] = "高估"
        # PEG = PE / 净利同比。增速<=0 或 PE<=0 时无意义
        np_yoy_latest = latest.get("net_profit_yoy")
        if np_yoy_latest is not None and np_yoy_latest > 0:
            peg = pe / np_yoy_latest
            if peg < 0.5:
                value_pts += 4
                metrics["peg_band"] = "极低估"
            elif peg < 1:
                value_pts += 3
                metrics["peg_band"] = "低估"
            elif peg < 2:
                value_pts += 2
                metrics["peg_band"] = "合理"
            elif peg < 3:
                value_pts += 1
                metrics["peg_band"] = "偏高"
            else:
                metrics["peg_band"] = "高估"
        else:
            metrics["peg_band"] = "增速缺失"
    elif pe is not None and pe <= 0:
        # 亏损公司 PE 失效,给 2/18 中性偏负分(避免与正利润公司同等)
        value_pts += 2
        metrics["pe_band"] = "亏损"
    else:
        # PE 数据缺失(腾讯接口偶发空字段)→ 5/18 中性分
        # 不奖不罚,仅阻止整个价值维度被清零
        value_pts += 5
        metrics["pe_band"] = "数据缺失"

    pb = profile.get("pb")
    if pb is not None and pb > 0:
        if pb <= 1:
            metrics["pb_band"] = "深度低估"
            value_pts += 4
        elif pb < 1.5:
            metrics["pb_band"] = "低估"
            value_pts += 3
        elif pb < 3:
            metrics["pb_band"] = "合理"
            value_pts += 2
        elif pb < 6:
            metrics["pb_band"] = "偏高"
            value_pts += 1
        else:
            metrics["pb_band"] = "高估"
    else:
        # PB 数据缺失 → 1/18 中性分(PB 信息含量低于 PE)
        value_pts += 1
        metrics["pb_band"] = "数据缺失"

    # 连续正增长 + 估值合理/低估：再给点奖励（基本面+价值双正向）
    np_trend = [f.get("net_profit_yoy") for f in fin[:3] if f.get("net_profit_yoy") is not None]
    if np_trend and all(x > 0 for x in np_trend) and metrics.get("pe_band") in ("深度低估", "低估", "合理"):
        value_pts += 1
    value_pts = max(0, min(18, value_pts))

    # -------- 现金流 4:OCF/净利润（数据缺失时记 0,不扣分）
    ocf_pts = 0
    ocf_to_np = latest.get("ocf_to_netprofit")
    if ocf_to_np is not None:
        if ocf_to_np >= 1.0:
            ocf_pts = 4
            metrics["ocf_band"] = "优秀"
        elif ocf_to_np >= 0.5:
            ocf_pts = 3
            metrics["ocf_band"] = "健康"
        elif ocf_to_np >= 0:
            ocf_pts = 2
            metrics["ocf_band"] = "偏弱"
        else:
            metrics["ocf_band"] = "恶化"
    else:
        metrics["ocf_band"] = None

    # -------- 行业 6:板块强度
    industry_pts = 0
    b = profile.get("board") or ""
    strength = board_strength.get(b) if b else None
    if strength is not None:
        industry_pts = max(0, min(6, round(strength * 6)))
        metrics["industry_band"] = f"{strength:.2f}"
    else:
        metrics["industry_band"] = None

    total = growth_pts + quality_pts + value_pts + ocf_pts + industry_pts
    total = round(min(50, total), 1)
    detail = (f"成长{growth_pts}/12 质量{quality_pts}/10 估值{value_pts}/18 "
              f"现金流{ocf_pts}/4 行业{industry_pts}/6")
    completeness = 1 if (fin and has_valuation) else 0
    return {
        "score": total,
        "detail": detail,
        "completeness": completeness,
        "value_metrics": metrics,
    }


def _board_score(
    profile: dict[str, Any], board_strength: dict[str, float],
    board_flow: "dict[str, dict[str, Any]] | None" = None,
) -> dict[str, Any]:
    """板块评分（满分 10）：市场强度 0~7 + 板块主力资金流 0~3。

    资金流来自东财板块资金流榜（push2delay 实测）：今日主力净流入 ≥5 亿 +3 /
    ≥2 亿 +2 / >0 +1 / ≤0 不加；今日与 5 日双正为「资金持续性确认」再 +0.5
    （上限 3）。资金数据缺失时不奖不罚，detail 如实标注。
    """
    b = profile.get("board") or ""
    if not b:
        # 盘前/候选缺板块字段属数据缺失：按中性 5/10 计，不罚分
        # （此前给 0 分，叠加阈值固定占比导致盘前全市场数学性 AVOID）
        return {"score": 5, "detail": "板块未知【数据缺失】按中性计", "completeness": 0}
    strength = board_strength.get(b)
    if strength is None:
        return {"score": 5, "detail": f"板块「{b}」无强度数据【数据缺失】按中性计",
                "completeness": 0}
    pts = max(0.0, min(7.0, strength * 7))
    detail = f"板块强度 {strength:.2f}"
    fl = (board_flow or {}).get(b) or {}
    mt = fl.get("main_today")
    if mt is not None:
        if mt >= 5e8:
            fund_pts = 3.0
        elif mt >= 2e8:
            fund_pts = 2.0
        elif mt > 0:
            fund_pts = 1.0
        else:
            fund_pts = 0.0
        if fund_pts > 0 and (fl.get("main_5d") or 0) > 0:
            fund_pts = min(3.0, fund_pts + 0.5)  # 今日与 5 日双正 → 持续性确认
        detail += f" · 板块主净{mt / 1e8:.1f}亿"
    else:
        fund_pts = 1.5   # 板块资金【数据缺失】按子项满分 3 的中性计，不罚分
        detail += " · 板块资金【数据缺失】按中性计"
    score = max(0.0, min(10.0, round(pts + fund_pts, 1)))
    return {"score": score, "detail": detail, "completeness": 1}


def _flow_score(profile: dict[str, Any]) -> dict[str, Any]:
    """资金评分（满分 12）：近 5 日主力资金拐点。

    重点找「30 日流出 → 20 日流出 → 10 日企稳 → 5/3/1 日流入」的资金拐点，
    禁止只看 30 日累计。

    资金信号强度按「流通市值占比」归一化：5 亿流入对 100 亿市值是 5%（强信号），
    对 1000 亿市值是 0.5%（弱信号）。有市值数据时按占比打分；市值缺失时退回
    绝对额（保持向后兼容）。
    """
    flow = profile.get("flow") or []
    if len(flow) < 2:
        # 盘前/接口全空属数据缺失：按中性 6/12 计，不罚分
        # （此前给 0 分，叠加阈值固定占比导致盘前全市场数学性 AVOID）
        return {"score": 6, "detail": "资金【数据缺失】按中性计", "completeness": 0}
    last5 = flow[-5:]
    main_1 = sum(d["main"] for d in last5[-1:])
    main_3 = sum(d["main"] for d in last5[-3:])
    main_5 = sum(d["main"] for d in last5)
    main_30 = sum(d["main"] for d in flow)

    pts = 0
    total_mv = profile.get("total_mv")  # 总市值（亿）
    # 按市值占比打分（小数,如 0.05 = 5%）
    if total_mv and total_mv > 0:
        r1 = main_1 / (total_mv * 1e8)
        r3 = main_3 / (total_mv * 1e8)
        r5 = main_5 / (total_mv * 1e8)
        r30 = main_30 / (total_mv * 1e8)
        # 阈值：1 日 ±0.1%、3 日 ±0.3%、5 日 0.5%、30 日 1%
        if r1 > 0.001: pts += 3
        elif r1 < -0.001: pts -= 2
        if r3 > 0.003: pts += 3
        elif r3 < -0.003: pts -= 2
        if r5 > 0.005: pts += 2
        if r30 > 0.01: pts += 1
        # 资金拐点：30 日占比流出但 5 日占比流入 → 额外加分
        if r30 < -0.005 and r5 > 0.003:
            pts += 3
        detail = (
            f"1日{r1*100:+.2f}% 3日{r3*100:+.2f}% 5日{r5*100:+.2f}% "
            f"30日{r30*100:+.2f}%(占市值{int(total_mv)}亿)"
        )
    else:
        # 市值缺失 → 退回绝对额打分(兼容老数据)
        if main_1 > 0: pts += 3
        elif main_1 < 0: pts -= 2
        if main_3 > 0: pts += 3
        elif main_3 < 0: pts -= 2
        if main_5 > 0: pts += 2
        if main_30 > 0: pts += 1
        if main_30 < 0 and main_5 > 0:
            pts += 3
        detail = (
            f"1日{main_1/1e8:.1f}亿 3日{main_3/1e8:.1f}亿 "
            f"5日{main_5/1e8:.1f}亿 30日{main_30/1e8:.1f}亿"
        )
    # 盘中实时主净确认（push2delay f62，延迟约 1 分钟）：
    #   实时与日级双正 → 资金流入确认 +1.5；实时正但日级负 → 盘中回流 +0.5；
    #   实时负但日级正 → 盘中转流出预警 -1.5（日内趋势逆转信号）；双负不加不减
    rt = profile.get("realtime") or {}
    rtm = rt.get("main_net_inflow")
    if rtm is not None:
        day_main = main_1
        if rtm > 0 and day_main > 0:
            pts += 1.5
            detail += f" · 实时主净{rtm/1e8:.1f}亿确认流入"
        elif rtm > 0 and day_main <= 0:
            pts += 0.5
            detail += f" · 实时主净{rtm/1e8:.1f}亿盘中回流"
        elif rtm < 0 and day_main > 0:
            pts -= 1.5
            detail += f" · 实时主净{rtm/1e8:.1f}亿盘中转流出⚠"
        else:
            detail += f" · 实时主净{rtm/1e8:.1f}亿"
    pts = max(0, min(12, pts))
    return {"score": pts, "detail": detail, "completeness": 1}


def _volume_score(profile: dict[str, Any]) -> dict[str, Any]:
    """量价筹码评分（满分 8）：量比/换手/涨跌幅/相对强度。"""
    pts = 0
    change = profile.get("change_pct")
    if change is not None:
        if change > 5: pts += 3
        elif change > 2: pts += 2
        elif change > 0: pts += 1
        elif change < -3: pts -= 2
    vr = profile.get("volume_ratio")
    if vr is not None:
        if vr > 1.5: pts += 2   # 放量
        elif vr > 1.0: pts += 1
        elif vr < 0.6: pts -= 1  # 缩量
    to = profile.get("turnover")
    if to is not None:
        if 2 <= to <= 15: pts += 1  # 活跃但不疯狂
        elif to > 25: pts -= 1      # 换手过猛
    # 涨停池内的候选本身带强势属性
    if profile.get("lianban"):
        pts += 2
    pts = max(0, min(8, pts))
    return {"score": pts, "detail": f"涨跌{change}% 量比{vr} 换手{to}%", "completeness": 1}


def _emotion_score(profile: dict[str, Any]) -> dict[str, Any]:
    """情绪妖股评分（满分 12）：连板高度 + 换手活跃度 + 涨停属性。

    连板 >= 6 视为「高位接盘」,与 _risk_score(+15)和 _buy_score(-8) 同向:
    不再奖励情绪溢价(给 0 而非 +6),避免三处评分互相打架。
    """
    pts = 0
    lb = profile.get("lianban") or 0
    if lb >= 6: pts += 0      # 高位接盘,不奖分(与风险/买点评分方向一致)
    elif lb >= 5: pts += 6
    elif lb >= 3: pts += 5
    elif lb >= 2: pts += 4
    elif lb >= 1: pts += 3
    to = profile.get("turnover")
    if to is not None:
        if 8 <= to <= 20: pts += 4   # 高换手
        elif 3 <= to < 8: pts += 2
        elif to > 25: pts += 1       # 过热但仍是焦点
    chg = profile.get("change_pct")
    # 连板 >= 6 时,涨幅加分也要打折(避免高位接盘再叠加情绪溢价)
    if chg is not None and chg > 5:
        pts += 2 if lb < 6 else 0
    pts = max(0, min(12, pts))
    note = "高位接盘" if lb >= 6 else f"连板{lb}"
    return {"score": pts, "detail": f"{note} 换手{to}%", "completeness": 1}


def _relative_score(profile: dict[str, Any]) -> dict[str, Any]:
    """个股相对板块强度（满分 8）：个股涨幅 − 所属板块平均涨幅。

    兑现策略文档中「个股强于板块」的承诺：同一热门板块里，跑赢板块均值的
    才是资金真正主攻的标的；跑输的往往是跟风补涨。
    """
    chg = profile.get("change_pct")
    board_avg = profile.get("board_avg_chg")
    board = profile.get("board") or ""
    if chg is None or board_avg is None:
        return {"score": 4, "detail": "板块强度数据缺失", "completeness": 0}
    diff = chg - board_avg
    if diff >= 5:
        pts = 8
    elif diff >= 3:
        pts = 7
    elif diff >= 1.5:
        pts = 6
    elif diff >= 0.5:
        pts = 5
    elif diff >= -1:
        pts = 4
    elif diff >= -3:
        pts = 2
    elif diff >= -6:
        pts = 1
    else:
        pts = 0
    return {
        "score": pts,
        "detail": f"相对板块 {diff:+.2f}%（个股 {chg:+.2f}% / 板块 {board_avg:+.2f}%）",
        "completeness": 1,
        "relative_chg": round(diff, 2),
    }


def _position_score(profile: dict[str, Any]) -> dict[str, Any]:
    """价格位置（满分 6）：当前价在近期高低区间中的百分位。

    自适应窗口:有 60 日数据用 60 日(对牛市中位 80% 仍判"高位"更准),否则
    回退 20 日。位置低（0-30%）= 回踩充分、上行空间大 → 高分；位置高
    (80-100%) = 接近阶段高点、追高风险 → 低分。中间位置给中性分,配合
    量价与资金维度共同决定买点。
    """
    kline = profile.get("kline") or []
    if len(kline) < 5:
        return {"score": 3, "detail": "K线数据不足", "completeness": 0}
    # 自适应窗口:60 日优先(更稳健),不足时回退 20 日
    if len(kline) >= 30:
        window = kline[-60:]
        win_label = "60日"
    else:
        window = kline[-20:]
        win_label = "20日"
    highs = [b.get("high") or b.get("close") for b in window if b.get("high") or b.get("close")]
    lows = [b.get("low") or b.get("close") for b in window if b.get("low") or b.get("close")]
    price = profile.get("price")
    if not highs or not lows or not price:
        return {"score": 3, "detail": "价格/区间缺失", "completeness": 0}
    hi, lo = max(highs), min(lows)
    # 关键：现价要参与区间计算。K 线最后一根常不含当日（数据源延迟/频控回落），
    # 而现价是实时的——涨停股现价会冲到 20 日区间之外，算出不合法的 >100% 位置。
    # 用现价扩展区间：创新高即 100%（最高位），破新低即 0%。
    hi = max(hi, price)
    lo = min(lo, price)
    if hi <= lo:
        return {"score": 3, "detail": "区间过窄", "completeness": 0}
    pos = max(0.0, min(100.0, (price - lo) / (hi - lo) * 100))  # 0=最低 100=最高
    if pos <= 20:
        pts = 6
    elif pos <= 40:
        pts = 5
    elif pos <= 60:
        pts = 4
    elif pos <= 80:
        pts = 2
    else:
        pts = 1
    return {
        "score": pts,
        "detail": f"{win_label}位置 {pos:.0f}%（{lo:.2f}~{hi:.2f}）",
        "completeness": 1,
        "position_pct": round(pos, 1),
        "position_window": win_label,
    }


def _risk_score(profile: dict[str, Any], fin_score: dict[str, Any]) -> dict[str, Any]:
    """风险评分（0-100，>60 禁入核心池）与风险扣分。"""
    fin = profile.get("financials") or []
    risk = 0
    notes: list[str] = []
    name = profile.get("name") or ""
    if "ST" in name.upper() or "*ST" in name.upper():
        risk += 40
        notes.append("ST/*ST")
    if not fin:
        risk += 10
        notes.append("财务数据缺失")
    else:
        latest = fin[0]
        np_yoy = latest.get("net_profit_yoy")
        if np_yoy is not None and np_yoy < -30:
            risk += 20
            notes.append(f"净利同比 {np_yoy:.0f}%")
        dr = latest.get("debt_ratio")
        if dr is not None and dr > 80:
            risk += 15
            notes.append(f"负债率 {dr:.0f}%")
        if latest.get("net_profit") is not None and latest["net_profit"] < 0:
            risk += 15
            notes.append("亏损")
    # 估值风险：PE / PB / PEG 异常
    pe = profile.get("pe")
    if pe is not None:
        if pe >= 200:
            risk += 15
            notes.append(f"PE {pe:.0f} 严重高估")
        elif pe > 100:
            risk += 10
            notes.append(f"PE {pe:.0f} 高估")
        elif pe > 80:
            risk += 5
            notes.append(f"PE {pe:.0f} 偏高")
        elif pe < 0:
            risk += 5
            notes.append("PE 亏损")
    pb = profile.get("pb")
    if pb is not None and pb > 10:
        risk += 8
        notes.append(f"PB {pb:.1f} 偏高")
    # OCF 持续恶化（若数据可得）
    fin = profile.get("financials") or []
    if len(fin) >= 2:
        ocf_curr = fin[0].get("ocf_to_netprofit")
        ocf_prev = fin[1].get("ocf_to_netprofit")
        if ocf_curr is not None and ocf_curr < 0:
            risk += 5
            notes.append("OCF 转负")
        if ocf_curr is not None and ocf_prev is not None and ocf_curr < -0.5 and ocf_prev < -0.5:
            risk += 5
            notes.append("OCF 连续恶化")
    # 涨幅过大追高风险
    chg = profile.get("change_pct")
    if chg is not None and chg > 9.5:
        risk += 10
        notes.append("接近涨停追高")
    # 连板过高接力风险
    if (profile.get("lianban") or 0) >= 6:
        risk += 15
        notes.append(f"{profile['lianban']} 连板高位")
    # ---- P2 暴雷过滤：商誉 / 股权质押 / 限售解禁（东财 datacenter 接口，均已实测可用）。
    # 审计意见：东财各 reportName 均「报表配置不存在」（数据源不可用），按项目原则
    # 不接入、不编造，待找到可用源后再补。数据缺失时对应项静默跳过（不奖不罚）。
    rx = profile.get("risk_extras") or {}
    gwr = rx.get("goodwill_ratio")  # 商誉 / 归母净资产（小数）
    if gwr is not None:
        if gwr > 0.3:
            risk += 15
            notes.append(f"商誉占净资产 {gwr * 100:.0f}%，减值暴雷高危")
        elif gwr > 0.15:
            risk += 10
            notes.append(f"商誉占净资产 {gwr * 100:.0f}%，减值风险高")
        elif gwr > 0.08:
            risk += 5
            notes.append(f"商誉占净资产 {gwr * 100:.0f}%")
    plg = rx.get("pledge_ratio")  # 大股东质押占总股本（%）。
    # 仅在 >0 时计分：质押登记数据止于清零年，旧零值视为已解押而非陈旧数据
    if plg is not None and plg > 0:
        if plg > 50:
            risk += 15
            notes.append(f"股权质押 {plg:.0f}%，平仓风险高")
        elif plg > 30:
            risk += 10
            notes.append(f"股权质押 {plg:.0f}%")
        elif plg > 15:
            risk += 5
            notes.append(f"股权质押 {plg:.0f}%")
    lift_cap = rx.get("lift_market_cap")  # 未来 90 日解禁市值合计（元）
    lift_days = rx.get("lift_days")       # 最近一次解禁距今天数
    total_mv = profile.get("total_mv")    # 总市值（亿）
    if lift_cap and total_mv and lift_days is not None and lift_days <= 90:
        lift_ratio = lift_cap / (total_mv * 1e8)
        if lift_ratio > 0.05:
            risk += 15 if lift_days <= 30 else 10
            notes.append(f"{lift_days}日后解禁占市值 {lift_ratio * 100:.1f}%")
        elif lift_ratio > 0.03:
            risk += 8
            notes.append(f"{lift_days}日后解禁占市值 {lift_ratio * 100:.1f}%")
        elif lift_ratio > 0.01:
            risk += 5
            notes.append(f"{lift_days}日后解禁占市值 {lift_ratio * 100:.1f}%")
    risk = max(0, min(100, risk))
    return {"score": risk, "notes": notes, "completeness": 1}


def _buy_score(profile: dict[str, Any], scores: dict[str, Any]) -> dict[str, Any]:
    """买点评分（0-100）：价格位置 + 量价 + 资金 + 板块 + 情绪的综合入场时机。"""
    pts = 0.0
    # 量价健康
    vr = profile.get("volume_ratio")
    chg = profile.get("change_pct")
    if vr is not None and vr >= 1.5 and chg is not None and chg > 0:
        pts += 20  # 放量上涨
    elif chg is not None and chg > 5:
        pts += 12
    elif chg is not None and chg < -3:
        pts -= 15  # 下跌不买
    # 资金
    flow = profile.get("flow") or []
    if len(flow) >= 3:
        main_3 = sum(d["main"] for d in flow[-3:])
        if main_3 > 0: pts += 20
        elif main_3 < 0: pts -= 10
    # 板块
    board_pts = scores["board"]["score"] if scores.get("board") else 0
    pts += board_pts * 4  # 满分 10 -> 40
    # 情绪（连板梯队）
    lb = profile.get("lianban") or 0
    if 1 <= lb <= 3: pts += 12   # 启动/发酵期最优
    elif 4 <= lb <= 5: pts += 6
    elif lb >= 6: pts -= 8       # 高位接力风险
    # 价格位置：低位回踩加分、接近阶段高点减分（避免追高）
    pos = (scores.get("position") or {}).get("position_pct")
    if pos is not None:
        if pos <= 25: pts += 8      # 回踩充分，上行空间大
        elif pos <= 40: pts += 4
        elif pos >= 90: pts -= 12   # 20 日高点附近，追高风险大
        elif pos >= 75: pts -= 6
    # 相对板块强度：跑赢板块均值的才是资金主攻标的
    rel = (scores.get("relative") or {}).get("relative_chg")
    if rel is not None:
        if rel >= 3: pts += 8
        elif rel >= 1: pts += 4
        elif rel <= -3: pts -= 8
    # 估值时机：低估值是价值投资的最佳买点（资金面+技术面的补充维度）
    fin_score = scores.get("finance") or {}
    vm = fin_score.get("value_metrics") or {}
    pe_band = vm.get("pe_band")
    peg_band = vm.get("peg_band")
    if pe_band in ("深度低估", "低估"):
        pts += 12   # 估值底 + 资金/量价配合 → 黄金坑买点
    elif pe_band == "合理":
        pts += 4    # 估值合理即可介入
    elif pe_band == "偏高":
        pts -= 6    # 不再便宜,资金/量价需要非常强才考虑
    elif pe_band == "高估":
        pts -= 14   # 高估区,即使其它维度亮眼也要克制
    # PEG 极低估叠加分（即便 PE 正常,但增速匹配的好估值仍可加分）
    if peg_band == "极低估":
        pts += 4
    elif peg_band == "低估":
        pts += 2
    # 风险否决
    risk = scores.get("risk", {}).get("score", 0)
    if risk > 60: pts -= 40
    pts = max(0, min(100, round(pts)))
    pe_note = "估值" + str(pe_band) if pe_band else "估值未知"
    return {"score": pts, "detail": f"量价+资金+板块+情绪+{pe_note}"}


# trade_score（交易排序分）合成口径：
#   trade = total × _TRADE_W_TOTAL + buy × _TRADE_W_BUY
#   - total  : 综合评分（0~BASE_TOTAL），完全由用户可调的 7 维权重驱动（见 _composite_score）
#   - buy    : _buy_score 择时买点分（0~100），固定择时口径，不吃维度权重
# 调整历史：2025-XX 由 0.7/0.3 调整为 0.85/0.15——用户反馈「想让权重话语权更大」，
#   买点分占比过高会稀释用户自定义维度的排序影响；保留 15% 给买点分以保底择时信息量。
# 注意：_TRADE_W_TOTAL + _TRADE_W_BUY 应恒等于 1.0。
_TRADE_W_TOTAL = 0.85  # 综合评分（吃维度权重）在 trade_score 中的占比
_TRADE_W_BUY = 0.15    # 择时买点分（不吃权重）在 trade_score 中的占比


def _composite_score(scores: dict[str, Any], weights: dict[str, float]) -> float:
    """综合评分：各维度按相对权重做归一化加权平均，再减风险扣分。

    总分 = BASE_TOTAL × Σ(维度分×权重) / Σ(维度满分×权重)。
    权重表达「相对看重程度」：默认全 1.0 时即原始分之和（行为不变）；
    调大某维度，该维度强的股票总分上升、弱的下降。任意维度权重翻倍的
    相对影响相同（不再受各维度满分差异影响），总分恒在 0~BASE_TOTAL 之间。

    风险扣分非线性：risk > 20 时按 (risk-20)^1.5 / 12 扣分,接近 AVOID 阈值
    (60)时扣分显著加速,与「risk>60 → AVOID」信号语义一致。
    参考点:risk=30→2.6,risk=50→13.7,risk=60→21.1,risk=80→38.7。
    """
    m = valuecfg.DIM_MAXES
    num = sum(scores[k]["score"] * weights.get(k, 1.0) for k in m)
    den = sum(m[k] * weights.get(k, 1.0) for k in m)
    total = valuecfg.BASE_TOTAL * num / den if den > 0 else 0.0
    risk = scores["risk"]["score"]
    if risk > 20:
        # 非线性扣分:接近 AVOID 阈值时大幅扣分
        total -= (risk - 20) ** 1.5 / 12
    return max(0.0, min(float(valuecfg.BASE_TOTAL), round(total, 1)))


_SIGNAL_TRIGGERS = {
    "VALUE_BUY": "估值低估 + 风险<30 + 非连板；分批建仓，跌破估值买区企稳再接",
    "QUALITY_HOLD": "总分达标 + 估值合理；持有条件=基本面不恶化，现金流恶化即减",
    "BREAKOUT_BUY": "放量突破 + 量比≥1.5；跌破突破位止损",
    "PULLBACK_BUY": "板块启动/分歧期低吸；跌破分歧低点止损",
    "BUY": "综合分达标；按买点评分择时，不追高",
    "WATCH": "总分 60~74；等待资金拐点或估值回落确认",
    "AVOID": "风险>60（暴雷/高位连板/追高）；暂不参与",
    "EXIT": "估值严重高估 + 风险偏高；利用反弹分批减仓",
    "REDUCE": "短线急跌 -4%+；先减仓再观察企稳信号",
}


def _advice_for(
    signal: str, risk: int, advice_map: dict[str, str]
) -> tuple[str, str, str]:
    """信号 → (signal, advice, trigger)。纯函数便于契约测试。

    AVOID 有两种成因，文案必须区分（此前混用「不参与」+「风险>60」触发说明，
    低分无风险的股票会显示误导性文案）：
    - risk > 60 → 真风险否决：「不参与」+ 原触发说明；
    - risk ≤ 60 → 只是总分未达 60% 观察线：「未达观察线」+ 对应说明。
    """
    if signal == "AVOID" and risk <= 60:
        return (signal, "未达观察线",
                "总分未达 60% 观察线（无风险否决）；盘前/弱市板块与情绪维度缺数据时"
                "评分普遍偏低属正常，等待资金或板块确认后再看")
    return (signal, advice_map.get(signal, "—"), _SIGNAL_TRIGGERS.get(signal, ""))


def _signal(
    profile: dict[str, Any], total: float, buy: int, risk: int,
    value_metrics: "dict[str, Any] | None" = None,
) -> str:
    """买卖信号：覆盖价值投资与情绪博弈两套语义：
        EXIT          高估 + 风险 → 建议清仓
        AVOID         风险 > 60    → 不参与
        VALUE_BUY     估值低估（PE/PEG 档位）+ 基本面稳健 + 风险低 → 价值低估买入
        QUALITY_HOLD  总分 80+ 且估值合理 → 长期持有
        BREAKOUT_BUY  趋势突破 + 量价齐升
        PULLBACK_BUY  启动期/分歧期低吸
        BUY           综合达标
        WATCH         60~74 尚需确认
        REDUCE        短线急跌 -4%+
    """
    chg = profile.get("change_pct") or 0
    vr = profile.get("volume_ratio") or 0
    lb = profile.get("lianban") or 0
    pe = profile.get("pe")
    # 阈值按 BASE_TOTAL 的相对比例（维度增减后 BASE_TOTAL 会变，写死会漂移）：
    #   hi ≈ 0.815（原 75/92）、top ≈ 0.87（原 80/92）、mid ≈ 0.65（原 60/92）
    base = valuecfg.BASE_TOTAL or 106.0
    # 阈值按 BASE_TOTAL 相对比例:hi=0.80(85)、top=0.85(90)、mid=0.60(64)
    # 写死绝对值会随维度增减漂移,统一用占比口径,信号分布更顺滑。
    hi_cut = base * 0.80
    top_cut = base * 0.85
    mid_cut = base * 0.60
    if risk > 60:
        return "AVOID"
    # 价值投资维度（独立分支,不依赖 total>=hi_cut）：
    # 旧口径硬性要求 PE<15 过严，会漏掉「PE 30 但增速 40%」这类 PEG 极优标的。
    # 改为按估值档位判定：PE 低估/深度低估 或 PEG 低估/极低估，配基本面与低风险。
    vm = value_metrics or {}
    pe_band = vm.get("pe_band")
    peg_band = vm.get("peg_band")
    value_ok = pe_band in ("深度低估", "低估") or peg_band in ("极低估", "低估")
    # 连板梯队股走情绪线，不占用价值买点语义
    if value_ok and total >= mid_cut and risk < 30 and lb == 0:
        return "VALUE_BUY"
    # EXIT：估值严重高估 + 风险中等 → 建议清仓
    if pe is not None and pe >= 100 and risk > 25:
        return "EXIT"
    if total >= hi_cut and buy >= 70:
        # 长期持有：总分优秀 + 估值明确合理（PE 缺失时不走 QUALITY_HOLD,保持既有信号）
        if total >= top_cut and pe is not None and 0 < pe <= 30:
            return "QUALITY_HOLD"
        if chg > 5 and vr >= 1.5 and lb >= 1:
            return "BREAKOUT_BUY"
        if 0 < chg <= 5 and vr < 1.2 and lb >= 1:
            return "PULLBACK_BUY"
        return "BUY"
    if total >= mid_cut:
        return "WATCH"
    if chg < -4:
        return "REDUCE"
    return "AVOID"


def _grade(total: float) -> tuple[str, str]:
    """分级：阈值按 BASE_TOTAL 的相对比例判定。

    维度增减会改变 BASE_TOTAL（如新增相对强度/价格位置后 92 → 106），
    写死绝对阈值会让分布整体漂移，故统一用占比口径，保证分级语义稳定。
    """
    ratio = total / valuecfg.BASE_TOTAL if valuecfg.BASE_TOTAL else 0.0
    # S=核心(0.88,稀缺),A=优质(0.80,常见),B=待确认(0.72),
    # C=观察(0.60),D=淘汰。
    if ratio >= 0.88: return "S", "核心机会池"
    if ratio >= 0.80: return "A", "重点观察池"
    if ratio >= 0.72: return "B", "待确认池"
    if ratio >= 0.60: return "C", "观察池"
    return "D", "淘汰"


def _completeness(profile: dict[str, Any]) -> int:
    """数据完整度 0-100：行情/财务/资金/K线各占权重。"""
    pts = 20
    if profile.get("price") is not None: pts += 20
    if profile.get("financials"): pts += 25
    if profile.get("flow"): pts += 20
    if profile.get("kline"): pts += 15
    return min(100, pts)


