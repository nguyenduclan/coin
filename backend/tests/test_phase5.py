import os

os.environ.update(ADMIN_PASSWORD="pw", SESSION_SECRET="x" * 40,
                  DATABASE_URL="sqlite+aiosqlite:////tmp/test_p5.db",
                  BINANCE_API_KEY="k", BINANCE_API_SECRET="s")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.binance.account import BinanceAccount, sign  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.data.state import market  # noqa: E402
from app.models import Symbol  # noqa: E402
from app.portfolio import service  # noqa: E402
from app.portfolio.ledger import apply_trades, max_drawdown, normalize_binance_trade, performance  # noqa: E402
from app.trading.strategy import check_validity, evaluate_position  # noqa: E402


def T(side, qty, price, fee=0.0, t=0, sym="BTCUSDT"):
    return {"symbol": sym, "side": side, "qty": qty, "price": price, "fee_usdt": fee, "time": t}


def test_average_cost_and_realized_pnl():
    r = apply_trades([T("BUY", 1, 100, 0.1, 1), T("BUY", 1, 120, 0.1, 2), T("SELL", 1, 130, 0.13, 3)])
    p = r["positions"]["BTCUSDT"]
    assert p["avg_cost"] == pytest.approx((100.1 + 120.1) / 2)  # phí mua cộng vào giá vốn
    assert p["qty"] == 1
    assert r["closed"][0]["pnl"] == pytest.approx((130 - 110.1) * 1 - 0.13)
    assert p["fees"] == pytest.approx(0.33)


def test_full_close_resets_and_oversell_is_capped():
    r = apply_trades([T("BUY", 1, 100, 0, 1), T("SELL", 1, 90, 0, 2)])
    p = r["positions"]["BTCUSDT"]
    assert p["qty"] == 0 and p["avg_cost"] == 0 and p["realized_pnl"] == pytest.approx(-10)
    r = apply_trades([T("BUY", 1, 100, 0, 1), T("SELL", 3, 110, 0, 2)])  # bán 3 nhưng chỉ có 1
    assert r["positions"]["BTCUSDT"]["realized_pnl"] == pytest.approx(10)


def test_normalize_fee_assets():
    base = {"symbol": "BTCUSDT", "id": 7, "qty": "0.1", "price": "100", "time": 5, "isBuyer": True}
    q = normalize_binance_trade({**base, "commission": "0.01", "commissionAsset": "USDT"}, "BTC", "USDT")
    assert q["fee_usdt"] == 0.01 and q["qty"] == 0.1 and q["fee_converted"]
    b = normalize_binance_trade({**base, "commission": "0.0001", "commissionAsset": "BTC"}, "BTC", "USDT")
    assert b["qty"] == pytest.approx(0.0999) and b["fee_usdt"] == pytest.approx(0.01)
    n = normalize_binance_trade({**base, "commission": "0.001", "commissionAsset": "BNB"}, "BTC", "USDT")
    assert n["fee_usdt"] == 0 and n["fee_converted"] is False
    assert n["id"] == "binance:BTCUSDT:7"


def test_performance_and_drawdown():
    p = performance([{"pnl": 10}, {"pnl": -5}, {"pnl": 20}, {"pnl": -5}])
    assert p["win_rate"] == 0.5 and p["profit_factor"] == pytest.approx(3.0)
    assert p["expectancy"] == pytest.approx(5.0) and p["closed_trades"] == 4 and p["note"]
    assert performance([])["win_rate"] is None
    d = max_drawdown([100, 120, 90, 110])
    assert d["max_drawdown_pct"] == pytest.approx(25.0) and d["current_drawdown_pct"] == pytest.approx(100 / 12)  # (120-110)/120


def test_position_risk():
    assert service.position_risk(2, 100, 90) == 20
    assert service.position_risk(2, 85, 90) == 0  # đã thủng stop: không còn "rủi ro tới stop"
    assert service.position_risk(2, 100, None) == 200  # không stop: tính toàn bộ giá trị
    assert service.position_risk(2, None, 90) is None


POS = {"symbol": "BTCUSDT", "qty": 1.0, "avg_cost": 100.0, "stop": 95.0, "take_profit": 120.0}


def rep(t1h, t4h="UP"):
    return {"timeframes": {"1h": {"trend": t1h}, "4h": {"trend": t4h}}}


