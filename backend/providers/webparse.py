"""同花顺/东方财富网页内嵌数据解析。"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta
from typing import Any

from ..utils import TZ, now
from .base import FinancialPeriod, NewsItem, ReportItem

_DATE_RE = re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")

_METRIC_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("revenue_yoy", ("营业总收入同比增长率", "营业收入同比增长率", "营业收入同比")),
    ("revenue", ("营业总收入", "营业收入", "营业总额")),
    ("net_profit_deduct_yoy", ("扣非净利润同比增长率", "扣非净利润同比")),
    ("net_profit_deduct", ("扣非净利润", "扣除非经常性损益后的净利润")),
    ("net_profit_yoy", ("归母净利润同比增长率", "归属于母公司股东的净利润同比", "净利润同比增长率")),
    ("net_profit", ("归母净利润", "归属于母公司股东的净利润", "净利润")),
    ("eps", ("基本每股收益", "每股收益")),
    ("roe", ("净资产收益率", "加权净资产收益率", "ROE")),
    ("gross_margin", ("销售毛利率", "毛利率")),
    ("debt_ratio", ("资产负债率",)),
)


def _date(value: str) -> str:
    match = _DATE_RE.search(value)
    if not match:
        return ""
    y, m, d = match.groups()
    try:
        return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", "").replace("，", "")
    if text in ("", "-", "--", "—", "无", "暂无"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("() ").replace("%", "")
    multiplier = 1.0
    if text.endswith("亿"):
        multiplier, text = 1e8, text[:-1]
    elif text.endswith("万"):
        multiplier, text = 1e4, text[:-1]
    try:
        number = float(text) * multiplier
    except ValueError:
        return None
    return -number if negative else number


def _period_label(date: str) -> str:
    month = int(date[5:7])
    year = date[:4]
    return {3: f"{year}Q1", 6: f"{year}H1", 9: f"{year}Q3", 12: f"{year}FY"}.get(month, date[:7])


def _parse_ths_finance_json(raw: str, source: str) -> list[FinancialPeriod]:
    match = re.search(r'<p\s+id=["\']main["\'][^>]*>(.*?)</p>', raw, re.I | re.S)
    if not match:
        return []
    try:
        payload = json.loads(html.unescape(match.group(1)))
    except (TypeError, json.JSONDecodeError):
        return []
    titles = payload.get("title") or []
    report = payload.get("report") or []
    if len(report) < 2 or not isinstance(report[0], list):
        return []

    date_columns = [
        (column, _date(str(value)))
        for column, value in enumerate(report[0])
        if _date(str(value))
    ]
    if not date_columns:
        return []
    values: dict[str, dict[str, float | None]] = {date: {} for _, date in date_columns}
    for row_index, title in enumerate(titles[1:], start=1):
        if row_index >= len(report) or not isinstance(title, list) or not title:
            continue
        label = str(title[0])
        metric_name = next(
            (key for key, aliases in _METRIC_ALIASES if any(alias in label for alias in aliases)),
            None,
        )
        if not metric_name or not isinstance(report[row_index], list):
            continue
        for column, date in date_columns:
            if column < len(report[row_index]):
                values[date][metric_name] = _number(report[row_index][column])

    result: list[FinancialPeriod] = []
    for _, date in date_columns:
        metric = values[date]
        if not any(value is not None for value in metric.values()):
            continue
        result.append(FinancialPeriod(
            date=date,
            period=_period_label(date),
            revenue=metric.get("revenue"),
            revenue_yoy=metric.get("revenue_yoy"),
            net_profit=metric.get("net_profit"),
            net_profit_yoy=metric.get("net_profit_yoy"),
            net_profit_deduct=metric.get("net_profit_deduct"),
            net_profit_deduct_yoy=metric.get("net_profit_deduct_yoy"),
            eps=metric.get("eps"),
            roe=metric.get("roe"),
            gross_margin=metric.get("gross_margin"),
            debt_ratio=metric.get("debt_ratio"),
            source=source,
        ))
    result.sort(key=lambda item: item.date, reverse=True)
    return result[:12]


def parse_financial_html(raw: str, source: str) -> list[FinancialPeriod]:
    """解析同花顺 finance.html 的主指标 JSON。"""
    rows = _parse_ths_finance_json(raw, source)
    if not rows:
        raise ValueError("网页中未找到可识别的同花顺财报 JSON")
    return rows


def _parse_linkage_news(raw: str, source: str, days: int, limit: int) -> list[NewsItem]:
    match = re.search(r'<div[^>]+id=["\']linkagedata["\'][^>]*>(.*?)</div>', raw, re.I | re.S)
    if not match:
        return []
    try:
        rows = json.loads(html.unescape(match.group(1).strip()))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []

    cutoff = now().date() - timedelta(days=max(days, 0))
    result: list[NewsItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            published = datetime.fromtimestamp(int(row.get("ctime") or 0), TZ)
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if published.date() < cutoff:
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("curl") or "").strip()
        if len(title) < 4 or not url:
            continue
        result.append(NewsItem(
            id=str(row.get("seq") or url.rsplit("/", 1)[-1]),
            date=published.strftime("%Y-%m-%d %H:%M:%S"),
            source=str(row.get("source") or source).strip(),
            title=title,
            summary=title,
            url=url,
        ))
    result.sort(key=lambda item: item.date, reverse=True)
    return result[:limit]


def parse_news_html(raw: str, source: str, days: int, limit: int) -> list[NewsItem]:
    """解析同花顺新闻页隐藏的 linkagedata JSON。"""
    rows = _parse_linkage_news(raw, source, days, limit)
    if not rows:
        raise ValueError("网页中未找到近期资讯 JSON")
    return rows


def _parse_eastmoney_report_json(raw: str, source: str, limit: int) -> list[ReportItem]:
    match = re.search(r"var\s+initdata\s*=\s*", raw, re.I)
    if not match:
        return []
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw[match.end():].lstrip())
        rows = (payload or {}).get("data") or []
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []

    result: list[ReportItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        date = str(row.get("publishDate") or "")[:10]
        if not title or not _date(date):
            continue
        info_code = str(row.get("infoCode") or "").strip()
        result.append(ReportItem(
            id=info_code or title,
            date=date,
            source=str(row.get("orgSName") or row.get("orgName") or source).strip(),
            researcher=str(row.get("researcher") or "").strip(),
            rating=str(row.get("sRatingName") or row.get("emRatingName") or "").strip(),
            title=title,
            url=(
                f"https://data.eastmoney.com/report/zw_stock.jshtml?infocode={info_code}"
                if info_code else ""
            ),
        ))
    result.sort(key=lambda item: item.date, reverse=True)
    return result[:limit]


def parse_report_html(raw: str, source: str, limit: int) -> list[ReportItem]:
    """解析东方财富研报网页内嵌的 initdata JSON。"""
    rows = _parse_eastmoney_report_json(raw, source, limit)
    if not rows:
        raise ValueError("网页中未找到研报 JSON")
    return rows
