import os

os.environ.update(ADMIN_PASSWORD="pw", SESSION_SECRET="x" * 40,
                  DATABASE_URL="sqlite+aiosqlite:////tmp/test_coin.db")

import pytest  # noqa: E402

from app.binance.rest import parse_symbol_info  # noqa: E402
from app.binance.stream import backoff_delay, build_stream_url, handle_message  # noqa: E402
from app.core import auth  # noqa: E402
from app.core.db import init_db  # noqa: E402
from app.data.state import MarketState, market  # noqa: E402


def test_backoff_grows_and_caps():
    assert backoff_delay(0) <= 1.0
    assert backoff_delay(3) <= 8.0
    assert backoff_delay(20) <= 60.0


def test_stream_url():
    u = build_stream_url(["BTCUSDT"], ["5m"])
    assert "btcusdt@bookTicker" in u and "btcusdt@kline_5m" in u and "btcusdt@ticker" in u


def test_parse_symbol_info():
    raw = {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
           "isSpotTradingAllowed": True,
           "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                       {"filterType": "LOT_SIZE", "stepSize": "0.00001"},
                       {"filterType": "NOTIONAL", "minNotional": "5.0"}]}
    i = parse_symbol_info(raw)
    assert (i["tick_size"], i["step_size"], i["min_notional"]) == (0.01, 0.00001, 5.0)


def test_status_unavailable_ok_stale():
    m = MarketState()
    m.set_symbols(["BTCUSDT"])
    assert m.status("BTCUSDT")["state"] == "DATA_UNAVAILABLE"
    m.ws_connected = True
    m.update_ticker("BTCUSDT", {"c": "100", "P": "1", "v": "1", "q": "100", "E": 1000}, now=1000.0)
    assert m.status("BTCUSDT", now=1002.0)["state"] == "OK"
    assert m.status("BTCUSDT", now=1100.0)["state"] == "STALE"
    m.ws_connected = False
    assert m.status("BTCUSDT", now=1001.0)["state"] == "STALE"


def test_token_roundtrip_and_expiry():
    t = auth.make_token(now=1000)
    assert auth.verify_token(t, now=1001)
    assert not auth.verify_token(t, now=1000 + 10**6)
    assert not auth.verify_token(t + "x")
    assert not auth.verify_token("garbage")


@pytest.mark.asyncio
async def test_handle_message_only_closed_klines_saved():
    await init_db()
    market.set_symbols(["BTCUSDT"])
    base = {"s": "BTCUSDT", "e": "kline"}
    k = {"t": 1, "T": 2, "i": "5m", "o": "1", "h": "2", "l": "1", "c": "2", "v": "3"}
    await handle_message({"data": {**base, "k": {**k, "x": False}}})
    assert "5m" not in market.symbols["BTCUSDT"].last_closed_candle
    await handle_message({"data": {**base, "k": {**k, "x": True}}})
    assert market.symbols["BTCUSDT"].last_closed_candle["5m"] == 1
    await handle_message({"data": {"s": "BTCUSDT", "b": "9", "a": "10", "u": 1}})
    assert market.symbols["BTCUSDT"].bid == 9.0