def test_evaluate_position():
    ev = lambda price, r, pos=POS: evaluate_position(pos, r, price, 0.001, 5)  # noqa: E731
    assert ev(105, rep("UP"))["state"] == "HOLD"
    assert ev(94, rep("UP"))["state"] == "SELL_EXIT"
    assert ev(121, rep("UP"))["state"] == "SELL_EXIT"
    assert ev(105, rep("DOWN", "DOWN"))["state"] == "SELL_EXIT"
    r = ev(105, rep("DOWN", "UP"))
    assert r["state"] == "REDUCE" and r["proposal"]["qty"] == 0.5 and r["proposal"]["side"] == "SELL"
    # phần còn lại dưới min notional -> thoát hết
    tiny = {**POS, "qty": 0.06}
    r = evaluate_position(tiny, rep("DOWN", "UP"), 105, 0.001, 5)
    assert r["state"] == "SELL_EXIT" and r["proposal"]["qty"] == 0.06
    # không đủ min notional để bán -> HOLD, không đề xuất
    assert evaluate_position({**POS, "qty": 0.01}, rep("UP"), 94, 0.001, 5)["proposal"] is None


def test_sell_validity_ignores_stop_and_trend():
    p = {"expires_at": 1000.0, "ref_price": 94.0, "stop": 95.0}
    assert check_validity(p, now=900, price=94.0, data_state="OK", side="SELL", trend_1h="DOWN") == []
    assert check_validity(p, now=900, price=94.0, data_state="OK", side="BUY", trend_1h=None)


def test_signature_known_vector():
    # Ví dụ trong tài liệu chính thức của Binance
    q = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
         "&recvWindow=5000&timestamp=1499827319559")
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    assert sign(q, secret) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def handler(req: httpx.Request):
    assert req.headers.get("X-MBX-APIKEY") == "k" or req.url.path == "/api/v3/time"
    if req.url.path == "/api/v3/time":
        return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
    if req.url.path == "/api/v3/account":
        return httpx.Response(200, json={"balances": [
            {"asset": "USDT", "free": "900", "locked": "50"},
            {"asset": "BTC", "free": "0.5", "locked": "0"},
            {"asset": "DOGE", "free": "10", "locked": "0"}]})
    if req.url.path == "/api/v3/myTrades":
        assert "signature=" in str(req.url)
        return httpx.Response(200, json=[
            {"symbol": "BTCUSDT", "id": 1, "qty": "0.5", "price": "100", "commission": "0.05",
             "commissionAsset": "USDT", "time": 1000, "isBuyer": True}])
    return httpx.Response(404)


async def test_sync_summary_protection_and_errors():
    from app.core.config import settings
    settings.binance_api_key, settings.binance_api_secret = "k", "s"  # settings là singleton, đã nạp từ trước
    await init_db()
    async with SessionLocal() as s:
        s.add(Symbol(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", tick_size=0.01,
                     step_size=0.001, min_notional=5))
        await s.commit()
    market.set_symbols(["BTCUSDT"])
    market.symbols["BTCUSDT"].last = 110.0
    async with httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler)) as c:
        acct = BinanceAccount(c)
        res = await service.sync_account(acct)
        assert res == {"ok": True, "new_trades": 1}
        assert (await service.sync_account(acct))["new_trades"] == 0  # idempotent
    sm = await service.summary()
    assert sm["source"] == "binance" and sm["open_positions"] == 1
    p = sm["positions"][0]
    assert p["avg_cost"] == pytest.approx(100.1) and p["qty"] == 0.5
    assert p["unrealized_pnl"] == pytest.approx((110 - 100.1) * 0.5)
    assert p["remaining_risk_usdt"] == pytest.approx(55.0) and p["risk_note"]  # chưa có stop
    assert sm["equity"] == pytest.approx(950 + 55)  # (900+50 USDT) + 0.5 BTC * 110
    assert sm["free_quote"] == pytest.approx(900) and "DOGE" in sm["unvalued_assets"]
    assert not p["balance_mismatch"]

    out = await service.set_protection("BTCUSDT", 95.0, 130.0)
    assert out["protection_status"] == "PLANNED_NOT_PLACED"
    with pytest.raises(ValueError):
        await service.set_protection("BTCUSDT", 140.0, 130.0)
    with pytest.raises(ValueError):
        await service.set_protection("ETHUSDT", 1.0, 2.0)
    er = await service.equity_and_risk()
    assert er["open_risk"] == pytest.approx((110 - 95) * 0.5)

    def bad(req):
        return httpx.Response(401, text='{"code":-2015}')
    async with httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(bad)) as c:
        res = await service.sync_account(BinanceAccount(c))
    assert res["ok"] is False and "401" in res["error"] and "signature" not in res["error"]
