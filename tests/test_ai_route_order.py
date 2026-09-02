"""Regression test: /ai/watchlist must NOT be shadowed by /ai/{code}.

History: 路由注册顺序 bug——@router.post('/ai/{code}') 抢在 @router.post('/ai/watchlist') 前面，
导致 POST /api/ai/watchlist?refresh=1 被 {code}='watchlist' 匹配，走单股 AI 流程，
所有行情源都拿不到 "watchlist" 的行情（它不是股票代码），触发「所有行情数据源均不可用」误报。
"""
from __future__ import annotations

import re


def test_ai_watchlist_route_registered_before_ai_code() -> None:
    """具体路由 /ai/watchlist 必须在 /ai/{code} 之前注册，否则会被抢匹配。"""
    api_path = __import__('pathlib').Path(__file__).resolve().parent.parent / 'backend' / 'api.py'
    text = api_path.read_text(encoding='utf-8')

    # Find decorator positions (each takes one line)
    watchlist_get = text.find('@router.get("/ai/watchlist")')
    watchlist_post = text.find('@router.post("/ai/watchlist")')
    code_post = text.find('@router.post("/ai/{code}")')

    assert watchlist_get != -1, '/ai/watchlist GET route not found'
    assert watchlist_post != -1, '/ai/watchlist POST route not found'
    assert code_post != -1, '/ai/{code} POST route not found'

    assert watchlist_get < code_post, (
        f'/ai/watchlist GET must be registered before /ai/{{code}}, '
        f'otherwise {{code}}="watchlist" will shadow it. '
        f'watchlist_get={watchlist_get} code_post={code_post}'
    )
    assert watchlist_post < code_post, (
        f'/ai/watchlist POST must be registered before /ai/{{code}}, '
        f'otherwise {{code}}="watchlist" will shadow it. '
        f'watchlist_post={watchlist_post} code_post={code_post}'
    )


def test_ai_analyze_rejects_non_numeric_code() -> None:
    """ai_analyze() 防御性：6 位数字才认，其它直接 404，避免把 'watchlist' 这种字面量当股票。"""
    api_path = __import__('pathlib').Path(__file__).resolve().parent.parent / 'backend' / 'api.py'
    text = api_path.read_text(encoding='utf-8')

    # Should have a regex validation matching exactly 6 digits
    pattern = re.compile(r're\.fullmatch\(r"\\d\{6\}"')
    assert pattern.search(text), (
        'ai_analyze() should validate code is 6 digits via re.fullmatch, '
        'otherwise non-numeric codes (like "watchlist") will trigger '
        '"all quote sources unavailable" misleading errors'
    )

    # The 404 error message should be present
    assert '非法的股票代码' in text, 'expected defensive error message "非法的股票代码"'
