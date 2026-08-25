"""离线冒烟测试：CI 门禁用，不依赖外部网络 / 数据源 / 浏览器。

覆盖：模块可导入、LLM JSON 修复阶梯、LLM 配置指纹、缓存单飞与过期重载、
AI 每股票单飞锁、均线与支撑压力指标、数据源注册表装配。
运行：python backend/smoke_test.py（退出码 0=通过）
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# 用临时数据目录，避免污染工作区 / CI 环境
_tmp = tempfile.mkdtemp(prefix="board-smoke-")
os.environ["DATA_DIR"] = _tmp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import analysis, api, cache, llm, llmcfg, news, reports, service, storage  # noqa: E402
from backend.config import settings  # noqa: E402
from backend.indicators import build_ma, summarize_flow, support_resistance  # noqa: E402
from backend.providers import registry  # noqa: E402
from backend.providers.base import Bar  # noqa: E402

# DATA_DIR 在 backend.config 导入时就解析定型：若本模块被导入前 backend.config 已加载
# （例如在 REPL 里先 import backend.storage 再跑这里的测试），上面的 environ 设置不再生效，
# 测试会直接写进使用者的真实库并抹掉已保存的 LLM 配置。此处硬断言，宁可不跑也不能写坏数据。
if settings.DATA_DIR != Path(_tmp):
    raise SystemExit(
        f"冒烟测试隔离失效：DATA_DIR={settings.DATA_DIR}，期望临时目录 {_tmp}。\n"
        "请以 `python backend/smoke_test.py` 独立进程运行，勿在已导入 backend 的会话中调用。"
    )

FAILED: list[str] = []
TOTAL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    print(f"[{'ok' if cond else 'FAIL'}] {name}" + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------------ LLM JSON 修复阶梯
def test_json_repair() -> None:
    cases: dict[str, str] = {
        '{"action": "持有观望", "confidence": 80}': "持有观望",
        '```json\n{"action": "积极持仓/加仓"}\n```': "积极持仓/加仓",
        '```JSON\n{"action": "清仓离场"}\n```': "清仓离场",
        "前文寒暄 {\"action\": \"减仓规避\"} 结尾废话": "减仓规避",
        '{"action": "清仓离场", "list": [1, 2,]}': "清仓离场",
        '{"action": "持有观望", "trend": {"summary": "多头", "short": "截断': "持有观望",
        '{"advice": {"action": "持有观望", "reason": "依据", "support": 12.34': "持有观望",
        '{"action": "减仓规避", "a": {"b": [1,2,3], "c": {"d": 1}': "减仓规避",
        '{"action": "持有观望", "x": 1}{"action": "减仓规避"}': "持有观望",
    }
    for raw, want in cases.items():
        try:
            obj = llm._extract_json(raw)
            got = obj.get("action") or (obj.get("advice") or {}).get("action")
            check(f"JSON修复: {raw[:24]}...", got == want, f"got={got!r}")
        except llm.LLMError as exc:
            check(f"JSON修复: {raw[:24]}...", False, str(exc))
    try:
        llm._extract_json("这不是 JSON 内容")
        check("JSON修复: 垃圾输入应报错", False)
    except llm.LLMError:
        check("JSON修复: 垃圾输入应报错", True)


# ------------------------------------------------------------------ 异常描述非空
# httpx 超时类异常 str() 为空，历史上导致「LLM 请求失败: 」「全部数据源失败 -> sina: 」
# 这类无信息报错。凡是把异常插值进用户可见文案的地方都必须经过 describe_exc。
def test_describe_exc() -> None:
    import httpx

    from backend.utils import describe_exc

    empty = [httpx.ReadTimeout(""), httpx.ConnectTimeout(""), httpx.PoolTimeout(""),
             httpx.WriteTimeout(""), httpx.ReadError("")]
    for exc in empty:
        got = describe_exc(exc)
        check(f"异常描述非空: {type(exc).__name__}", got == type(exc).__name__, f"got={got!r}")
    check("异常描述: 保留原消息",
          describe_exc(httpx.ConnectError("getaddrinfo failed")) == "getaddrinfo failed")

    # LLM 路径：超时应给出可执行提示，且不得出现空的尾巴
    msg = llm._request_error(httpx.ReadTimeout(""), "https://api.deepseek.com/v1/chat/completions")
    check("LLM 超时提示含主机名", "api.deepseek.com" in msg, msg)
    check("LLM 超时提示非空", len(msg.strip()) > 10, msg)


def test_llm_timeout_floor() -> None:
    """LLM 超时下限：旧 .env/compose 残留的 45s 等过小配置不得掐断正常生成。"""
    from backend.config import _clamp_llm_timeout

    check("LLM 超时下限: 45s 提到 90s", _clamp_llm_timeout(45.0) == 90.0)
    check("LLM 超时下限: 0 提到 90s", _clamp_llm_timeout(0.0) == 90.0)
    check("LLM 超时下限: 90s 保持", _clamp_llm_timeout(90.0) == 90.0)
    check("LLM 超时下限: 120s 保持", _clamp_llm_timeout(120.0) == 120.0)
    check("LLM 超时下限: 默认生效值≥90s", settings.LLM_TIMEOUT >= 90.0, f"got={settings.LLM_TIMEOUT}")


# ------------------------------------------------------------------ LLM 配置指纹
def test_fingerprint() -> None:
    """指纹随配置变化。

    本测试会写入 llm_config，因此必须先确认落在临时库上（见文件头的 DATA_DIR 断言），
    并在结束时还原原值——直接 clear 会抹掉使用者保存的真实 API Key。
    """
    storage.init_db()
    saved = storage.get_kv("llm_config")
    try:
        f1 = llmcfg.fingerprint()
        storage.set_llm_config({"enabled": True, "base_url": "https://x/v1", "model": "m1", "api_key": "k", "json_mode": True})
        f2 = llmcfg.fingerprint()
        storage.set_llm_config({"enabled": True, "base_url": "https://x/v1", "model": "m2", "api_key": "k", "json_mode": True})
        f3 = llmcfg.fingerprint()
        check("指纹随配置变化", f1 != f2 and f2 != f3)
    finally:
        if saved:
            storage.set_kv("llm_config", saved)
        else:
            storage.clear_llm_config()


# ------------------------------------------------------------------ LLM 多档案
# 多模型支持：多份档案存取、主模型唯一、密钥保留、旧单配置迁移、指纹联动、故障转移
def test_llm_profiles() -> None:
    """多档案：保存/主模型唯一/密钥保留/迁移/指纹（落在临时库上，结束还原）。"""
    storage.init_db()
    saved_profiles = storage.get_kv("llm_profiles")
    saved_legacy = storage.get_kv("llm_config")
    try:
        # 1) 保存两份档案，未标记主模型时第一个启用的自动设为主
        clean = llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "ka", "json_mode": True},
            {"id": "b", "name": "B", "enabled": True, "vendor": "custom",
             "base_url": "https://b/v1", "model": "m-b", "api_key": "kb", "json_mode": False},
        ])
        check("多档案: 保存两份", len(clean) == 2, str(clean))
        check("多档案: 未标记时首启自动为主", clean[0]["primary"] is True and clean[1]["primary"] is False,
              str([(p["name"], p["primary"]) for p in clean]))
        check("多档案: 主模型优先", llmcfg.get_config()["id"] == "a")
        check("多档案: 可用性", llm.available() is True)

        # 2) 主模型唯一：再存时显式标记 b 为主，a 取消
        clean = llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "primary": False, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "", "json_mode": True},
            {"id": "b", "name": "B", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://b/v1", "model": "m-b", "api_key": "", "json_mode": False},
        ])
        check("多档案: 主模型切换", clean[1]["primary"] is True and clean[0]["primary"] is False,
              str([(p["name"], p["primary"]) for p in clean]))
        # api_key 传空且档案已存在 → 保留原密钥
        check("多档案: 留空保留密钥", clean[0]["api_key"] == "ka" and clean[1]["api_key"] == "kb",
              str([p["api_key"] for p in clean]))
        # clear_key=True 清空
        clean = llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "x", "clear_key": True,
             "json_mode": True},
        ])
        check("多档案: clear_key 清空密钥", clean[0]["api_key"] == "", str(clean))

        # 3) 旧单配置迁移：清空多档案，写旧 llm_config，get_profiles 应得到一份主档案
        storage.delete_kv("llm_profiles")
        storage.set_llm_config({"enabled": True, "base_url": "https://old/v1", "model": "m-old",
                                "api_key": "k-old", "json_mode": True})
        migrated = llmcfg.get_profiles()
        check("多档案: 旧单配置迁移为主档案",
              len(migrated) == 1 and migrated[0]["primary"] is True
              and migrated[0]["base_url"] == "https://old/v1" and migrated[0]["model"] == "m-old",
              str(migrated))

        # 4) 指纹联动：多档案任一变化指纹即变
        llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "ka", "json_mode": True},
        ])
        fp1 = llmcfg.fingerprint()
        llmcfg.save_profiles([
            {"id": "a", "name": "A", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a2", "api_key": "ka", "json_mode": True},
        ])
        fp2 = llmcfg.fingerprint()
        check("多档案: 模型变化指纹变", fp1 != fp2, f"{fp1} vs {fp2}")

        # 5) 故障转移顺序：主模型在前
        llmcfg.save_profiles([
            {"id": "b", "name": "B", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://b/v1", "model": "m-b", "api_key": "kb", "json_mode": True},
            {"id": "a", "name": "A", "enabled": True, "primary": False, "vendor": "custom",
             "base_url": "https://a/v1", "model": "m-a", "api_key": "ka", "json_mode": True},
        ])
        order = llm.ordered_profiles()
        check("多档案: 调用顺序主模型优先", [p["id"] for p in order] == ["b", "a"],
              str([p["id"] for p in order]))

        # 6) merge_pending（测试连接）：按 id 匹配档案为基底，api_key 留空保留已存密钥
        #    主模型是 b，测试备选 a 时不能拿 b 的字段、也不能把 a 的密钥覆盖成空
        merged = llmcfg.merge_pending({"id": "a", "name": "A", "enabled": True,
                                       "primary": False, "vendor": "custom",
                                       "base_url": "https://a/v1", "model": "m-a",
                                       "api_key": "", "json_mode": True})
        check("多档案: merge_pending 按 id 取档案", merged["base_url"] == "https://a/v1"
              and merged["model"] == "m-a", str(merged))
        check("多档案: merge_pending 留空保留密钥", merged["api_key"] == "ka", str(merged))
        merged2 = llmcfg.merge_pending({"id": "a", "vendor": "custom",
                                        "base_url": "https://a/v1", "model": "m-a",
                                        "api_key": "new-key", "json_mode": True})
        check("多档案: merge_pending 显式填密钥覆盖", merged2["api_key"] == "new-key", str(merged2))
        merged3 = llmcfg.merge_pending({"id": "no-such", "vendor": "custom",
                                        "base_url": "https://x/v1", "model": "m-x",
                                        "api_key": "", "json_mode": True})
        check("多档案: merge_pending 未知 id 回退主档案", merged3["id"] == "b", str(merged3))
    finally:
        if saved_profiles is not None:
            storage.set_kv("llm_profiles", saved_profiles)
        else:
            storage.delete_kv("llm_profiles")
        if saved_legacy is not None:
            storage.set_kv("llm_config", saved_legacy)
        else:
            storage.clear_llm_config()


def test_llm_failover() -> None:
    """chat_json 故障转移：主模型失败自动切换下一个，全部失败汇总原因。"""
    storage.init_db()
    saved_profiles = storage.get_kv("llm_profiles")
    saved_legacy = storage.get_kv("llm_config")
    orig_chat_one = llm._chat_one
    try:
        llmcfg.save_profiles([
            {"id": "bad", "name": "坏模型", "enabled": True, "primary": True, "vendor": "custom",
             "base_url": "https://bad/v1", "model": "m-bad", "api_key": "kb", "json_mode": True},
            {"id": "good", "name": "好模型", "enabled": True, "primary": False, "vendor": "custom",
             "base_url": "https://good/v1", "model": "m-good", "api_key": "kg", "json_mode": True},
        ])
        calls: list[str] = []

        async def fake_chat_one(cfg, system, user):
            calls.append(cfg["id"])
            if cfg["id"] == "bad":
                raise llm.LLMError("超时")
            return {"ok": 1}, {"model": cfg["model"]}

        llm._chat_one = fake_chat_one
        data, meta = asyncio.run(llm.chat_json("s", "u"))
        check("故障转移: 主模型失败切备选", calls == ["bad", "good"] and data == {"ok": 1},
              f"calls={calls}")
        check("故障转移: 返回实际模型", meta.get("model") == "m-good", str(meta))

        # 全部失败 → 汇总各档案原因
        async def fake_all_fail(cfg, system, user):
            raise llm.LLMError("挂了")

        llm._chat_one = fake_all_fail
        try:
            asyncio.run(llm.chat_json("s", "u"))
            check("故障转移: 全败应抛错", False, "未抛错")
        except llm.LLMError as exc:
            msg = str(exc)
            check("故障转移: 全败汇总原因", "坏模型" in msg and "好模型" in msg, msg)
    finally:
        llm._chat_one = orig_chat_one
        if saved_profiles is not None:
            storage.set_kv("llm_profiles", saved_profiles)
        else:
            storage.delete_kv("llm_profiles")
        if saved_legacy is not None:
            storage.set_kv("llm_config", saved_legacy)
        else:
            storage.clear_llm_config()


# ------------------------------------------------------------------ 价值选股评分引擎
# 纯函数评分：市场状态分类 / 财务评分 / 资金拐点 / 风险 / 信号 / 分级
def test_value_screener() -> None:
    from backend import value_screener as vs

    # 市场状态分类
    mk = vs._market_state(
        [{"code": "000001", "change_pct": 1.8}, {"code": "399006", "change_pct": 2.1}],
        zt_count=60, avg_chg=2.0)
    check("价值选股: 强趋势市场识别", mk["state"] == "A" and mk["attack"] >= 80, str(mk))
    mk2 = vs._market_state([{"code": "000001", "change_pct": -2.0}], zt_count=10, avg_chg=-1.0)
    check("价值选股: 退潮市场识别", mk2["state"] in ("E", "F") and mk2["attack"] <= 30, str(mk2))

    # 候选过滤：剔除 ETF / 北交所 / 非 A 股代码
    check("价值选股: 保留沪主板/创业板", vs._is_stock_code("601012", "SH")
          and vs._is_stock_code("300750", "SZ"))
    check("价值选股: 剔除 ETF/LOF", not vs._is_stock_code("515070", "SH")
          and not vs._is_stock_code("159819", "SZ"))
    check("价值选股: 剔除北交所", not vs._is_stock_code("920002", "BJ"))

    # 财务评分：高质量高成长 > 亏损低质
    good_fin = [
        {"revenue_yoy": 25.0, "net_profit_yoy": 40.0, "roe": 18.0, "gross_margin": 45.0, "debt_ratio": 30.0},
        {"revenue_yoy": 15.0, "net_profit_yoy": 20.0, "roe": 15.0, "gross_margin": 40.0, "debt_ratio": 35.0},
    ]
    bad_fin = [
        {"revenue_yoy": -20.0, "net_profit_yoy": -50.0, "roe": -5.0, "gross_margin": 5.0, "debt_ratio": 90.0},
    ]
    g = vs._financial_score(good_fin)
    b = vs._financial_score(bad_fin)
    check("价值选股: 优质公司财务分高", g["score"] > b["score"] + 10, f"good={g['score']} bad={b['score']}")
    check("价值选股: 财务缺失记 0 分", vs._financial_score([])["score"] == 0)

    # 资金拐点：30 日流出但近 5 日流入应加分
    flow_out = [{"date": f"d{i}", "main": -2e7} for i in range(25)]
    flow_in = [{"date": f"d{i}", "main": 3e7} for i in range(5)]
    fp = vs._flow_score({"flow": flow_out + flow_in})
    fn = vs._flow_score({"flow": [{"date": f"d{i}", "main": -2e7} for i in range(8)]})
    check("价值选股: 资金拐点得分更高", fp["score"] > fn["score"], f"turn={fp['score']} out={fn['score']}")

    # 风险：ST/亏损/高负债/高位连板
    risk = vs._risk_score({"name": "*ST测试", "financials": [{"net_profit_yoy": -60.0, "debt_ratio": 85.0, "net_profit": -1e8}], "pe": 200, "change_pct": 10.0, "lianban": 6}, {})
    check("价值选股: 暴雷组合风险高", risk["score"] > 60 and "ST" in " ".join(risk["notes"]), str(risk))
    safe = vs._risk_score({"name": "正常股", "financials": [{"net_profit_yoy": 20.0, "debt_ratio": 30.0, "net_profit": 1e8}], "pe": 20, "change_pct": 1.0, "lianban": 0}, {})
    check("价值选股: 正常股风险低", safe["score"] <= 20, str(safe))

    # 分级与信号
    check("价值选股: S 级判定", vs._grade(88)[0] == "S")
    check("价值选股: D 级淘汰", vs._grade(55)[0] == "D")
    check("价值选股: 风险高信号 AVOID", vs._signal({"change_pct": 1, "volume_ratio": 1, "lianban": 0}, 80, 80, 70) == "AVOID")
    check("价值选股: 突破买入信号", vs._signal({"change_pct": 6, "volume_ratio": 2, "lianban": 1}, 80, 75, 10) == "BREAKOUT_BUY")

    # 综合评分：归一化相对权重（默认全 1.0 = 原始分之和，总分恒在 0~92）
    def _sc(v): return {"score": v, "detail": ""}
    sc = {"finance": _sc(30), "board": _sc(5), "flow": _sc(6),
          "volume": _sc(4), "emotion": _sc(6), "risk": _sc(10)}
    w1 = {k: 1.0 for k in ("finance", "board", "flow", "volume", "emotion")}
    t1 = vs._composite_score(sc, w1)
    check("价值选股: 默认权重=原始分之和", abs(t1 - 51) < 0.01, f"t1={t1}")
    # 权重是相对看重：基本面强的股票调大 finance 权重总分上升，弱的下降（只改排序不单方向加分）
    strong = {"finance": _sc(45), "board": _sc(1), "flow": _sc(1),
              "volume": _sc(1), "emotion": _sc(1), "risk": _sc(10)}
    weak = {"finance": _sc(5), "board": _sc(9), "flow": _sc(11),
            "volume": _sc(7), "emotion": _sc(11), "risk": _sc(10)}
    w_fin2 = dict(w1); w_fin2["finance"] = 2.0
    up = vs._composite_score(strong, w_fin2) - vs._composite_score(strong, w1)
    down = vs._composite_score(weak, w_fin2) - vs._composite_score(weak, w1)
    check("价值选股: 权重放大强维度、压低弱维度", up > 0 and down < 0,
          f"up={up:.1f} down={down:.1f}")
    # 全部维度同强度（50%）时权重不影响总分（只改变相对排序）
    neutral = {k: _sc(v / 2) for k, v in vs.valuecfg.DIM_MAXES.items()}
    neutral["risk"] = _sc(10)
    t_neutral = vs._composite_score(neutral, w1)
    t_neutral2 = vs._composite_score(neutral, w_fin2)
    check("价值选股: 同质股票权重不影响总分", abs(t_neutral - 46) < 0.01 and abs(t_neutral2 - 46) < 0.01,
          f"t={t_neutral} t2={t_neutral2}")
    # 总分恒在 0~92（不再因权重放大顶到 100 截断失真）
    maxed = {k: _sc(v) for k, v in vs.valuecfg.DIM_MAXES.items()}
    maxed["risk"] = _sc(10)
    for w_ in (w1, w_fin2, {"finance": 3.0, "board": 0.2, "flow": 3.0, "volume": 0.2, "emotion": 3.0}):
        t = vs._composite_score(maxed, w_)
        check("价值选股: 总分在 0~92", 0 <= t <= 92, f"t={t}")
    sc_risk = dict(sc); sc_risk["risk"] = _sc(80)
    check("价值选股: 高风险扣分", vs._composite_score(sc_risk, w1) < vs._composite_score(sc, w1))


def test_value_weights() -> None:
    """价值选股权重：默认 1.0 / 保存 clamp / 恢复默认 / 指纹联动（临时库，结束还原）。"""
    storage.init_db()
    saved = storage.get_kv("value_weights")
    try:
        from backend import valuecfg as vc
        w0 = vc.get_weights()
        check("价值权重: 默认全 1.0", all(abs(v - 1.0) < 1e-9 for v in w0.values()), str(w0))
        w = vc.save_weights({"finance": 2.0, "board": 0.5, "emotion": 9.9, "bad": 3})
        check("价值权重: 保存并 clamp", w["finance"] == 2.0 and w["board"] == 0.5
              and w["emotion"] == vc._MAX, str(w))
        check("价值权重: 未知维度忽略", "bad" not in w)
        check("价值权重: 重读一致", vc.get_weights()["finance"] == 2.0)
        fp1 = vc.fingerprint()
        vc.save_weights({"finance": 1.0})
        check("价值权重: 权重变化指纹变", fp1 != vc.fingerprint())
        vc.reset_weights()
        check("价值权重: 恢复默认", all(abs(v - 1.0) < 1e-9 for v in vc.get_weights().values()))
    finally:
        if saved is not None:
            storage.set_kv("value_weights", saved)
        else:
            storage.delete_kv("value_weights")

    # 选股结果补自选状态：pools 与 stocks 中每只股票都应被标记 watched
    from backend import storage as _storage
    from backend import api as _api
    sample = {"pools": {"core": [{"code": "601012", "name": "隆基绿能"}]},
              "stocks": [{"code": "601012", "name": "隆基绿能"}, {"code": "300750", "name": "宁德时代"}]}
    _api._mark_value_watched(sample)
    check("价值选股: pools 补自选状态", sample["pools"]["core"][0]["watched"] is False)
    check("价值选股: stocks 补自选状态", sample["stocks"][0]["watched"] is False
          and sample["stocks"][1]["watched"] is False)


# ------------------------------------------------------------------ 模型列表过滤
# 云端 /models 里混有 embedding/图片/语音等非对话模型，应被过滤、对话模型应保留
def test_model_filter() -> None:
    keep = ["deepseek-chat", "deepseek-reasoner", "qwen-plus", "glm-4-plus",
            "moonshot-v1-32k", "gpt-4o", "llama3.1:70b", "kimi-k2-thinking"]
    drop = ["text-embedding-3-large", "bge-m3", "dall-e-3", "whisper-1",
            "sdxl-turbo", "rerank-3", "image-1", "flux-dev"]
    bad_keep = [m for m in keep if llm._NON_CHAT_RE.search(m)]
    bad_drop = [m for m in drop if not llm._NON_CHAT_RE.search(m)]
    check("模型过滤（保留对话/剔除非对话）", not bad_keep and not bad_drop,
          f"keep误杀={bad_keep} drop漏网={bad_drop}")


# ------------------------------------------------------------------ 资讯解读
def test_news_interpret() -> None:
    # 规则解读关键词情绪
    bull = news.rule_interpret({"title": "公司中标大订单", "summary": "业绩预增 50%"})
    bear = news.rule_interpret({"title": "股东减持", "summary": "收到处罚决定书"})
    flat = news.rule_interpret({"title": "召开股东大会", "summary": "审议日常议案"})
    check("资讯规则解读: 利好词", bull["sentiment"] == "利好" and bull["engine"] == "rule", str(bull))
    check("资讯规则解读: 利空词", bear["sentiment"] == "利空", str(bear))
    check("资讯规则解读: 中性", flat["sentiment"] == "中性", str(flat))

    # 资讯评分进规则引擎：利好加分、封顶 ±12、无资讯不引用
    detail = {
        "quote": {"code": "600000", "name": "浦发银行", "price": 9.0, "prev_close": 9.1, "change_pct": -1.1},
        "boards": [], "kline": [], "ma": [],
        "ma_summary": {"arrangement": "交织", "above_count": 0, "above": [], "below": [], "series": {}},
        "support_resistance": {}, "fund_flow": {"rows": [], "summary": {}},
        "margin": {"rows": [], "summary": {}},
        "status": {"tags": [], "trend": {}},
    }
    news_items = [
        {"title": "中标大单", "summary": "s", "date": "2026-08-17 10:00:00",
         "interpretation": {"sentiment": "利好", "impact": "高", "summary": "正面"}},
        {"title": "获准收购", "summary": "s", "date": "2026-08-16 10:00:00",
         "interpretation": {"sentiment": "利好", "impact": "中", "summary": "正面"}},
        {"title": "被罚款", "summary": "s", "date": "2026-08-15 10:00:00",
         "interpretation": {"sentiment": "利空", "impact": "中", "summary": "负面"}},
    ]
    fb = analysis.rule_based(detail, news_items)
    check("资讯评分写入建议依据", "资讯面 2 利好/1 利空（计 +4 分）" in fb["advice"]["reason"], fb["advice"]["reason"])
    check("资讯情绪进机会面", any("资讯面偏暖" in o for o in fb["risk"]["opportunities"]))
    fb0 = analysis.rule_based(detail, None)
    check("无资讯不引用", "资讯面" not in fb0["advice"]["reason"])
    payload = analysis.build_payload(detail, news_items)
    check("投喂数据含资讯段", len(payload.get("市场资讯_近30日") or []) == 3)

    # 券商研报面进规则引擎：利好加分、封顶 ±15、无研报不引用
    report_items = [
        {"rating": "买入", "title": "业绩预增", "source": "国海证券", "date": "2026-08-10 09:00:00",
         "interpretation": {"sentiment": "利好", "impact": "高", "summary": "正面"}},
        {"rating": "增持", "title": "盈利增速抬升", "source": "平安证券", "date": "2026-08-05 09:00:00",
         "interpretation": {"sentiment": "利好", "impact": "中", "summary": "正面"}},
        {"rating": "减持", "title": "业绩下滑", "source": "某券商", "date": "2026-08-01 09:00:00",
         "interpretation": {"sentiment": "利空", "impact": "中", "summary": "负面"}},
    ]
    fb_r = analysis.rule_based(detail, None, report_items)
    check("研报评分写入建议依据", "研报面 2 利好/1 利空（计 +5 分）" in fb_r["advice"]["reason"], fb_r["advice"]["reason"])
    check("研报情绪进机会面", any("券商研报面偏暖" in o for o in fb_r["risk"]["opportunities"]))
    fb_no_r = analysis.rule_based(detail, None, None)
    check("无研报不引用", "研报面" not in fb_no_r["advice"]["reason"])
    payload_r = analysis.build_payload(detail, None, report_items)
    check("投喂数据含券商观点段", len(payload_r.get("券商观点_近30日") or []) == 3)


# ------------------------------------------------------------------ 市场热点追踪
def test_hotspot() -> None:
    from backend import hotspot
    import json as _json

    now_ts = int(time.time())
    old_ts = now_ts - 3600  # 一小时前，应在窗口外

    # 同花顺：ctime 为 unix 秒
    ths_payload = _json.dumps({"code": "200", "data": {"list": [
        {"id": "1", "title": "富时中国A50指数期货跌2%", "digest": "摘要A",
         "url": "http://news.10jqka.com.cn/1.shtml", "ctime": now_ts, "source": ""},
        {"id": "2", "title": "旧闻不展示", "digest": "摘要B",
         "url": "http://news.10jqka.com.cn/2.shtml", "ctime": old_ts, "source": "测试媒体"},
        {"id": "bad", "title": "时间缺失", "digest": "x", "url": "", "ctime": None},
    ]}})
    ths_rows = hotspot._parse_ths(ths_payload)
    check("热点解析: 同花顺", len(ths_rows) == 2 and ths_rows[0]["source"] == "同花顺", str(ths_rows))

    # 东方财富：showTime 为字符串
    em_payload = _json.dumps({"code": "1", "data": {"list": [
        {"code": "E1", "title": "财联社：央行开展逆回购操作", "summary": "摘要C",
         "showTime": "2026-08-19 10:30:00", "mediaName": "财联社", "url": "http://finance.eastmoney.com/a/E1.html"},
        {"code": "E2", "title": "彭博：美股期货上涨", "summary": "摘要D",
         "showTime": "2026-08-19 10:31:00", "mediaName": "彭博", "url": "http://finance.eastmoney.com/a/E2.html"},
        {"code": "E3", "title": "坏时间", "summary": "x",
         "showTime": "not-a-date", "mediaName": "东方财富", "url": ""},
    ]}})
    em_rows = hotspot._parse_em(em_payload)
    check("热点解析: 东方财富", len(em_rows) == 2 and em_rows[0]["source"] == "财联社", str(em_rows))

    # 新浪：rich_text 拆标题/摘要
    sina_payload = _json.dumps({"result": {"data": {"feed": {"list": [
        {"id": "S1", "create_time": "2026-08-19 10:32:00",
         "rich_text": "【澎湃新闻】沪深两市成交额突破万亿", "docurl": "http://finance.sina.com.cn/S1.html"},
        {"id": "S2", "create_time": "2026-08-19 10:33:00",
         "rich_text": "无括号纯文本内容", "docurl": ""},
    ]}}}})
    sina_rows = hotspot._parse_sina(sina_payload)
    check("热点解析: 新浪拆标题", sina_rows[0]["title"] == "澎湃新闻" and sina_rows[0]["summary"] == "沪深两市成交额突破万亿",
          str(sina_rows[0]))
    check("热点解析: 新浪无括号兜底", sina_rows[1]["title"] == "无括号纯文本内容", str(sina_rows[1]))

    # 华尔街见闻：display_time 为 unix 秒，content_text 无【】包裹，首句拆标题
    wscn_payload = _json.dumps({"code": 20000, "message": "OK", "data": {"items": [
        {"id": 3152710, "title": "", "content_text": "浙江：7月份，规模以上工业增加值同比增长7.4%。",
         "display_time": now_ts, "uri": "https://wallstreetcn.com/livenews/3152710"},
        {"id": 3152711, "title": "", "content_text": "沪深两市成交额突破万亿。A股放量上行，北向资金净流入。",
         "display_time": now_ts - 10, "uri": ""},
        {"id": 3152712, "title": "", "content_text": "无时间条目", "display_time": None, "uri": ""},
    ]}})
    wscn_rows = hotspot._parse_wscn(wscn_payload)
    check("热点解析: 华尔街见闻条数", len(wscn_rows) == 2, str(wscn_rows))
    check("热点解析: 华尔街见闻首句为标题",
          wscn_rows[0]["title"] == "浙江：7月份，规模以上工业增加值同比增长7.4%。"
          and wscn_rows[0]["summary"] == "", str(wscn_rows[0]))
    check("热点解析: 华尔街见闻多句拆摘要",
          wscn_rows[1]["title"] == "沪深两市成交额突破万亿。"
          and wscn_rows[1]["summary"] == "A股放量上行，北向资金净流入。", str(wscn_rows[1]))
    check("热点解析: 华尔街见闻媒体署名",
          wscn_rows[0]["source"] == "华尔街见闻" and wscn_rows[0]["origin"] == "华尔街见闻", str(wscn_rows[0]))

    # 窗口过滤：旧条目剔除
    in_rows = [r for r in ths_rows if hotspot._in_window(r["ts"], 30)]
    check("热点窗口: 30分钟内保留", len(in_rows) == 1 and in_rows[0]["id"] == "1", str(in_rows))
    check("热点窗口: 坏时间剔除", hotspot._in_window(None, 30) is False)

    # 跨源去重：同一标题只留一条（取最新时间），其余保留；华尔街见闻也参与
    dup = [
        {"id": "t1", "title": "央行开展逆回购操作", "ts": now_ts - 100, "origin": "同花顺", "source": "同花顺"},
        {"id": "e1", "title": "央行开展逆回购操作！", "ts": now_ts - 50, "origin": "东方财富", "source": "财联社"},
        {"id": "w1", "title": "央行开展逆回购操作，", "ts": now_ts - 20, "origin": "华尔街见闻", "source": "华尔街见闻"},
        {"id": "s1", "title": "富时中国A50指数期货跌2%", "ts": now_ts - 10, "origin": "新浪财经", "source": "新浪财经"},
    ]
    merged = hotspot._merge(dup)
    check("热点去重: 同标题合并留最新", len(merged) == 2 and any(m["id"] == "w1" for m in merged)
          and not any(m["id"] in ("t1", "e1") for m in merged), str(merged))
    check("热点去重: 按时间倒序", merged[0]["id"] == "s1" and merged[1]["id"] == "w1", str(merged))
    check("热点标题指纹: 标点归一", hotspot._title_fp("央行开展逆回购操作！") == hotspot._title_fp("央行开展逆回购操作"),
          hotspot._title_fp("央行开展逆回购操作！"))
    check("热点标题指纹: 【】包裹归一", hotspot._title_fp("【央行开展逆回购操作】") == hotspot._title_fp("央行开展逆回购操作"),
          hotspot._title_fp("【央行开展逆回购操作】"))

    # 重点媒体标注（金十数据只是快讯数据源，不在用户点名的重点媒体白名单内，
    # 因此不在 _HOT_MEDIA 中——这里只断言名单内媒体的命中/不命中）。
    check("热点媒体: 财联社命中", hotspot.is_hot_media("财联社") is True)

    # _FEEDS 注册项：两源已加入且解析函数正确绑定（用 id 断言避免闭包到原对象上）
    from backend import hotspot as _hp
    feed_names = [name for name, _u, _h, _p, _t in _hp._FEEDS]
    feed_tiers = {name: tier for name, _u, _h, _p, tier in _hp._FEEDS}
    feed_parsers = {name: parse for name, _u, _h, parse, _t in _hp._FEEDS}
    check("热点_FEEDS: 6 个源", len(feed_names) == 6, str(feed_names))
    check("热点_FEEDS: 含财联社与金十数据", "财联社" in feed_names and "金十数据" in feed_names, str(feed_names))
    check("热点_FEEDS: 财联社解析函数绑定", feed_parsers.get("财联社") is _hp._parse_cls, str(feed_parsers))
    check("热点_FEEDS: 金十解析函数绑定", feed_parsers.get("金十数据") is _hp._parse_jin10, str(feed_parsers))
    # 超时分级：快源应 ≤ 4s，标准源 ≤ 6s，金十应走 slow (≥ fast)。验证差异确实存在。
    tier_set = set(feed_tiers.values())
    check("热点_超时: 三档分级覆盖", "fast" in tier_set and "normal" in tier_set and "slow" in tier_set,
          str(feed_tiers))
    check("热点_超时: 金十数据走慢源档", feed_tiers.get("金十数据") == "slow", str(feed_tiers))
    check("热点_超时: 同花顺/新浪/华尔街见闻走快源档",
          feed_tiers.get("同花顺") == "fast" and feed_tiers.get("新浪财经") == "fast"
          and feed_tiers.get("华尔街见闻") == "fast", str(feed_tiers))
    # 整体预算应小于所有源超时之和，避免一个慢源把响应拖到 34s
    all_timeouts = sum(_hp._TIMEOUT_BY_TIER.get(t, 6.0) for t in feed_tiers.values())
    check("热点_超时: 整体预算小于源超时之和",
          settings.HOTSPOT_BUDGET < all_timeouts,
          f"budget={settings.HOTSPOT_BUDGET}, sum={all_timeouts}")
    # 整体预算收紧到 8s：实测 6 源全正常 < 0.5s，8s 已留足余量。
    check("热点_超时: 预算已收紧到 8s", settings.HOTSPOT_BUDGET == 8.0,
          f"budget={settings.HOTSPOT_BUDGET}")
    # SourceStat：连续失败 → 熔断 → 冷却 → 自动恢复，半开放重试
    from backend.hotspot import SourceStat
    import asyncio as _aio

    async def _run_circuit() -> bool:
        """熔断生命周期：调用 SourceStat 的 async API（加锁后改 async）。"""
        stat = SourceStat("测试源", open_at=2, cooldown=0.5)
        check("热点_熔断: 初始未熔断", await stat.is_open() is False, "")
        await stat.record_failure()
        check("热点_熔断: 失败 1 次仍未熔断", await stat.is_open() is False
              and stat.consecutive_failures == 1, "")
        await stat.record_failure()
        check("热点_熔断: 失败达阈值后熔断", await stat.is_open() is True
              and stat.consecutive_failures == 2, "")
        await _aio.sleep(0.6)
        check("热点_熔断: 冷却到期自动恢复（半开放）", await stat.is_open() is False, "")
        await stat.record_success()
        check("热点_熔断: 成功后清零计数", stat.consecutive_failures == 0, "")
        return True

    _aio.run(_run_circuit())
    # _fetch_one 重试参数化为指数序列
    import inspect
    sig = inspect.signature(_hp._fetch_one)
    retry_param = sig.parameters.get("retry_backoffs")
    check("热点_重试: _fetch_one 接受 retry_backoffs 序列参数", retry_param is not None, "")
    if retry_param is not None:
        check("热点_重试: 默认 (1.0, 2.0) 指数序列",
              retry_param.default == (1.0, 2.0), str(retry_param.default))
    # 熔断配置项
    check("热点_熔断: 配置项 OPEN_AT=3", settings.HOTSPOT_CIRCUIT_OPEN_AT == 3,
          str(settings.HOTSPOT_CIRCUIT_OPEN_AT))
    check("热点_熔断: 配置项 COOLDOWN=120s", settings.HOTSPOT_CIRCUIT_COOLDOWN == 120.0,
          str(settings.HOTSPOT_CIRCUIT_COOLDOWN))

    # Prometheus 指标：模块可导入、5 个指标注册、记录后能取回值
    from backend import metrics as _mt
    check("热点_指标: 模块可导入", hasattr(_mt, "SOURCE_REQUESTS"), "")
    check("热点_指标: 5 个核心指标都已注册",
          all(hasattr(_mt, n) for n in (
              "SOURCE_REQUESTS", "SOURCE_FAILURES", "SOURCE_CIRCUIT_OPEN",
              "SOURCE_ITEMS", "SOURCE_DURATION",
          )), "")

    async def _run_metrics() -> bool:
        """驱动一次 SourceStat + 直方图打点，验证 Gauge 增量与 Counter 递增。"""
        stat = SourceStat("指标测试源", open_at=2, cooldown=60.0)
        await stat.record_failure()           # 失败 1 → gauge=1, circuit=0
        await stat.record_failure()           # 失败 2 → 触发熔断，gauge=2, circuit=1
        # 取 Gauge 当前值（无需 scrape）
        from prometheus_client import REGISTRY
        def _gauge(name: str) -> float:
            return REGISTRY.get_sample_value(
                name, {"source": "指标测试源"}) or 0.0
        check("热点_指标: 连续失败 Gauge 同步到 2",
              _gauge("hotspot_source_consecutive_failures") == 2.0,
              str(_gauge("hotspot_source_consecutive_failures")))
        check("热点_指标: 熔断状态 Gauge=1",
              _gauge("hotspot_source_circuit_open") == 1.0,
              str(_gauge("hotspot_source_circuit_open")))
        await stat.record_success()           # 恢复 → gauge=0, circuit=0
        check("热点_指标: 恢复后连续失败=0",
              _gauge("hotspot_source_consecutive_failures") == 0.0,
              str(_gauge("hotspot_source_consecutive_failures")))
        check("热点_指标: 恢复后熔断=0",
              _gauge("hotspot_source_circuit_open") == 0.0,
              str(_gauge("hotspot_source_circuit_open")))
        # 模拟一次抓取：record_request + observe_duration
        _mt.SOURCE_REQUESTS.labels(source="指标测试源", result="success").inc()
        _mt.observe_duration("指标测试源", "success", 0.123)
        check("热点_指标: Counter 递增成功",
              REGISTRY.get_sample_value(
                  "hotspot_source_requests_total",
                  {"source": "指标测试源", "result": "success"}) or 0 >= 1,
              "")
        # export() 输出非空 + content_type 正确
        body, ctype = _mt.export()
        check("热点_指标: /metrics 输出含 Counter",
              b"hotspot_source_requests_total" in body, f"len={len(body)}")
        check("热点_指标: content_type 正确",
              "text/plain" in ctype, ctype)
        return True

    _aio.run(_run_metrics())

    # 财联社签名与 URL：参数固定后签名可复现，URL 含 sign 字段
    sign1 = _hp._cls_sign(_hp._CLS_ROLL_PARAMS)
    sign2 = _hp._cls_sign({"os": "web", "sv": "7.7.5", "app": "CailianpressWeb", "rn": "50", "last_time": ""})
    check("热点_财联社: 签名与顺序无关但值稳定", isinstance(sign1, str) and len(sign1) == 32 and sign1 == sign2, sign1)
    url = _hp._cls_url()
    check("热点_财联社: URL 含 sign 参数", "sign=" in url and "rn=50" in url, url)

    # 财联社解析：含电头 → 剥除后首句作标题；纯文本 → 首句兜底；时间缺失 → 剔除
    cls_payload = _json.dumps({"errno": 0, "data": {"roll_data": [
        {"id": 1001, "ctime": now_ts, "content": "财联社8月24日电，央行开展逆回购操作。规模为500亿元，期限7天。",
         "brief": "", "shareurl": "https://api3.cls.cn/share/article/1001?os=web"},
        {"id": 1002, "ctime": now_ts - 30, "content": "光通信板块盘前普跌，Coherent跌超5%，Lumentum跌近5%。",
         "brief": "", "shareurl": ""},
        {"id": 1003, "ctime": None, "content": "财联社8月24日电，无效时间。", "brief": "", "shareurl": ""},
        {"id": 1004, "ctime": now_ts - 60, "content": "", "brief": "", "shareurl": ""},
    ]}})
    cls_rows = _hp._parse_cls(cls_payload)
    check("热点_财联社: 数量与电头剥离", len(cls_rows) == 2 and cls_rows[0]["title"] == "央行开展逆回购操作。"
          and cls_rows[0]["source"] == "财联社", str(cls_rows))
    check("热点_财联社: 纯文本首句为标题", cls_rows[1]["title"] == "光通信板块盘前普跌，Coherent跌超5%，Lumentum跌近5%。",
          str(cls_rows[1]))
    check("热点_财联社: 时间缺失/空内容剔除", all(r["id"] not in ("1003", "1004") for r in cls_rows), str(cls_rows))

    # 财联社容错：非 JSON / data 缺失 / roll_data 非列表
    check("热点_财联社: 非 JSON 返回空", _hp._parse_cls("") == [], "expected []")
    check("热点_财联社: data 缺失返回空", _hp._parse_cls('{"errno":0}') == [], "expected []")
    check("热点_财联社: roll_data 非列表返回空", _hp._parse_cls('{"data":{"roll_data":"x"}}') == [], "expected []")

    # 金十解析：【标题】摘要 → 拆标题并剥电头；裸文本 → 首句兜底；time 非 datetime → 剔除
    jin10_payload = _json.dumps({"status": 200, "message": "OK", "data": [
        {"id": "J1", "data": {"content": "【福瑞医科：上半年净利润6394万】金十数据8月24日讯，同比增长23.12%。"}, "time": "2026-08-24 16:05:39"},
        {"id": "J2", "data": {"content": "伦敦金属交易所（LME）铜注册仓单增加5.14万吨，为5月以来最大增幅。"}, "time": "2026-08-24 16:05:30"},
        {"id": "J3", "data": {"content": "金十数据8月24日讯，无效时间。"}, "time": "not-a-date"},
        {"id": "J4", "data": {"content": ""}, "time": "2026-08-24 16:05:30"},
    ]})
    jin10_rows = _hp._parse_jin10(jin10_payload)
    check("热点_金十: 【】拆分+电头剥离", len(jin10_rows) == 2 and jin10_rows[0]["title"] == "福瑞医科：上半年净利润6394万"
          and jin10_rows[0]["source"] == "金十数据", str(jin10_rows))
    check("热点_金十: 裸文本首句为标题", jin10_rows[1]["title"] == "伦敦金属交易所（LME）铜注册仓单增加5.14万吨，为5月以来最大增幅。",
          str(jin10_rows[1]))
    check("热点_金十: 时间非法/空内容剔除", all(r["id"] not in ("J3", "J4") for r in jin10_rows), str(jin10_rows))

    # 金十容错：顶层 data 缺失 / 非 JSON
    check("热点_金十: data 缺失返回空", _hp._parse_jin10('{"status":200}') == [], "expected []")
    check("热点_金十: 非 JSON 返回空", _hp._parse_jin10("oops") == [], "expected []")

    check("热点媒体: 彭博命中", hotspot.is_hot_media("彭博社") is True)
    check("热点媒体: 普通媒体不命中", hotspot.is_hot_media("某地方日报") is False)
    check("热点媒体: 空来源不命中", hotspot.is_hot_media("") is False)

    # get_hotspot 兜底：全部源失败时返回结构化错误而非抛异常；
    # 且失败结果短暂缓存，故障期间反复请求不重打外部快讯接口
    from backend.cache import cache as _cache
    calls: dict[str, int] = {"n": 0}

    async def _boom(minutes: int) -> dict:
        calls["n"] += 1
        raise hotspot.ProviderError("全部热点快讯源均不可用")

    async def _fail() -> dict:
        orig = hotspot._load
        hotspot._load = _boom
        try:
            first = await hotspot.get_hotspot(41)
            second = await hotspot.get_hotspot(41)
            return first, second
        finally:
            hotspot._load = orig
            _cache.drop("hotspot:41")

    f1, f2 = asyncio.run(_fail())
    check("热点兜底: 全源失败返回错误结构", f1["items"] == [] and "error" in f1["meta"], str(f1))
    check("热点兜底: 失败结果缓存不重打", calls["n"] == 1 and f2["meta"]["error"] == f1["meta"]["error"],
          f"loads={calls['n']}")


# ------------------------------------------------------------------ 热点快讯 AI 分析
def test_hotspot_ai() -> None:
    from backend import hotspot_ai
    from backend.cache import cache as _cache
    from backend.providers.base import SearchItem

    # 规则路径：情绪 + 行业识别 + 检索关键词
    sent, bull, bear, watch, kws = hotspot_ai.rule_analyze(
        "光伏行业迎来政策利好，储能需求爆发", "多家组件厂商订单饱满"
    )
    check("快讯规则: 情绪利好", sent == "利好", sent)
    check("快讯规则: 利好行业含光伏/储能",
          any(x["industry"] == "光伏" for x in bull) and any(x["industry"] == "储能" for x in bull),
          str(bull))
    check("快讯规则: 关键词含光伏/储能", "光伏" in kws and "储能" in kws, str(kws))

    sent2, _b, bear2, _w, kws2 = hotspot_ai.rule_analyze(
        "煤炭价格大跌，煤企利润承压", "多家煤企下调全年产量目标"
    )
    check("快讯规则: 情绪利空", sent2 == "利空", sent2)
    check("快讯规则: 利空行业归入利空", any(x["industry"] == "煤炭" for x in bear2), str(bear2))

    sent3, _b3, _be3, watch3, _k3 = hotspot_ai.rule_analyze("光伏行业召开行业大会", "会议讨论行业规范")
    check("快讯规则: 中性归关注行业", sent3 == "中性" and any(x["industry"] == "光伏" for x in watch3), str(watch3))

    # 关键词提取：去重 + 限长 6
    kws4 = hotspot_ai._extract_keywords(
        "光伏 光伏 储能 半导体 芯片 医药 白酒 券商 军工 算力 机器人 黄金 煤炭 石油 有色"
    )
    check("快讯关键词: 去重限 6", len(kws4) == 6 and kws4.count("光伏") == 1, str(kws4))

    # 股票代码过滤：普通 A 股保留，ETF/LOF/基金剔除（东财 suggest 会把 ETF 标成 A股）
    check("股票过滤: 沪主板/科创板保留", hotspot_ai._is_stock_code("601012", "SH")
          and hotspot_ai._is_stock_code("688981", "SH"))
    check("股票过滤: 深主板/创业板保留", hotspot_ai._is_stock_code("000001", "SZ")
          and hotspot_ai._is_stock_code("300750", "SZ"))
    check("股票过滤: 北交所保留", hotspot_ai._is_stock_code("920002", "BJ"))
    check("股票过滤: 沪 ETF 剔除", not hotspot_ai._is_stock_code("515070", "SH")
          and not hotspot_ai._is_stock_code("510300", "SH"))
    check("股票过滤: 深 ETF/LOF 剔除", not hotspot_ai._is_stock_code("159819", "SZ")
          and not hotspot_ai._is_stock_code("161631", "SZ"))

    # 关联股票解析 + analyze_news 缓存（mock 真实搜索接口，不触网）
    async def _run() -> None:
        orig_search, orig_quotes, orig_avail = (
            hotspot_ai._search_one, hotspot_ai._with_quotes, llm.available,
        )
        llm.available = lambda: False
        try:
            async def fake_search(kw: str) -> tuple:
                if kw == "光伏":
                    return ([SearchItem(code="601012", market="SH", name="隆基绿能"),
                             SearchItem(code="600438", market="SH", name="通威股份")], "东方财富")
                if kw == "储能":
                    return ([SearchItem(code="300274", market="SZ", name="阳光电源"),
                             SearchItem(code="601012", market="SH", name="隆基绿能")], "同花顺")
                return [], ""
            async def _noop(s):
                return s
            hotspot_ai._search_one = fake_search
            hotspot_ai._with_quotes = _noop

            stocks = await hotspot_ai._resolve_stocks(["光伏", "储能"])
            check("关联股: 去重后 3 只", len(stocks) == 3, str(stocks))
            check("关联股: 命中双关键词排前", stocks[0]["code"] == "601012", str(stocks))
            # 命中明细：每个检索词 + 检索来源
            top_matches = stocks[0].get("matches") or []
            check("关联股: 命中明细含双关键词",
                  len(top_matches) == 2
                  and {m["keyword"] for m in top_matches} == {"光伏", "储能"},
                  str(top_matches))
            check("关联股: 命中明细带来源",
                  {m["source"] for m in top_matches} == {"东方财富", "同花顺"},
                  str(top_matches))
            check("关联股: 单关键词命中来源正确",
                  (stocks[1].get("matches") or [{}])[0].get("source") == "东方财富",
                  str(stocks[1].get("matches")))

            calls = {"n": 0}
            async def fake_search2(kw: str) -> tuple:
                calls["n"] += 1
                return [SearchItem(code="601012", market="SH", name="隆基绿能")], "东方财富"
            hotspot_ai._search_one = fake_search2
            r1 = await hotspot_ai.analyze_news("光伏政策利好", "")
            r2 = await hotspot_ai.analyze_news("光伏政策利好", "")
            check("快讯分析: ok 且引擎 rule", r1["ok"] and r1["engine"] == "rule", str(r1)[:120])
            check("快讯分析: 缓存命中不重算", calls["n"] == 1, f"n={calls['n']}")
            check("快讯分析: 关联股为真实代码", r1["stocks"] and r1["stocks"][0]["code"] == "601012",
                  str(r1["stocks"]))
        finally:
            hotspot_ai._search_one = orig_search
            hotspot_ai._with_quotes = orig_quotes
            llm.available = orig_avail
            _cache.drop_prefix("hotspot_ai:")

    asyncio.run(_run())

    # _with_quotes 行情补齐（mock service.get_quotes，防漏 for 推导回归）
    from backend.providers.base import Quote as _Q
    async def _quotes_test() -> None:
        orig_get_quotes = service.get_quotes
        async def fake_get_quotes(keys, force=False):
            return {"601012.SH": _Q(code="601012", market="SH", name="隆基绿能",
                                     price=18.5, change_pct=2.3, board="光伏")}
        service.get_quotes = fake_get_quotes
        try:
            got = await hotspot_ai._with_quotes([
                {"code": "601012", "market": "SH", "name": "隆基绿能", "keywords": ["光伏"]},
            ])
            check("关联股行情: 补齐价格/涨跌/板块", got[0]["price"] == 18.5
                  and got[0]["change_pct"] == 2.3 and got[0]["board"] == "光伏", str(got))
            check("关联股行情: 关联理由为关键词", got[0]["reason"] == "光伏", str(got))
        finally:
            service.get_quotes = orig_get_quotes
    asyncio.run(_quotes_test())

    empty = asyncio.run(hotspot_ai.analyze_news("  "))
    check("快讯分析: 空标题返回错误", empty["ok"] is False, str(empty))


# ------------------------------------------------------------------ 研报解读
def test_reports_interpret() -> None:
    # 规则解读：评级本身即信号 + 标题关键词修正
    buy = reports.rule_interpret({"rating": "买入", "title": "业绩预增，目标价上调", "source": "国海证券"})
    over = reports.rule_interpret({"rating": "增持", "title": "盈利增速抬升", "source": "平安证券"})
    flat = reports.rule_interpret({"rating": "中性", "title": "经营平稳", "source": "华泰证券"})
    sell = reports.rule_interpret({"rating": "减持", "title": "业绩下滑风险", "source": "某券商"})
    check("研报规则解读: 买入=利好", buy["sentiment"] == "利好" and buy["engine"] == "rule", str(buy))
    check("研报规则解读: 增持=利好", over["sentiment"] == "利好", str(over))
    check("研报规则解读: 中性", flat["sentiment"] == "中性", str(flat))
    check("研报规则解读: 减持=利空", sell["sentiment"] == "利空", str(sell))
    # 评级中性但标题强利好词 -> 利好（关键词修正）
    mixed = reports.rule_interpret({"rating": "中性", "title": "业绩超预期大增", "source": "某券商"})
    check("研报规则解读: 关键词修正评级", mixed["sentiment"] == "利好", str(mixed))

    # 近一年评级分布统计：只计入 since 之后的条目
    dist = reports.rating_distribution(
        [
            {"date": "2026-08-01", "rating": "买入"},
            {"date": "2026-07-01", "rating": "增持"},
            {"date": "2026-06-01", "rating": "增持"},
            {"date": "2025-06-01", "rating": "买入"},   # 一年前，应排除
            {"date": "2026-05-01", "rating": ""},        # 无评级 -> --
        ],
        "2025-08-17",
    )
    check("研报评级分布统计", dist == {"买入": 1, "增持": 2, "--": 1}, str(dist))


# ------------------------------------------------------------------ 规则引擎精准化
def _mk_detail(**kw) -> dict:
    d = {
        "quote": {"code": "600000", "name": "浦发银行", "price": 9.0, "prev_close": 9.1, "change_pct": -1.1},
        "boards": [], "kline": [], "ma": [],
        "ma_summary": {"arrangement": "交织", "above_count": 0, "above": [], "below": [], "series": {}},
        "support_resistance": {}, "fund_flow": {"rows": [], "summary": {}},
        "margin": {"rows": [], "summary": {}},
        "status": {"tags": [], "trend": {}},
    }
    d.update(kw)
    return d


def _ma_item(w: int, v: float, slope: str = "上行") -> dict:
    return {"window": w, "value": v, "slope": slope, "position": "站上",
            "deviation_pct": 0.0, "slope_pct": 0.0}


def test_financials() -> None:
    from backend.providers.webparse import parse_financial_html, parse_news_html, parse_report_html

    ths_json = (
        '<p id="main">{"title":["科目\\\\时间",["营业总收入","元",0,false,true],'
        '["营业总收入同比增长率","",0,false,true]],'
        '"report":[["2026-06-30","2026-03-31"],["12.5亿","10亿"],["25%","20%"]]}</p>'
    )
    embedded_rows = parse_financial_html(ths_json, "ths")
    check("同花顺财报内嵌JSON", embedded_rows[0].revenue == 1.25e9 and embedded_rows[0].revenue_yoy == 25, str(embedded_rows))
    linkage = (
        f'<div id="linkagedata">[{{"seq":1,"ctime":{int(time.time())},'
        '"curl":"http://news.10jqka.com.cn/field/test.shtml",'
        '"title":"公司签订重大订单","source":"测试媒体"}]</div>'
    )
    linkage_rows = parse_news_html(linkage, "ths", 30, 5)
    check("同花顺资讯内嵌JSON", linkage_rows[0].title == "公司签订重大订单" and linkage_rows[0].source == "测试媒体", str(linkage_rows))
    eastmoney_json = (
        '<script>var initdata = {"data":[{"title":"业绩增长点评",'
        '"orgSName":"国金证券","publishDate":"2026-08-18 00:00:00.000",'
        '"infoCode":"APTEST001","sRatingName":"增持",'
        '"researcher":"张三"}]};</script>'
    )
    embedded_reports = parse_report_html(eastmoney_json, "eastmoney", 5)
    check("东财研报内嵌JSON", embedded_reports[0].rating == "增持" and embedded_reports[0].source == "国金证券", str(embedded_reports))

    detail = _mk_detail(financials={"source": "ths", "rows": [
        {"period": "2026H1", "date": "2026-06-30", "revenue": 1.25e9, "revenue_yoy": 25,
         "net_profit": 2.5e8, "net_profit_yoy": 25, "roe": 11.2, "debt_ratio": 40},
    ]})
    fb = analysis.rule_based(detail)
    scores = fb["advice"]["scores"]
    check("财报分析: 基本面加分", scores["fundamental"] > 0 and "2026H1" in fb["fundamental"]["period"], str(fb["fundamental"]))
    check("财报分析: 纳入机会", any("基本面偏强" in str(x) for x in fb["risk"]["opportunities"]), str(fb["risk"]["opportunities"]))
    reversed_detail = _mk_detail(financials={"rows": [
        {"period": "2025FY", "date": "2025-12-31", "revenue_yoy": -20, "net_profit_yoy": -20},
        {"period": "2026H1", "date": "2026-06-30", "revenue_yoy": 10, "net_profit_yoy": 10},
    ]})
    reversed_fb = analysis.rule_based(reversed_detail)
    check("财报分析: 按报告日期取最新", reversed_fb["fundamental"]["period"] == "2026H1", str(reversed_fb["fundamental"]))
    payload = analysis.build_payload(detail)
    fin_payload = payload.get("财报数据_季报中报") or {}
    check("财报分析: LLM投喂报告期", fin_payload.get("最新报告期") == "2026H1", str(fin_payload))
    stale_detail = _mk_detail(financials={"source": "ths", "stale": True, "error": "网页暂不可用", "rows": [
        {"period": "2026H1", "date": "2026-06-30", "revenue_yoy": 10, "net_profit_yoy": 10},
    ]})
    stale_payload = analysis.build_payload(stale_detail)["财报数据_季报中报"]
    stale_fb = analysis.rule_based(stale_detail)
    check("财报分析: 缓存状态投喂", "使用上次成功缓存" in stale_payload.get("数据状态", ""), str(stale_payload))
    check("财报分析: 缓存状态提示", "缓存数据" in stale_fb["fundamental"]["summary"], str(stale_fb["fundamental"]))


def test_rule_precision() -> None:
    # 1) 量能确认：放量上涨 +6 / 放量下跌 -6 / 缩量下跌 +3 / 缩量上涨 -3 / 数据不足 0
    def bars(closes: list, vols: list) -> list:
        return [{"date": f"2026-08-{i+1:02d}", "close": c, "volume": v} for i, (c, v) in enumerate(zip(closes, vols))]

    vol_bull = analysis._volume_confirm(bars([9, 9.1, 9.2, 9.3, 9.4, 9.5], [100, 100, 100, 100, 100, 150]))
    check("量能确认: 放量上涨 +6", vol_bull[0] == 6, str(vol_bull))
    vol_bear = analysis._volume_confirm(bars([9.5, 9.4, 9.3, 9.2, 9.1, 9.0], [100, 100, 100, 100, 100, 150]))
    check("量能确认: 放量下跌 -6", vol_bear[0] == -6, str(vol_bear))
    vol_shrink_dn = analysis._volume_confirm(bars([9.5, 9.4, 9.3, 9.2, 9.1, 9.0], [100, 100, 100, 100, 100, 60]))
    check("量能确认: 缩量下跌 +3", vol_shrink_dn[0] == 3, str(vol_shrink_dn))
    vol_shrink_up = analysis._volume_confirm(bars([9, 9.1, 9.2, 9.3, 9.4, 9.5], [100, 100, 100, 100, 100, 60]))
    check("量能确认: 缩量上涨 -3", vol_shrink_up[0] == -3, str(vol_shrink_up))
    check("量能确认: 数据不足 0", analysis._volume_confirm([])[0] == 0)

    # 1.1) 地量股放量阈值上调：近5日均量远低于60日均量时，量比 1.4 不再判放量
    # 构造 60 根 bar：前 55 根均量 1000（高位），近 5 根均量 100（地量），
    # 最后一日量 140（相对近5日均量的 1.4 倍，但绝对量仍远低于历史均值）
    def bars60() -> list:
        rows = []
        for i in range(60):
            # 前 54 根高位量 1000，近 5 根地量 100，最后一天 140
            vol = 1000 if i < 54 else (100 if i < 59 else 140)
            close = 9.0 + (i - 54) * 0.2  # 近 5 日上涨
            rows.append({"date": f"2026-{i//30+1:02d}-{i%30+1:02d}", "close": close, "volume": vol})
        return rows

    # 近5日均量 = (100*5)/5 = 100；60日均量 = (1000*54 + 100*5)/60 ≈ 908
    # 100 < 908*0.7=636 → 地量股，阈值上调到 1.5；量比 140/100=1.4 不达标 → 0
    check("量能确认: 地量股量比1.4不判放量", analysis._volume_confirm(bars60())[0] == 0,
          str(analysis._volume_confirm(bars60())))

    # 2) 乖离修正：价格超 MA20 8% -> 超买风险进 risks + tech 扣分
    detail_over = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 10.0, "prev_close": 9.9},
        ma=[_ma_item(5, 9.2), _ma_item(10, 9.1), _ma_item(20, 9.0), _ma_item(60, 8.8)],
    )
    fb_over = analysis.rule_based(detail_over)
    check("乖离修正: 超买进风险面", any("超买" in r for r in fb_over["risk"]["risks"]), str(fb_over["risk"]["risks"]))
    detail_low = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 8.0, "prev_close": 8.1},
        ma=[_ma_item(5, 8.8), _ma_item(10, 8.9), _ma_item(20, 9.0), _ma_item(60, 9.1)],
    )
    fb_low = analysis.rule_based(detail_low)
    check("乖离修正: 超卖进机会面", any("超卖" in o for o in fb_low["risk"]["opportunities"]), str(fb_low["risk"]["opportunities"]))

    # 3) 三维分面明细输出（含当日盘口分项）
    check("分面分数输出", set(fb_over["advice"]["scores"].keys()) == {"tech", "capital", "news", "fundamental", "intraday", "total"},
          str(fb_over["advice"]["scores"]))

    # 4) 信号冲突降档：技术/资金偏空 + 消息强多 -> signal=conflict、降档、置信度压低
    detail_cf = _mk_detail(
        ma=[_ma_item(5, 9.2, "持平"), _ma_item(10, 9.1, "持平"), _ma_item(20, 9.0, "持平"), _ma_item(60, 8.8, "持平")],
        ma_summary={"arrangement": "交织", "above_count": 2, "above": ["MA5", "MA10"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": -1e8, "main_last": -1e8, "main_last5": 0, "streak": 0, "streak_dir": ""}},
        status={"tags": [], "trend": {"chg_20d": -3}},
    )
    news_cf = [{"title": "重大利好" + str(i), "date": f"2026-08-{i+1:02d}",
                 "interpretation": {"sentiment": "利好", "impact": "高", "summary": "s"}} for i in range(2)]
    reports_cf = [{"rating": "买入", "title": "业绩预增" + str(i), "date": f"2026-08-{i+1:02d}",
                   "interpretation": {"sentiment": "利好", "impact": "高", "summary": "s"}} for i in range(2)]
    fb_cf = analysis.rule_based(detail_cf, news_cf, reports_cf)
    check("信号冲突标记", fb_cf["advice"]["signal"] == "conflict", str(fb_cf["advice"]["signal"]))
    check("冲突时提示背离", "背离" in fb_cf["advice"]["reason"], fb_cf["advice"]["reason"])
    check("冲突时不清仓", fb_cf["advice"]["action"] != "清仓离场", fb_cf["advice"]["action"])
    check("冲突时置信度压低", fb_cf["advice"]["confidence"] < 60, str(fb_cf["advice"]["confidence"]))
    # 5) 信号一致增强：技术/资金/消息同向 -> signal=aligned 且置信度上修
    detail_al = _mk_detail(
        ma=[_ma_item(5, 8.6, "上行"), _ma_item(10, 8.5, "上行"), _ma_item(20, 8.4, "上行"), _ma_item(60, 8.2, "上行")],
        ma_summary={"arrangement": "多头排列", "above_count": 4, "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 3e8, "main_last": 1e8, "main_last5": 1e8, "streak": 4, "streak_dir": "流入"}},
    )
    fb_al = analysis.rule_based(detail_al, news_cf, reports_cf)
    check("信号一致标记", fb_al["advice"]["signal"] == "aligned", str(fb_al["advice"]["signal"]))
    check("一致时提示共振", "共振" in fb_al["advice"]["reason"], fb_al["advice"]["reason"])
    check("一致时置信度上修", fb_al["advice"]["confidence"] > 80, str(fb_al["advice"]["confidence"]))

    # 5.4) 资金面当日优先：当日流出但 30 日累计流入 -> 判定偏空（与详情页展示一致）
    detail_daily = _mk_detail(
        ma=[_ma_item(5, 9.2, "持平"), _ma_item(10, 9.1, "持平"), _ma_item(20, 9.0, "持平"), _ma_item(60, 8.8, "持平")],
        ma_summary={"arrangement": "交织", "above_count": 2, "above": ["MA5", "MA10"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 15e8, "main_last": -2.45e8, "main_last5": 0.6e8, "streak": 1, "streak_dir": "流出", "fresh": True, "last_date": "2026-08-18"}},
    )
    fb_daily = analysis.rule_based(detail_daily)
    check("资金面当日优先: 当日流出进风险面",
          any("当日主力净流出" in (r if isinstance(r, str) else r.get("text", "")) for r in fb_daily["risk"]["risks"]),
          str(fb_daily["risk"]["risks"]))
    check("资金面当日优先: 不出现累计流入机会",
          not any("近30日主力累计净流入" in (o if isinstance(o, str) else o.get("text", "")) for o in fb_daily["risk"]["opportunities"]),
          str(fb_daily["risk"]["opportunities"]))

    # 5.45) 当日资金流向未发布（16点前，最后一行=昨天）：判定退回近5日口径并标注日期
    detail_nf = _mk_detail(
        ma=[_ma_item(5, 9.2, "持平"), _ma_item(10, 9.1, "持平"), _ma_item(20, 9.0, "持平"), _ma_item(60, 8.8, "持平")],
        ma_summary={"arrangement": "交织", "above_count": 2, "above": ["MA5", "MA10"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 15e8, "main_last": -2.45e8, "main_last5": -0.8e8, "streak": 2, "streak_dir": "流出", "fresh": False, "last_date": "2026-08-17"}},
    )
    fb_nf = analysis.rule_based(detail_nf)
    check("资金未发布: 退回近5日口径",
          any("最近交易日（2026-08-17）" in (r if isinstance(r, str) else r.get("text", "")) for r in fb_nf["risk"]["risks"]),
          str(fb_nf["risk"]["risks"]))
    check("资金未发布: 不把昨日当当日",
          not any("当日主力" in (x if isinstance(x, str) else x.get("text", "")) for x in fb_nf["risk"]["opportunities"] + fb_nf["risk"]["risks"]),
          str(fb_nf["risk"]["risks"]))

    # 5.5) 当日实时盘口数据：趋势/资金段含 intraday，盘口分项计入技术面
    detail_intra = _mk_detail(
        quote={
            "code": "600000", "name": "浦发银行", "price": 9.55, "prev_close": 9.1,
            "change": 0.45, "change_pct": 4.95, "open": 9.2, "high": 9.6, "low": 9.0,
            "volume": 5e7, "amount": 4.8e8, "turnover": 3.2, "volume_ratio": 2.5,
        },
    )
    fb_intra = analysis.rule_based(detail_intra)
    check("当日盘中数据进趋势段", "当日振幅" in fb_intra["trend"].get("intraday", ""), fb_intra["trend"].get("intraday", ""))
    check("盘中位置/量比/换手", "区间" in fb_intra["trend"]["intraday"] and "量比" in fb_intra["trend"]["intraday"]
          and "换手率" in fb_intra["trend"]["intraday"], fb_intra["trend"]["intraday"])
    check("当日资金活跃进资金段", "当日成交额" in fb_intra["capital"].get("intraday", ""), fb_intra["capital"].get("intraday", ""))
    # 高位上涨 + 放量 -> 盘口分项为正、进机会面
    intra_pts = fb_intra["advice"]["scores"]["intraday"]
    check("盘口分项计入技术面", intra_pts > 0, f"intraday={intra_pts}")
    check("盘口分项写进依据", f"盘口 {intra_pts:+d} 分" in fb_intra["advice"]["reason"], fb_intra["advice"]["reason"])
    def _o_txt(x):
        return x if isinstance(x, str) else x.get("text", "")

    check("高位强势提示进机会面", any("高位" in _o_txt(o) and "强势" in _o_txt(o) for o in fb_intra["risk"]["opportunities"]),
          str(fb_intra["risk"]["opportunities"]))
    # 高位回落转跌 -> 盘口分项为负、进风险面（现价贴近当日高点但较昨收下跌）
    detail_intra_bear = _mk_detail(
        quote={
            "code": "600000", "name": "浦发银行", "price": 9.85, "prev_close": 10.0,
            "change": -0.15, "change_pct": -1.5, "open": 9.8, "high": 9.9, "low": 8.9,
            "volume": 5e7, "amount": 4.8e8, "turnover": 3.2, "volume_ratio": 1.2,
        },
    )
    fb_intra_bear = analysis.rule_based(detail_intra_bear)
    intra_bear = fb_intra_bear["advice"]["scores"]["intraday"]
    check("高位回落盘口分项为负", intra_bear < 0, f"intraday={intra_bear}")
    check("冲高回落提示进风险面", any("回落" in _o_txt(r) for r in fb_intra_bear["risk"]["risks"]),
          str(fb_intra_bear["risk"]["risks"]))
    # 数据缺失时 intraday 字段仍存在且为 0、不报错
    fb_no_intra = analysis.rule_based(_mk_detail())
    check("无盘口数据时字段兜底", "intraday" in fb_no_intra["trend"] and "intraday" in fb_no_intra["capital"]
          and fb_no_intra["advice"]["scores"]["intraday"] == 0,
          str(fb_no_intra["trend"].get("intraday")))

    # 5.6) 盘口分项四象限 + 量比/振幅/换手修正
    def _q(**kw) -> dict:
        base = {"code": "600000", "price": 9.4, "prev_close": 9.1, "change_pct": 3.3,
                "open": 9.1, "high": 9.5, "low": 9.0, "volume_ratio": 1.0, "turnover": 2.0}
        base.update(kw)
        return base

    hi_up = analysis._intraday_score(_q(price=9.45, change_pct=3.8))      # 位置 90% + 涨
    hi_dn = analysis._intraday_score(_q(price=9.45, change_pct=-1.1))     # 位置 90% + 跌
    lo_dn = analysis._intraday_score(_q(price=9.05, change_pct=-2.2))     # 位置 10% + 跌
    lo_up = analysis._intraday_score(_q(price=9.05, change_pct=0.6))      # 位置 10% + 涨
    check("盘口四象限: 高位涨 加分", hi_up[0] > 0, str(hi_up))
    check("盘口四象限: 高位跌 减分", hi_dn[0] < 0, str(hi_dn))
    check("盘口四象限: 低位跌 减分", lo_dn[0] < 0, str(lo_dn))
    check("盘口四象限: 低位涨 加分", lo_up[0] > 0, str(lo_up))
    check("盘口: 放量上涨加分", analysis._intraday_score(_q(volume_ratio=2.5))[0] > 3)
    check("盘口: 放量下跌减分", analysis._intraday_score(_q(volume_ratio=2.5, change_pct=-3.3))[0] < -3)
    check("盘口: 缩量削弱信号", analysis._intraday_score(_q(volume_ratio=0.5))[0] < analysis._intraday_score(_q(volume_ratio=1.0))[0])
    check("盘口: 振幅大减分", analysis._intraday_score(_q(high=9.9, low=8.6))[0] < 0)
    check("盘口: 换手极高下跌更空", analysis._intraday_score(_q(turnover=12.0, change_pct=-3.3))[0] < 0)
    check("盘口: 缺数据为 0", analysis._intraday_score({"code": "600000", "price": 9.0})[0] == 0)

    # 5.7) 盘口信号历史命中率强度标注（_annotate_intraday 拆分 + rule_based 输出）
    ann = analysis._annotate_intraday(
        "现价自当日高位回落（92%）转跌，短线抛压显现；量比 2.30 放量下挫，抛压集中释放；"
        "换手率 0.5% 过低，交投清淡"
    )
    check("信号标注: 拆分 3 条", len(ann) == 3, str(ann))
    check("信号标注: 冲高回落=高", ann[0]["strength"] == "高" and "盘中57.1%" in ann[0]["hit"], str(ann[0]))
    check("信号标注: 放量下挫=中样本不足", ann[1]["strength"] == "中" and "样本不足" in ann[1]["hit"], str(ann[1]))
    check("信号标注: 交投清淡=高", ann[2]["strength"] == "高" and "54.3%" in ann[2]["hit"], str(ann[2]))
    check("信号标注: 未匹配子句不标", analysis._annotate_intraday("当日成交额 4.21 亿元，市场交投正常")[0]["strength"] == "",
          str(analysis._annotate_intraday("当日成交额 4.21 亿元，市场交投正常")))
    # 信号置信度：由支撑样本数经 utils.confidence 折算（与自检/回测口径统一）
    ann_c = analysis._annotate_intraday(
        "现价自当日高位回落（92%）转跌，短线抛压显现；换手率 0.5% 过低，交投清淡"
    )
    check("信号置信度: 字段存在", all("confidence" in a for a in ann_c), str(ann_c))
    check("信号置信度: 高样本=高置信", ann_c[1]["confidence"]["level"] == "high"
          and ann_c[1]["confidence"]["label"] == "高", str(ann_c[1]))
    check("信号置信度: 少样本=低置信且与强度独立", ann_c[0]["strength"] == "高" and ann_c[0]["confidence"]["level"] == "low",
          str(ann_c[0]))
    # 口径统一：analysis 标注与 utils.confidence 同函数
    from backend.utils import confidence as _uconf
    check("信号置信度: 口径与 utils 一致",
          ann_c[1]["confidence"] == _uconf(249), str((ann_c[1]["confidence"], _uconf(249))))
    # 盘口机会/风险条目为 dict 结构（带强度），非盘口条目保持字符串
    d_sig = _mk_detail(quote={
        "code": "600000", "name": "浦发银行", "price": 9.85, "prev_close": 10.0,
        "change": -0.15, "change_pct": -1.5, "open": 9.8, "high": 9.9, "low": 8.9,
        "volume": 5e7, "amount": 4.8e8, "turnover": 0.5, "volume_ratio": 2.3,
    })
    # LLM 投喂：payload 含盘口信号可靠性段（强度/命中率/置信度）
    _payload_sig = analysis.build_payload(d_sig).get("盘口信号可靠性_当日") or []
    check("投喂盘口: 段存在且有信号", len(_payload_sig) >= 2, str(_payload_sig)[:200])
    check("投喂盘口: 含强度/命中率/置信度",
          all({"信号", "历史强度", "历史命中率", "置信度"} <= set(s.keys()) for s in _payload_sig),
          str(_payload_sig[0]) if _payload_sig else "无")

    # 7) MACD/KDJ：计算、投喂与规则评分
    from backend.indicators import compute_oscillators
    _obars = []
    _p = 10.0
    for _i in range(60):
        _p += (-0.05 if _i < 30 else 0.08)
        _obars.append(Bar(date=f"2026-08-{_i % 28 + 1:02d}", open=_p - 0.05, close=_p,
                          high=_p + 0.3, low=_p - 0.3, volume=1e6))
    _osc = compute_oscillators(_obars)
    check("MACD/KDJ: 计算产出", _osc["macd"].get("dif") is not None and _osc["kdj"].get("k") is not None,
          str({k: v for k, v in _osc["macd"].items() if k != "series"}))
    check("MACD/KDJ: 数据不足兜底", compute_oscillators(_obars[:20])["macd"] == {} and
          compute_oscillators(_obars[:20])["kdj"] == {}, str(compute_oscillators(_obars[:20])))
    # 摆动指标仅分析展示，不参与评分与结论（当前市场行情下已不适合作为决策数据）
    _d_osc = _mk_detail(quote={"code": "600000", "name": "浦发银行", "price": 10.0,
                                "prev_close": 9.9, "change_pct": 1.0, "high": 10.2,
                                "low": 9.8, "open": 9.95},
                         oscillators=_osc)
    _d_osc2 = _mk_detail(quote={"code": "600000", "name": "浦发银行", "price": 10.0,
                                 "prev_close": 9.9, "change_pct": 1.0, "high": 10.2,
                                 "low": 9.8, "open": 9.95},
                          oscillators={"macd": {"cross": "死叉", "dif": -0.1, "dea": 0.1,
                                                 "hist_trend": "绿柱放大"},
                                       "kdj": {"cross": "死叉", "k": 20, "d": 40, "j": -10, "zone": "超卖"}})
    _fb_osc = analysis.rule_based(_d_osc)
    _fb_osc2 = analysis.rule_based(_d_osc2)
    check("MACD/KDJ: 趋势段含指标行", "MACD" in _fb_osc["trend"].get("oscillators", "") and "KDJ" in _fb_osc["trend"]["oscillators"],
          _fb_osc["trend"].get("oscillators", ""))
    check("MACD/KDJ: 不参与评分",
          _fb_osc["advice"]["scores"]["tech"] == _fb_osc2["advice"]["scores"]["tech"],
          f"金叉 {_fb_osc['advice']['scores']['tech']} vs 死叉 {_fb_osc2['advice']['scores']['tech']}")
    check("MACD/KDJ: 不进机会/风险",
          not any("MACD" in str(x) or "KDJ" in str(x) for x in _fb_osc["risk"]["opportunities"] + _fb_osc["risk"]["risks"]),
          str(_fb_osc["risk"]["opportunities"])[:150])
    check("MACD/KDJ: 投喂段存在", "MACD" in analysis.build_payload(_d_osc)["技术指标_MACD_KDJ"],
          str(analysis.build_payload(_d_osc)["技术指标_MACD_KDJ"])[:200])
    fb_sig = analysis.rule_based(d_sig)
    sig_risks = [x for x in fb_sig["risk"]["risks"] if isinstance(x, dict)]
    check("规则输出: 风险含 dict 标注条目", len(sig_risks) >= 2, str(fb_sig["risk"]["risks"]))
    check("规则输出: dict 条目含 strength/hit",
          all("strength" in x and "hit" in x and "text" in x for x in sig_risks), str(sig_risks))
    check("规则输出: 非盘口条目仍为字符串",
          any(isinstance(x, str) for x in fb_sig["risk"]["risks"]), str(fb_sig["risk"]["risks"]))

    # 6) 三维权重：clamp 越界 + 权重影响分面分
    from backend import scorecfg
    check("权重 clamp 下限", scorecfg._clamp(0.01, 1.0) == 0.2)
    check("权重 clamp 上限", scorecfg._clamp(9.9, 1.0) == 3.0)
    check("权重 clamp 正常值", scorecfg._clamp(1.5, 1.0) == 1.5)
    # 默认权重 1.0 时 score 与分面和一致
    detail_w = _mk_detail(
        ma=[_ma_item(5, 8.6, "上行"), _ma_item(10, 8.5, "上行"), _ma_item(20, 8.4, "上行"), _ma_item(60, 8.2, "上行")],
        ma_summary={"arrangement": "多头排列", "above_count": 4, "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 3e8, "main_last": 1e8, "main_last5": 1e8, "streak": 4, "streak_dir": "流入"}},
    )
    fb_w = analysis.rule_based(detail_w)
    s = fb_w["advice"]["scores"]
    check("默认权重下总分=分面和", abs(s["tech"] + s["capital"] + s["news"] - s["total"]) < 0.05, str(s))
    check("权重字段输出", fb_w["advice"]["weights"] == {"tech": 1.0, "capital": 1.0, "news": 1.0}, str(fb_w["advice"]["weights"]))


# ------------------------------------------------------------------ LLM 输出兜底校验
# _sanitize 是 LLM 路径唯一的安全网：缺失/越界/非枚举值都用规则引擎补齐。
# 这是「模型幻觉」与「JSON 不规范」的最后一道防线，任何分支失效都会让坏值流到前端。
def test_ai_sanitize() -> None:
    # 构造一个「真实可用」的 fallback：作为兜底值参考基准
    detail_ok = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 10.0, "prev_close": 9.9,
               "change_pct": 1.01, "open": 9.95, "high": 10.1, "low": 9.85},
        ma=[_ma_item(5, 9.8), _ma_item(10, 9.7), _ma_item(20, 9.5), _ma_item(60, 9.0)],
        ma_summary={"arrangement": "多头排列", "above_count": 4,
                    "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 1e8, "main_last": 0.5e8,
                                          "main_last5": 0.3e8, "streak": 3, "streak_dir": "流入"}},
    )
    fb = analysis.rule_based(detail_ok)
    price = 10.0  # 与 quote.price 对齐，用于价位越界判断

    # 1) LLM 返回完整且合规：直接通过（无回退）
    out_ok = analysis._sanitize(fb, fb, price)
    check("兜底: 完整合规输入直通", out_ok["advice"]["action"] == fb["advice"]["action"],
          str(out_ok["advice"]["action"]))
    check("兜底: 价位保留原值 2 位", out_ok["advice"]["support"] == round(fb["advice"]["support"], 2),
          str(out_ok["advice"]["support"]))

    # 2) action 子串模糊匹配：「继续持有」含「持有」→ 命中「持有观望」（ACTIONS = [积极持仓/加仓, 持有观望, 减仓规避, 清仓离场]）
    llm_substr = {**fb, "advice": {**fb["advice"], "action": "继续持有"}}
    out_substr = analysis._sanitize(llm_substr, fb, price)
    check("兜底: action 子串匹配→持有观望", out_substr["advice"]["action"] == "持有观望",
          str(out_substr["advice"]["action"]))
    check("兜底: 子串匹配无 action_note", "action_note" not in out_substr["advice"], str(out_substr["advice"]))

    # 2.1) action 完全不在 4 选 1（前 2 字不在 ACTIONS 任何项里）：回退规则值 + action_note 标注
    # 「减持规避」含「减持」但 ACTIONS 里只有「减仓」 → 模糊匹配失败
    llm_no_match = {**fb, "advice": {**fb["advice"], "action": "减持规避"}}
    out_nm = analysis._sanitize(llm_no_match, fb, price)
    check("兜底: 完全不合规action回退规则", out_nm["advice"]["action"] == fb["advice"]["action"],
          str(out_nm["advice"]["action"]))
    check("兜底: action_note 标注模型输出",
          "action_note" in out_nm["advice"] and "减持规避" in out_nm["advice"]["action_note"],
          str(out_nm["advice"].get("action_note")))

    # 3) 价位越界：support=1.0（现价 10 的 10%）→ 回退；resistance=50（5 倍现价）→ 回退
    llm_bad_levels = {**fb, "advice": {
        **fb["advice"],
        "support": 1.0, "resistance": 50.0, "stop_loss": 0, "take_profit": -5,
    }}
    out_lvl = analysis._sanitize(llm_bad_levels, fb, price)
    # 现价 10，±50% 区间 = [5, 15]
    check("兜底: support=1.0越界回退", out_lvl["advice"]["support"] == fb["advice"]["support"], str(out_lvl["advice"]["support"]))
    check("兜底: resistance=50.0越界回退", out_lvl["advice"]["resistance"] == fb["advice"]["resistance"], str(out_lvl["advice"]["resistance"]))
    check("兜底: take_profit=-5越界回退", out_lvl["advice"]["take_profit"] == fb["advice"]["take_profit"], str(out_lvl["advice"]["take_profit"]))
    check("兜底: stop_loss=0越界回退", out_lvl["advice"]["stop_loss"] == fb["advice"]["stop_loss"], str(out_lvl["advice"]["stop_loss"]))

    # 3.0) 价位在合理区间：应保留并保留 2 位小数
    fb_support = fb["advice"]["support"]
    fb_resistance = fb["advice"]["resistance"]
    out_ok_levels = analysis._sanitize({
        **fb,
        "advice": {**fb["advice"], "support": 9.5, "resistance": 10.5, "stop_loss": 9.5, "take_profit": 10.5},
    }, fb, price)
    check("兜底: support=9.5保留(2位)", out_ok_levels["advice"]["support"] == 9.5, str(out_ok_levels["advice"]["support"]))
    check("兜底: resistance=10.5保留(2位)", out_ok_levels["advice"]["resistance"] == 10.5, str(out_ok_levels["advice"]["resistance"]))
    check("兜底: take_profit=10.5保留(2位)", out_ok_levels["advice"]["take_profit"] == 10.5, str(out_ok_levels["advice"]["take_profit"]))

    # 3.1) 价位非数字（字符串）：回退规则值
    llm_str_level = {**fb, "advice": {**fb["advice"], "support": "约九块五", "resistance": None}}
    out_str = analysis._sanitize(llm_str_level, fb, price)
    check("兜底: 非数字价位回退", out_str["advice"]["support"] == fb["advice"]["support"], str(out_str["advice"]["support"]))
    check("兜底: None价位回退", out_str["advice"]["resistance"] == fb["advice"]["resistance"], str(out_str["advice"]["resistance"]))

    # 3.2) 价位 0 或负数：视为非法 → 回退
    llm_zero = {**fb, "advice": {**fb["advice"], "stop_loss": 0, "take_profit": -5}}
    out_zero = analysis._sanitize(llm_zero, fb, price)
    check("兜底: stop_loss=0回退", out_zero["advice"]["stop_loss"] == fb["advice"]["stop_loss"], str(out_zero["advice"]["stop_loss"]))
    check("兜底: take_profit=-5回退", out_zero["advice"]["take_profit"] == fb["advice"]["take_profit"], str(out_zero["advice"]["take_profit"]))

    # 4) confidence 越界：150 → 截到 100；-10 → 截到 0；"abc" → 默认 70
    llm_conf_high = {**fb, "advice": {**fb["advice"], "confidence": 150}}
    out_ch = analysis._sanitize(llm_conf_high, fb, price)
    check("兜底: confidence=150截到100", out_ch["advice"]["confidence"] == 100, str(out_ch["advice"]["confidence"]))
    llm_conf_low = {**fb, "advice": {**fb["advice"], "confidence": -10}}
    out_cl = analysis._sanitize(llm_conf_low, fb, price)
    check("兜底: confidence=-10截到0", out_cl["advice"]["confidence"] == 0, str(out_cl["advice"]["confidence"]))
    llm_conf_str = {**fb, "advice": {**fb["advice"], "confidence": "abc"}}
    out_cs = analysis._sanitize(llm_conf_str, fb, price)
    check("兜底: confidence=字符串默认70", out_cs["advice"]["confidence"] == 70, str(out_cs["advice"]["confidence"]))

    # 5) risk.opportunities/risks 非列表（字符串/空）：回退规则值；超过 5 条截断
    llm_risk_str = {**fb, "risk": {**fb["risk"], "opportunities": "多头格局", "risks": []}}
    out_rs = analysis._sanitize(llm_risk_str, fb, price)
    check("兜底: opportunities字符串→列表", isinstance(out_rs["risk"]["opportunities"], list)
          and out_rs["risk"]["opportunities"] == ["多头格局"], str(out_rs["risk"]["opportunities"]))
    check("兜底: risks空列表回退", out_rs["risk"]["risks"] == fb["risk"]["risks"], str(out_rs["risk"]["risks"]))

    # 5.1) opportunities 超过 5 条：截断到 5
    llm_too_many = {**fb, "risk": {**fb["risk"], "opportunities": [f"机会{i}" for i in range(8)]}}
    out_tm = analysis._sanitize(llm_too_many, fb, price)
    check("兜底: opportunities>5截到5", len(out_tm["risk"]["opportunities"]) == 5,
          str(out_tm["risk"]["opportunities"]))

    # 6) 各 section 顶层字段缺失：用 fallback 补（不全空）
    llm_partial = {"advice": fb["advice"], "risk": {**fb["risk"]}}
    # trend/capital/fundamental 缺失，应整体用 fallback
    out_partial = analysis._sanitize(llm_partial, fb, price)
    check("兜底: 缺失trend/capital/fundamental用fallback", out_partial["trend"] == fb["trend"]
          and out_partial["capital"] == fb["capital"] and out_partial["fundamental"] == fb["fundamental"],
          str({k: out_partial.get(k) for k in ("trend", "capital", "fundamental")}))

    # 6.1) trend.summary 是空串：视为空值，用 fallback 的 summary 补
    llm_empty_summary = {**fb, "trend": {**fb["trend"], "summary": ""}}
    out_es = analysis._sanitize(llm_empty_summary, fb, price)
    check("兜底: 空字符串summary用fallback补", out_es["trend"]["summary"] == fb["trend"]["summary"],
          str(out_es["trend"]["summary"]))

    # 7) 低置信度撤销激进建议：confidence<50 且 action 为积极/清仓 → 撤销为持有观望
    llm_agg_low = {**fb, "advice": {**fb["advice"], "action": "积极持仓/加仓", "confidence": 40}}
    out_al = analysis._sanitize(llm_agg_low, fb, price)
    check("兜底: 低置信度撤销激进加仓", out_al["advice"]["action"] == "持有观望",
          str(out_al["advice"]["action"]))
    check("兜底: 撤销时标注 action_note",
          "action_note" in out_al["advice"] and "置信度过低" in out_al["advice"]["action_note"],
          str(out_al["advice"].get("action_note")))
    llm_liq_low = {**fb, "advice": {**fb["advice"], "action": "清仓离场", "confidence": 30}}
    out_ll = analysis._sanitize(llm_liq_low, fb, price)
    check("兜底: 低置信度撤销清仓", out_ll["advice"]["action"] == "持有观望",
          str(out_ll["advice"]["action"]))

    # 7.1) 非激进 action 低置信度不撤销；高置信度激进不撤销
    llm_hold_low = {**fb, "advice": {**fb["advice"], "action": "持有观望", "confidence": 40}}
    out_hl = analysis._sanitize(llm_hold_low, fb, price)
    check("兜底: 低置信度持有观望不撤销", out_hl["advice"]["action"] == "持有观望", str(out_hl["advice"]["action"]))
    llm_agg_high = {**fb, "advice": {**fb["advice"], "action": "积极持仓/加仓", "confidence": 80}}
    out_ah = analysis._sanitize(llm_agg_high, fb, price)
    check("兜底: 高置信度激进不撤销", out_ah["advice"]["action"] == "积极持仓/加仓", str(out_ah["advice"]["action"]))

    # 8) risk 列表按含数字条目优先：泛泛而谈的空话排后
    llm_risk_order = {**fb, "risk": {
        **fb["risk"],
        "opportunities": ["重大利好催化", "营收同比增长20%", "订单饱满"],
    }}
    out_ro = analysis._sanitize(llm_risk_order, fb, price)
    check("兜底: 含数字条目排前", "20%" in out_ro["risk"]["opportunities"][0],
          str(out_ro["risk"]["opportunities"]))


# ------------------------------------------------------------------ LLM 投喂质量
# build_payload 是喂给 LLM 的唯一数据源，字段完整性与准确性直接决定模型结论上限。
def test_payload_quality() -> None:
    # 构造 60 根 K 线：volume 前 30 根 1e8 股、后 30 根 5e7 股，均量可验证
    kline = [
        {"date": f"2026-{i // 30 + 1:02d}-{i % 30 + 1:02d}",
         "close": 9.0 + i * 0.01, "volume": 1e8 if i < 30 else 5e7}
        for i in range(60)
    ]
    detail = _mk_detail(
        quote={"code": "600000", "name": "浦发银行", "price": 9.6, "prev_close": 9.5,
               "change": 0.1, "change_pct": 1.05, "open": 9.5, "high": 9.7, "low": 9.4,
               "volume": 5e6, "amount": 4.8e8, "turnover": 1.0, "volume_ratio": 1.2},
        kline=kline,
        ma=[_ma_item(5, 9.5), _ma_item(10, 9.4), _ma_item(20, 9.3), _ma_item(60, 9.0)],
        ma_summary={"arrangement": "多头排列", "above_count": 4,
                    "above": ["MA5", "MA10", "MA20", "MA60"], "below": [], "series": {}},
        fund_flow={"rows": [], "summary": {"main_total": 1e8, "main_last": 0.5e8,
                                          "main_last5": 0.3e8, "streak": 3, "streak_dir": "流入"}},
        support_resistance={"support": 9.4, "resistance": 9.8, "state": "突破"},
    )
    payload = analysis.build_payload(detail, None, None)

    # 1) 近 60 日均量字段存在且值正确：
    #    sum = 1e8*30 + 5e7*30 = 4.5e9 股；avg = 4.5e9/60 = 7.5e7 股
    #    万手 = 7.5e7 / 1e6 = 75.0
    base = payload["基础数据"]
    check("投喂: 基础数据含近60日均量", "近60日均量_万手" in base, str(list(base.keys())))
    check("投喂: 近60日均量数值正确",
          base["近60日均量_万手"] is not None and abs(base["近60日均量_万手"] - 75.0) < 0.01,
          str(base["近60日均量_万手"]))

    # 2) 近 60 日收盘序列存在且长度 60
    ma_block = payload["均线技术数据"]
    check("投喂: 含近60日收盘序列", "近60日收盘序列" in ma_block, str(list(ma_block.keys())))
    seq60 = ma_block["近60日收盘序列"]
    check("投喂: 近60日序列长度=60", len(seq60) == 60, f"len={len(seq60)}")

    # 3) 近 30 日序列仍在且长度为 30（未误删旧字段）
    check("投喂: 近30日序列仍存在且长度=30",
          len(ma_block["近30日收盘序列"]) == 30, str(len(ma_block["近30日收盘序列"])))


# ------------------------------------------------------------------ K 线滞后判定
def test_kline_stale() -> None:
    from datetime import datetime
    from backend.utils import kline_is_stale

    # 固定注入时间，避免 CI 在任意时刻运行时判据漂移
    mon_close = datetime(2026, 8, 17, 16, 0)   # 周一收盘后
    # 周一收盘：K线停在周五应判滞后，含今天则正常
    check("K线滞后判定: 周一收盘缺周五->周一(停08-14)", kline_is_stale("2026-08-14", mon_close) is True)
    check("K线滞后判定: 周一收盘已含今天(08-17)", kline_is_stale("2026-08-17", mon_close) is False)
    check("K线滞后判定: 盘中不判定", kline_is_stale("2026-08-14", datetime(2026, 8, 17, 11, 0)) is False)
    check("K线滞后判定: 空日期不滞后", kline_is_stale("", mon_close) is False)
    check("K线滞后判定: 非法日期不滞后", kline_is_stale("abc", mon_close) is False)


# ------------------------------------------------------------------ 缓存

def test_cache() -> None:
    async def run() -> None:
        c = cache.TTLCache()
        calls = {"n": 0}

        async def loader() -> str:
            calls["n"] += 1
            await asyncio.sleep(0.01)
            return "v"

        await asyncio.gather(*[c.get_or_set("k", 60, loader) for _ in range(5)])
        check("缓存单飞（并发只加载一次）", calls["n"] == 1, f"n={calls['n']}")
        check("缓存锁表回收", not c._locks)
        # 过期后应重新加载
        c._data["k"] = (0.0, "stale")
        await c.get_or_set("k", 60, loader)
        check("缓存过期重载", calls["n"] == 2, f"n={calls['n']}")
        # 上限淘汰后不超限
        for i in range(cache.MAX_ENTRIES + 50):
            c.put(f"bulk{i}", i, 100.0)
        check("缓存条目硬上限", len(c._data) <= cache.MAX_ENTRIES, f"n={len(c._data)}")

    asyncio.run(run())


# ------------------------------------------------------------------ AI 每股票单飞锁
def test_ai_lock() -> None:
    async def run() -> None:
        calls = {"n": 0}
        store: dict[str, str] = {}

        async def work() -> str:
            if "report" in store:          # 等锁后二次检查缓存
                return store["report"]
            calls["n"] += 1                 # 真实计算只应执行一次
            await asyncio.sleep(0.01)
            store["report"] = "done"
            return store["report"]

        r = await asyncio.gather(*[api._with_ai_lock("600000", work) for _ in range(5)])
        check("AI 每股票单飞", calls["n"] == 1 and all(x == "done" for x in r), f"n={calls['n']}")
        check("AI 锁表回收", "600000" not in api._ai_locks)

    asyncio.run(run())


def test_ai_cache_freshness() -> None:
    """AI 当日缓存时效：过期快照必须作废重建（保证点击分析时是最新实时数据）。"""
    from datetime import datetime, timedelta

    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    fresh = api._cache_fresh((now - timedelta(seconds=30)).strftime(fmt))
    check("AI 缓存时效: 30s 前快照仍新鲜", fresh is True, f"fresh={fresh}")
    # 2 小时前快照超过盘后 1h TTL，任何时段（盘中 120s / 盘后 1h）都必过期
    stale = api._cache_fresh((now - timedelta(hours=2)).strftime(fmt))
    check("AI 缓存时效: 2 小时前快照过期作废", stale is False, f"stale={stale}")
    bad = api._cache_fresh("not-a-date")
    check("AI 缓存时效: 坏格式快照作废", bad is False, f"bad={bad}")
    # 字段缺失（get_report 无 cached_at）同样视为过期，宁可重建
    none_at = api._cache_fresh("")
    check("AI 缓存时效: 无时间戳作废", none_at is False, f"none={none_at}")


def test_ai_cache_blank_degraded_invalidated() -> None:
    """历史缺陷缓存作废：旧版本把空白「LLM 请求失败: 」写进 degraded_reason，

    升级后这些缓存仍能通过各项校验继续命中，用户会一直看到空白报错；
    检测到即作废重建，有具体内容的降级原因则正常命中。
    """
    from backend import scorecfg

    base: dict[str, object] = {
        "code": "600000", "name": "测试", "board": "",
        "price": 10.0, "change_pct": 0.0,
        "analysis": {"advice": {"scores": {"intraday": 1}}},
        "meta": {
            "engine": "rule", "model": "内置规则引擎",
            "generated_at": "2026-08-21 10:00:00",
            "fingerprint": llmcfg.fingerprint(),
            "score_fp": scorecfg.fingerprint(),
            "schema_version": api.REPORT_SCHEMA_VERSION,
            "degraded_reason": "AI 服务调用失败（LLM 请求失败: ），已降级为内置规则引擎",
        },
        "report_sentiment": {"bull": 0, "bear": 0, "neutral": 0},
        "rating_dist": {}, "reports_preview": [], "status_tags": [],
    }
    storage.save_report("600000", base)
    check("AI 缓存作废: 空白降级原因不命中", api._cached_report("600000") is None)

    # 有具体内容的降级原因（如当前版本的超时提示）应正常命中
    base["meta"]["degraded_reason"] = (  # type: ignore[index]
        "AI 服务调用失败（LLM 请求失败: 等待 api.deepseek.com 响应超时（90s）。"
        "请调大 LLM_TIMEOUT 环境变量，或换用更快的模型），已降级为内置规则引擎"
    )
    storage.save_report("600000", base)
    hit = api._cached_report("600000")
    check("AI 缓存命中: 有具体内容的降级原因",
          hit is not None and hit.get("from_cache") is True, f"hit={hit is not None}")

    # 正则本身：空白尾巴匹配，有内容不匹配
    blank = api._BLANK_LLM_REASON_RE.search("AI 服务调用失败（LLM 请求失败: ），已降级为内置规则引擎")
    check("AI 空白正则: 空白尾巴命中", blank is not None)
    ok_reason = "AI 服务调用失败（LLM 请求失败: 无法连接 api.deepseek.com，请检查网络）"
    check("AI 空白正则: 有内容不命中", api._BLANK_LLM_REASON_RE.search(ok_reason) is None)


# ------------------------------------------------------------------ 指标
def test_indicators() -> None:
    bars = [
        Bar(date=f"2026-01-{i:02d}", open=10 + i, close=10 + i * 0.5, high=12 + i, low=9 + i, volume=100.0)
        for i in range(1, 70)
    ]
    infos, summary = build_ma(bars, 20.0)
    check("均线 4 条", len(infos) == 4, str(len(infos)))
    check("均线汇总", "arrangement" in summary and "series" in summary)
    ma_values = {i.window: i.value for i in infos}
    sr = support_resistance(bars, 20.0, ma_values)
    check("支撑压力", bool(sr.get("support") and sr.get("resistance")), str(sr.get("state")))

    # 资金流向当日新鲜度：ref_date=K线最新日期
    from backend.providers.base import FlowDay
    flow_rows = [
        FlowDay(date="2026-08-14", main=-1.8e8, sm=0, md=0, lg=0, xl=-1.3e8),
        FlowDay(date="2026-08-17", main=2.4e8, sm=0, md=0, lg=0, xl=2.9e8),
        FlowDay(date="2026-08-18", main=-2.45e8, sm=0, md=0, lg=0, xl=-2.58e8),
    ]
    f_fresh = summarize_flow(flow_rows, ref_date="2026-08-18")
    check("资金新鲜: 当日已发布", f_fresh["fresh"] is True and f_fresh["last_date"] == "2026-08-18", str(f_fresh.get("fresh")))
    check("资金新鲜: 当日口径判定", f_fresh["state"] == "主力净流出", f_fresh["state"])
    # 模拟 16 点前：最后一行是 17 日（昨日）且为流出，K线已到 18 日
    flow_y = [
        FlowDay(date="2026-08-14", main=1.8e8, sm=0, md=0, lg=0, xl=1.3e8),
        FlowDay(date="2026-08-17", main=-2.4e8, sm=0, md=0, lg=0, xl=-2.9e8),
    ]
    f_y = summarize_flow(flow_y, ref_date="2026-08-18")
    check("资金未发布: fresh=False", f_y["fresh"] is False and f_y["last_date"] == "2026-08-17", str(f_y.get("fresh")))
    check("资金未发布: 退回近5日口径", f_y["state"] == "主力净流出（近5日）", f_y["state"])

    # --------------------- 5 档分级 + 价量背离 + 主力类型 ---------------------

    # 主力抢筹：连入 3 日 + 超大单主导（机构抢筹）+ 共振看多
    # 至少 10 日数据才能计算价量背离（前 5 日 vs 后 5 日对比）
    flow_strong_in = [
        FlowDay(date="2026-07-29", main=1.0e8, sm=0, md=0, lg=0, xl=0.9e8, close=9.0),
        FlowDay(date="2026-07-30", main=1.2e8, sm=0, md=0, lg=0, xl=1.0e8, close=9.2),
        FlowDay(date="2026-07-31", main=1.1e8, sm=0, md=0, lg=0, xl=1.0e8, close=9.4),
        FlowDay(date="2026-08-03", main=1.3e8, sm=0, md=0, lg=0, xl=1.1e8, close=9.6),
        FlowDay(date="2026-08-04", main=1.0e8, sm=0, md=0, lg=0, xl=0.9e8, close=9.8),
        FlowDay(date="2026-08-11", main=2.0e8, sm=0, md=0, lg=0, xl=1.8e8, close=10.0),
        FlowDay(date="2026-08-12", main=3.0e8, sm=0, md=0, lg=0, xl=2.7e8, close=10.2),
        FlowDay(date="2026-08-13", main=2.5e8, sm=0, md=0, lg=0, xl=2.4e8, close=10.5),
        FlowDay(date="2026-08-14", main=2.8e8, sm=0, md=0, lg=0, xl=2.6e8, close=10.8),
        FlowDay(date="2026-08-17", main=2.2e8, sm=0, md=0, lg=0, xl=2.0e8, close=11.0),
        FlowDay(date="2026-08-18", main=3.0e8, sm=0, md=0, lg=0, xl=2.9e8, close=11.3),
    ]
    f_si = summarize_flow(flow_strong_in, ref_date="2026-08-18")
    check("资金状态: 主力抢筹(连入+超大单主导)", f_si["state"] == "主力抢筹" and f_si["state_grade"] == "inflow", f_si["state"])
    check("主力类型: 机构主导(超大单70%+)", "机构主导" in (f_si.get("xl_dominance") or ""), str(f_si.get("xl_dominance")))
    check("价量背离: 价格↑资金↑共振看多", "共振看多" in (f_si.get("price_flow_note") or ""), str(f_si.get("price_flow_note")))

    # 主力出逃：连出 3 日 + 超大单主导 + 共振看空
    flow_strong_out = [
        FlowDay(date="2026-07-29", main=-1.0e8, sm=0, md=0, lg=0, xl=-0.9e8, close=11.8),
        FlowDay(date="2026-07-30", main=-1.2e8, sm=0, md=0, lg=0, xl=-1.0e8, close=11.6),
        FlowDay(date="2026-07-31", main=-1.1e8, sm=0, md=0, lg=0, xl=-1.0e8, close=11.4),
        FlowDay(date="2026-08-03", main=-1.3e8, sm=0, md=0, lg=0, xl=-1.1e8, close=11.2),
        FlowDay(date="2026-08-04", main=-1.0e8, sm=0, md=0, lg=0, xl=-0.9e8, close=11.0),
        FlowDay(date="2026-08-11", main=-2.0e8, sm=0, md=0, lg=0, xl=-1.8e8, close=11.5),
        FlowDay(date="2026-08-12", main=-3.0e8, sm=0, md=0, lg=0, xl=-2.7e8, close=11.3),
        FlowDay(date="2026-08-13", main=-2.5e8, sm=0, md=0, lg=0, xl=-2.4e8, close=11.0),
        FlowDay(date="2026-08-14", main=-2.8e8, sm=0, md=0, lg=0, xl=-2.6e8, close=10.8),
        FlowDay(date="2026-08-17", main=-2.2e8, sm=0, md=0, lg=0, xl=-2.0e8, close=10.5),
        FlowDay(date="2026-08-18", main=-3.0e8, sm=0, md=0, lg=0, xl=-2.9e8, close=10.2),
    ]
    f_so = summarize_flow(flow_strong_out, ref_date="2026-08-18")
    check("资金状态: 主力出逃(连出+超大单主导)", f_so["state"] == "主力出逃" and f_so["state_grade"] == "outflow", f_so["state"])
    check("价量背离: 价格↓资金↓共振看空", "共振看空" in (f_so.get("price_flow_note") or ""), str(f_so.get("price_flow_note")))

    # 普通流入（非连入/超大单主导） → 主力净流入
    flow_plain_in = [
        FlowDay(date="2026-08-15", main=1.0e8, sm=0, md=0, lg=0, xl=0.3e8, close=10.0),
        FlowDay(date="2026-08-16", main=1.5e8, sm=0, md=0, lg=0, xl=0.4e8, close=10.1),
        FlowDay(date="2026-08-17", main=0.8e8, sm=0, md=0, lg=0, xl=0.2e8, close=10.3),
        FlowDay(date="2026-08-18", main=1.2e8, sm=0, md=0, lg=0, xl=0.3e8, close=10.5),
    ]
    f_pi = summarize_flow(flow_plain_in, ref_date="2026-08-18")
    check("资金状态: 普通流入→主力净流入", f_pi["state"] == "主力净流入" and f_pi["state_grade"] == "inflow", f_pi["state"])
    check("主力类型: 主力分散(超大单<40%)", "主力分散" in (f_pi.get("xl_dominance") or ""), str(f_pi.get("xl_dominance")))

    # 价量背离：价格↑ 资金↓ = 高位诱多
    flow_divergence = [
        # 前5日：价格低、资金高（吸筹期）
        FlowDay(date="2026-08-11", main=2.0e8, sm=0, md=0, lg=0, xl=1.5e8, close=9.0),
        FlowDay(date="2026-08-12", main=2.5e8, sm=0, md=0, lg=0, xl=2.0e8, close=9.2),
        FlowDay(date="2026-08-13", main=3.0e8, sm=0, md=0, lg=0, xl=2.5e8, close=9.4),
        FlowDay(date="2026-08-14", main=2.8e8, sm=0, md=0, lg=0, xl=2.3e8, close=9.6),
        FlowDay(date="2026-08-17", main=2.2e8, sm=0, md=0, lg=0, xl=1.8e8, close=9.8),
        # 后5日：价格上涨但资金流出（诱多）
        FlowDay(date="2026-08-18", main=-1.5e8, sm=0, md=0, lg=0, xl=-1.2e8, close=10.2),
        FlowDay(date="2026-08-19", main=-2.0e8, sm=0, md=0, lg=0, xl=-1.8e8, close=10.5),
        FlowDay(date="2026-08-20", main=-2.5e8, sm=0, md=0, lg=0, xl=-2.2e8, close=10.8),
        FlowDay(date="2026-08-21", main=-2.0e8, sm=0, md=0, lg=0, xl=-1.7e8, close=11.0),
        FlowDay(date="2026-08-24", main=-1.8e8, sm=0, md=0, lg=0, xl=-1.5e8, close=11.3),
    ]
    f_dv = summarize_flow(flow_divergence, ref_date="2026-08-24")
    check("价量背离: 高位诱多(价格↑资金↓)", "高位诱多" in (f_dv.get("price_flow_note") or ""), str(f_dv.get("price_flow_note")))

    # 价量背离：价格↓ 资金↑ = 低位吸筹
    flow_absorb = [
        FlowDay(date="2026-08-11", main=-2.0e8, sm=0, md=0, lg=0, xl=-1.5e8, close=11.5),
        FlowDay(date="2026-08-12", main=-2.5e8, sm=0, md=0, lg=0, xl=-2.0e8, close=11.3),
        FlowDay(date="2026-08-13", main=-3.0e8, sm=0, md=0, lg=0, xl=-2.5e8, close=11.0),
        FlowDay(date="2026-08-14", main=-2.8e8, sm=0, md=0, lg=0, xl=-2.3e8, close=10.8),
        FlowDay(date="2026-08-17", main=-2.2e8, sm=0, md=0, lg=0, xl=-1.8e8, close=10.5),
        FlowDay(date="2026-08-18", main=1.5e8, sm=0, md=0, lg=0, xl=1.2e8, close=10.2),
        FlowDay(date="2026-08-19", main=2.0e8, sm=0, md=0, lg=0, xl=1.8e8, close=10.0),
        FlowDay(date="2026-08-20", main=2.5e8, sm=0, md=0, lg=0, xl=2.2e8, close=9.8),
        FlowDay(date="2026-08-21", main=2.0e8, sm=0, md=0, lg=0, xl=1.7e8, close=9.6),
        FlowDay(date="2026-08-24", main=1.8e8, sm=0, md=0, lg=0, xl=1.5e8, close=9.4),
    ]
    f_ab = summarize_flow(flow_absorb, ref_date="2026-08-24")
    check("价量背离: 低位吸筹(价格↓资金↑)", "低位吸筹" in (f_ab.get("price_flow_note") or ""), str(f_ab.get("price_flow_note")))

    # 新浪兜底源 xl=0 → 主力类型应为空
    flow_sina = [
        FlowDay(date="2026-08-17", main=1.5e8, sm=0.3e8, md=0, lg=0, xl=0, close=10.0),
        FlowDay(date="2026-08-18", main=2.0e8, sm=0.5e8, md=0, lg=0, xl=0, close=10.2),
    ]
    f_sina = summarize_flow(flow_sina, ref_date="2026-08-18")
    check("主力类型: 新浪兜底源xl=0时返回空", not f_sina.get("xl_dominance"), str(f_sina.get("xl_dominance")))

    # 资金观望：main_last ≈ 0
    flow_watch = [
        FlowDay(date="2026-08-17", main=0.5e7, sm=0, md=0, lg=0, xl=0.3e7, close=10.0),
        FlowDay(date="2026-08-18", main=0.0, sm=0, md=0, lg=0, xl=0.0, close=10.1),
    ]
    f_w = summarize_flow(flow_watch, ref_date="2026-08-18")
    check("资金状态: 资金观望(main=0)", f_w["state"] == "资金观望" and f_w["state_grade"] == "neutral", f_w["state"])

    # 样本太少时不计算背离
    flow_short = [
        FlowDay(date="2026-08-17", main=1.0e8, sm=0, md=0, lg=0, xl=0.5e8, close=10.0),
        FlowDay(date="2026-08-18", main=1.5e8, sm=0, md=0, lg=0, xl=0.8e8, close=10.2),
    ]
    f_sh = summarize_flow(flow_short, ref_date="2026-08-18")
    check("价量背离: 样本不足返回空", not f_sh.get("price_flow_note"), str(f_sh.get("price_flow_note")))

    # _tone_flow 染色覆盖
    from backend.indicators import _tone_flow
    check("_tone_flow: 主力抢筹→up", _tone_flow("主力抢筹") == "up", _tone_flow("主力抢筹"))
    check("_tone_flow: 主力净流入→up", _tone_flow("主力净流入") == "up", _tone_flow("主力净流入"))
    check("_tone_flow: 主力净流入（近5日）→up", _tone_flow("主力净流入（近5日）") == "up", _tone_flow("主力净流入（近5日）"))
    check("_tone_flow: 主力出逃→down", _tone_flow("主力出逃") == "down", _tone_flow("主力出逃"))
    check("_tone_flow: 主力净流出→down", _tone_flow("主力净流出") == "down", _tone_flow("主力净流出"))
    check("_tone_flow: 资金观望→flat", _tone_flow("资金观望") == "flat", _tone_flow("资金观望"))


# ------------------------------------------------------------------ 数据源装配（不触网）
def test_registry() -> None:
    reg = registry()
    names = [p.name for p in reg.providers]
    check("数据源装配", "eastmoney" in names and len(names) >= 3, str(names))
    check("健康度接口（不触网）", len(reg.health()) == len(names))
    # 资讯/研报/财报能力：同花顺主源，东方财富辅源，资讯保留新浪末级兜底
    news_caps = sorted(p.name for p in reg.providers if "news" in p.caps)
    report_caps = sorted(p.name for p in reg.providers if "reports" in p.caps)
    financial_caps = sorted(p.name for p in reg.providers if "financials" in p.caps)
    check("资讯源装配（同花顺+东财+新浪兜底）", news_caps == ["eastmoney", "sina", "ths"], str(news_caps))
    check("研报源装配（同花顺+东财兜底）", report_caps == ["eastmoney", "ths"], str(report_caps))
    check("财报源装配（同花顺+东财兜底）", financial_caps == ["eastmoney", "ths"], str(financial_caps))
    ordered_news = [p.name for p in reg._available("news")]
    ordered_reports = [p.name for p in reg._available("reports")]
    check("资讯研报优先级: 同花顺在东财前", ordered_news[:2] == ["ths", "eastmoney"] and ordered_reports[:2] == ["ths", "eastmoney"],
          str((ordered_news, ordered_reports)))


def test_watch_monitor() -> None:
    add = service.watch_monitor({"status": "normal", "change_pct": 3.2, "volume_ratio": 1.8})
    reduce = service.watch_monitor({"status": "normal", "change_pct": -3.0, "volume_ratio": 1.0})
    observe = service.watch_monitor({"status": "normal", "change_pct": 1.2, "volume_ratio": 1.0})
    delayed = service.watch_monitor({"status": "delayed", "status_text": "数据更新延迟"})
    check("关键监测: 放量上涨提示可加仓", add["action"] == "可加仓" and add["tone"] == "up", str(add))
    check("关键监测: 下跌提示应减仓", reduce["action"] == "应减仓" and reduce["tone"] == "down", str(reduce))
    check("关键监测: 普通波动继续观察", observe["action"] == "继续观察", str(observe))
    check("关键监测: 异常行情不误报加减仓", delayed["action"] == "继续观察" and delayed["tone"] == "warn", str(delayed))
    old_trading, old_session = service.is_trading_now, service.session_state
    try:
        service.is_trading_now = lambda: True
        service.session_state = lambda: "open"
        check("首页刷新周期: 5秒", service.session_info()["interval_ms"] == 5000)
    finally:
        service.is_trading_now, service.session_state = old_trading, old_session


def test_watch_monitor() -> None:
    add = service.watch_monitor({"status": "normal", "change_pct": 3.2, "volume_ratio": 1.8})
    reduce = service.watch_monitor({"status": "normal", "change_pct": -3.0, "volume_ratio": 1.0})
    observe = service.watch_monitor({"status": "normal", "change_pct": 1.2, "volume_ratio": 1.0})
    delayed = service.watch_monitor({"status": "delayed", "status_text": "数据更新延迟"})
    check("关键监测: 放量上涨提示可加仓", add["action"] == "可加仓" and add["tone"] == "up", str(add))
    check("关键监测: 下跌提示应减仓", reduce["action"] == "应减仓" and reduce["tone"] == "down", str(reduce))
    check("关键监测: 普通波动继续观察", observe["action"] == "继续观察", str(observe))
    check("关键监测: 异常行情不误报加减仓", delayed["action"] == "继续观察" and delayed["tone"] == "warn", str(delayed))
    old_trading, old_session = service.is_trading_now, service.session_state
    try:
        service.is_trading_now = lambda: True
        service.session_state = lambda: "open"
        check("首页刷新周期: 5秒", service.session_info()["interval_ms"] == 5000)
    finally:
        service.is_trading_now, service.session_state = old_trading, old_session


def test_items_fingerprint() -> None:
    """解读缓存指纹：条目内容一变指纹即变，防止新标题配旧解读的缓存错位。"""
    from backend.utils import items_fingerprint

    a = [{"id": "1", "date": "2026-08-18", "title": "中标合同"},
         {"id": "2", "date": "2026-08-17", "title": "回购"}]
    b = [{"id": "2", "date": "2026-08-17", "title": "回购"},
         {"id": "3", "date": "2026-08-16", "title": "减持"}]
    check("指纹: 相同条目同指纹", items_fingerprint(a) == items_fingerprint(list(a)), items_fingerprint(a))
    check("指纹: 条目变化指纹变化", items_fingerprint(a) != items_fingerprint(b),
          f"{items_fingerprint(a)} vs {items_fingerprint(b)}")
    check("指纹: 标题变化指纹变化",
          items_fingerprint([{"id": "1", "title": "A"}]) != items_fingerprint([{"id": "1", "title": "B"}]))
    collision_a = [{"id": "1", "date": "2026:08", "title": "18"}]
    collision_b = [{"id": "1", "date": "2026", "title": "08:18"}]
    check("指纹: 字段分隔符不会碰撞", items_fingerprint(collision_a) != items_fingerprint(collision_b))
    check("指纹: 空列表稳定不报错", isinstance(items_fingerprint([]), str) and len(items_fingerprint([])) == 12)


# ------------------------------------------------------------------ 前端外链协议白名单
# 资讯/快讯的 url 来自第三方源且后端不校验协议，javascript: 地址若进入 a.href，
# 点击标题即在本站源内执行脚本（本站源可读写自选股与 LLM 配置接口）。
# 运行镜像不带 node，缺失时跳过而非判失败。
def test_safe_url() -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        print("[skip] 前端 safeUrl（未安装 node）")
        return

    root = Path(__file__).resolve().parent.parent
    util = (root / "frontend" / "static" / "js" / "util.js").as_posix()
    script = (
        "global.window={};global.document={};require('%s');"
        "const U=global.window.U;"
        "const pass=['https://a.cn/x','http://a.cn/x','//a.cn/x','/api/x'];"
        "const block=['javascript:alert(1)','JavaScript:alert(1)','java\\tscript:alert(1)',"
        "' javascript:alert(1)','data:text/html,<script>a</script>','vbscript:msgbox(1)'];"
        "let bad=[];"
        "for(const u of pass){if(U.safeUrl(u)!==u)bad.push('应放行:'+u);}"
        "for(const u of block){if(U.safeUrl(u)!=='')bad.push('应拦截:'+u);}"
        "console.log(bad.length?bad.join('|'):'OK');"
    ) % util
    try:
        proc = subprocess.run(
            [node, "-e", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        check("前端 safeUrl 协议白名单", False, f"超时: {exc}")
        return
    out = (proc.stdout or "").strip()
    check("前端 safeUrl 协议白名单", out == "OK", out or (proc.stderr or "")[:200])


# ------------------------------------------------------------------ 盘口回测脚本自测（不触网，合成日线）
def test_backtest_selftest() -> None:
    import subprocess
    import sys as _sys

    root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            [_sys.executable, str(root / "backtest_intraday.py"), "--selftest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        detail = (proc.stdout or "")[-200:] + (proc.stderr or "")[-200:]
    except subprocess.TimeoutExpired as exc:
        check("盘口回测脚本自测", False, f"超时: {exc}")
        return
    check("盘口回测脚本自测", proc.returncode == 0, detail)

    # 盘中 vs 收盘对照实验脚本自测（不触网，合成日线+分钟线）
    try:
        proc2 = subprocess.run(
            [_sys.executable, str(root / "backtest_compare.py"), "--selftest"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        detail2 = (proc2.stdout or "")[-200:] + (proc2.stderr or "")[-200:]
    except subprocess.TimeoutExpired as exc:
        check("盘中对照实验脚本自测", False, f"超时: {exc}")
        return
    check("盘中对照实验脚本自测", proc2.returncode == 0, detail2)


# ------------------------------------------------------------------ 自检报告含回测段落（不触网，仅结构）
def test_check_sources_backtest_struct() -> None:
    from backend import check_sources

    # _backtest_advice 口径与回测脚本一致
    check("回测校准建议: 有效可上调", check_sources._backtest_advice(0.60, 0.46, 200) == "有效，可维持或上调权重")
    check("回测校准建议: 有效可维持", check_sources._backtest_advice(0.49, 0.46, 200) == "有效，权重可维持")
    check("回测校准建议: 偏弱下调", check_sources._backtest_advice(0.44, 0.46, 200) == "偏弱，建议下调权重")
    check("回测校准建议: 反向", check_sources._backtest_advice(0.38, 0.46, 200) == "反向/无效，建议大幅下调或检查方向")
    check("回测校准建议: 样本不足", check_sources._backtest_advice(0.90, 0.46, 10) == "样本不足，暂不调整")

    # check_backtest 失败分支（空样本）返回 ok=False 且不抛错
    async def _probe_empty() -> dict:
        return await check_sources.check_backtest([("600000", "SH")], days=5)

    res = asyncio.run(_probe_empty())
    check("回测探测: 样本不足返回 ok=False", res.get("ok") is False, str(res))

    # 回测脚本缺失降级分支：镜像未打包 backtest_intraday.py 时返回 degraded
    # 提示而非抛异常（容器运行时场景，不触网）
    orig_import = check_sources.__dict__.get("__bt_import_guard")
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "backtest_intraday":
            raise ModuleNotFoundError("No module named 'backtest_intraday'")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _fake_import
    try:
        res_d = asyncio.run(check_sources.check_backtest([("600000", "SH")], days=5))
    finally:
        builtins.__import__ = real_import
    check("回测探测: 脚本缺失降级不抛错",
          res_d.get("ok") is False and res_d.get("degraded") is True and "未打包" in res_d.get("error", ""),
          str(res_d))

    # 渲染函数能处理含回测段/无回测段的报告
    text_with = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": ["tencent"],
        "latest_trade_date": "2026-08-17", "issues": [],
        "backtest": {
            "ok": True, "samples": 100, "stocks": 1, "base_up_rate": 46.0,
            "base_avg_ret": 0.1, "buckets": [], "signals": [],
        },
    })
    check("自检渲染: 含回测标题", "盘口信号近期命中率" in text_with, text_with[:200])
    text_no = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": [],
        "latest_trade_date": "", "issues": [], "backtest": {"ok": False, "error": "回测失败: x"},
    })
    check("自检渲染: 回测失败分支", "回测失败" in text_no)
    text_dg = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": ["tencent"],
        "latest_trade_date": "", "issues": [],
        "backtest": {"ok": False, "error": "回测脚本未打包进镜像（ModuleNotFoundError）", "degraded": True},
    })
    check("自检渲染: 回测降级分支", "已降级" in text_dg and "其余自检正常" in text_dg, text_dg[:300])
    text_sk = check_sources.render_text({
        "time": "2026-08-18 12:00:00", "session": "closed", "trading": False,
        "sample": ["600000.SH"], "providers": [], "quote_sources_ok": ["tencent"],
        "latest_trade_date": "", "issues": [],
        "backtest": {"ok": False, "skipped": True, "error": "已跳过回测（仅数据源自检）"},
    })
    check("自检渲染: 回测跳过分支", "已跳过回测" in text_sk, text_sk[:300])
    # run_diagnostics 分离：with_backtest=False 时返回 skipped 且不执行回测
    report_fast = asyncio.run(check_sources.run_diagnostics("600000", with_backtest=False))
    check("自检分离: 仅数据源跳过回测", report_fast["backtest"].get("skipped") is True
          and report_fast["backtest_days"] == 0,
          str(report_fast["backtest"])[:120])
    # 回测深度参数化：with_backtest=True 时 backtest_days 传递并记录
    report_days = asyncio.run(check_sources.run_diagnostics("600000", with_backtest=True,
                                                             backtest_days=30))
    check("回测深度: backtest_days 传递", report_days.get("backtest_days") == 30,
          str(report_days.get("backtest_days")))

    # 置信度分档：样本越深越可靠
    check("置信度: ≥100 高", check_sources._confidence(500)["level"] == "high",
          str(check_sources._confidence(500)))
    check("置信度: 50-99 中", check_sources._confidence(60)["level"] == "medium",
          str(check_sources._confidence(60)))
    check("置信度: <50 低", check_sources._confidence(10)["level"] == "low",
          str(check_sources._confidence(10)))
    # 汇总报告三层置信度字段（总体/分桶/信号）
    conf_samples = [
        {"score": 3, "next_ret": 1.2, "labels": [("高位强势", True)]},
        {"score": -2, "next_ret": -0.5, "labels": [("低位下跌", False)]},
        {"score": 0, "next_ret": 0.3, "labels": []},
    ] * 20
    conf_rep = check_sources._summarize_backtest_safe(
        {"samples": conf_samples, "per_stock": [{"code": "600000"}]}, [("600000", "SH")]
    )
    check("置信度: 报告含总体字段", conf_rep["confidence"]["level"] == "low", str(conf_rep["confidence"]))
    check("置信度: 分桶含字段", all("confidence" in b for b in conf_rep["buckets"]), str(conf_rep["buckets"][:1]))
    check("置信度: 信号含字段", all("confidence" in s for s in conf_rep["signals"]), str(conf_rep["signals"][:1]))

    # 独立回测脚本同步置信度：confidence 分档与 render 报告含置信列（不触网）
    import backtest_intraday as _bt
    check("脚本置信度: 分档一致", _bt.confidence(500)[0] == "高" and _bt.confidence(60)[0] == "中"
          and _bt.confidence(10)[0] == "低", str((_bt.confidence(500), _bt.confidence(60), _bt.confidence(10))))
    _bt_report = _bt.render({
        "per_stock": [{"code": "600000"}],
        "samples": [
            {"score": 3, "next_ret": 1.2, "labels": [("高位强势", True)]},
            {"score": -2, "next_ret": -0.5, "labels": [("低位下跌", False)]},
            {"score": 0, "next_ret": 0.3, "labels": []},
        ] * 40,
    })
    check("脚本置信度: 报告含总体/列", "置信度:" in _bt_report and "置信" in _bt_report,
          _bt_report[:200])

    # _summarize_backtest_safe 兜底：异常样本结构不抛错，返回 ok=False
    bad_summary = check_sources._summarize_backtest_safe(
        {"samples": [{"score": 3} for _ in range(40)], "per_stock": []}, [("600000", "SH")]
    )
    check("回测统计兜底: 坏样本不抛错", bad_summary.get("ok") is False and "回测统计失败" in bad_summary.get("error", ""),
          str(bad_summary))
    # 正常样本走通统计
    ok_samples = [
        {"score": 3, "next_ret": 1.2, "labels": [("高位强势", True)]},
        {"score": -2, "next_ret": -0.5, "labels": [("低位下跌", False)]},
        {"score": 0, "next_ret": 0.3, "labels": []},
    ] * 20
    good_summary = check_sources._summarize_backtest_safe(
        {"samples": ok_samples, "per_stock": [{"code": "600000"}]}, [("600000", "SH")]
    )
    check("回测统计兜底: 正常样本可出报告", good_summary.get("ok") is True and good_summary["samples"] == 60,
          str(good_summary)[:200])


def main() -> int:
    test_json_repair()
    test_describe_exc()
    test_llm_timeout_floor()
    test_fingerprint()
    test_llm_profiles()
    test_llm_failover()
    test_model_filter()
    test_value_screener()
    test_value_weights()
    test_news_interpret()
    test_hotspot()
    test_hotspot_ai()
    test_reports_interpret()
    test_financials()
    test_rule_precision()
    test_kline_stale()
    test_cache()
    test_ai_lock()
    test_ai_cache_freshness()
    test_ai_cache_blank_degraded_invalidated()
    test_ai_sanitize()
    test_payload_quality()
    test_indicators()
    test_registry()
    test_watch_monitor()
    test_items_fingerprint()
    test_safe_url()
    test_backtest_selftest()
    test_check_sources_backtest_struct()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 共 {TOTAL} 项检查: {FAILED}")
        return 1
    print(f"全部通过（{TOTAL} 项检查）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
