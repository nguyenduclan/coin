import os

os.environ.update(ADMIN_PASSWORD="pw", SESSION_SECRET="x" * 40,
                  DATABASE_URL="sqlite+aiosqlite:////tmp/test_p3.db")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.analysis.engine import _extract_json, preliminary_state  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.indicators import ta  # noqa: E402
from app.models import NewsItem  # noqa: E402
from app.news import rss  # noqa: E402

RSS = """<rss><channel>
<item><title>Bitcoin hits new high</title><link>https://x.com/a?utm=1</link>
<pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;BTC rally&lt;/p&gt;</description></item>
<item><title>Solana outage</title><link>https://x.com/b</link></item>
<item><title>no link</title></item></channel></rss>"""


def make_df(prices, vol=1.0):
    p = pd.Series(prices, dtype=float)
    return pd.DataFrame({"open": p, "high": p * 1.001, "low": p * 0.999, "close": p,
                         "volume": vol})


def test_rsi_extremes_and_ema():
    up = pd.Series(np.arange(1, 100, dtype=float))
    assert ta.rsi(up).iloc[-1] == 100
    assert ta.rsi(up[::-1].reset_index(drop=True)).iloc[-1] < 1
    assert ta.ema(pd.Series([5.0] * 30), 9).iloc[-1] == pytest.approx(5.0)


def test_rsi_known_value():
    # Chuỗi mẫu kinh điển của Wilder (n=14) cho RSI ~ 70.5 ở điểm 15
    c = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                   45.89, 46.03, 45.61, 46.28, 46.28])
    assert ta.rsi(c).iloc[-1] == pytest.approx(70.5, abs=0.1)


def test_trend_classification():
    assert ta.classify_trend(make_df(np.linspace(100, 200, 120))) == "UP"
    assert ta.classify_trend(make_df(np.linspace(200, 100, 120))) == "DOWN"
    assert ta.classify_trend(make_df([100] * 30)) == "INSUFFICIENT"


def test_swing_levels_split_around_price():
    t = np.arange(200)
    df = make_df(100 + 10 * np.sin(t / 6))
    lv = ta.swing_levels(df)
    last = df["close"].iloc[-1]
    assert all(x > last for x in lv["resistance"]) and all(x < last for x in lv["support"])


def test_preliminary_state():
    assert preliminary_state({"1h": {"trend": "INSUFFICIENT"}})[0] == "DATA_UNAVAILABLE"
    assert preliminary_state({"1h": {"trend": "UP"}, "4h": {"trend": "DOWN"}})[0] == "NO_TRADE"
    assert preliminary_state({"1h": {"trend": "SIDEWAYS"}, "4h": {"trend": "UP"}})[0] == "WAIT"


def test_parse_feed_and_tagging():
    items = rss.parse_feed(RSS, "x.com")
    assert [i["url"] for i in items] == ["https://x.com/a?utm=1", "https://x.com/b"]
    assert items[0]["published_at"].year == 2026 and items[1]["published_at"] is None
    assert items[0]["summary"] == "BTC rally"
    assert rss.tag_assets("Bitcoin and Solana", ["BTC", "SOL", "ETH"]) == ["BTC", "SOL"]
    assert rss.tag_assets("Resolved issue", ["SOL"]) == []  # không khớp giữa từ
    assert rss.url_hash("https://x.com/a?utm=1") == rss.url_hash("https://x.com/a?utm=2")


def test_extract_json():
    assert _extract_json('ok ```json\n{"a": 1}\n```')["a"] == 1
    with pytest.raises(ValueError):
        _extract_json("không có json")


async def test_fetch_news_dedupes_and_survives_bad_feed():
    import httpx
    from app.core.config import settings
    settings.news_feeds = "https://ok.test/rss,https://bad.test/rss"

    def handler(req: httpx.Request):
        return httpx.Response(200, text=RSS) if req.url.host == "ok.test" else httpx.Response(500)

    await init_db()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r1 = await rss.fetch_news(c)
        r2 = await rss.fetch_news(c)
    assert r1["new"] == 2 and len(r1["errors"]) == 1
    assert r2["new"] == 0
    async with SessionLocal() as s:
        assert len(list(await s.scalars(select(NewsItem)))) == 2
