import os
import time

os.environ.update(ADMIN_PASSWORD="pw", SESSION_SECRET="x" * 40,
                  DATABASE_URL="sqlite+aiosqlite:////tmp/test_p6.db")

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.backtest import engine as bt  # noqa: E402
from app.backtest import service as bt_service  # noqa: E402
from app.binance.rest import BinanceRest  # noqa: E402
from app.core.config import Settings, settings  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.data.state import market  # noqa: E402
from app.indicators import ta  # noqa: E402
from app.models import Candle  # noqa: E402
from app.paper import service as paper  # noqa: E402
from app.validation import evaluate  # noqa: E402


# ---------- chỉ báo dạng chuỗi: khớp bản live và không look-ahead ----------
def rand_df(n=400, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.5, "low": np.minimum(o, c) - 0.5,
                         "close": c, "volume": rng.uniform(1, 2, n)})


def test_series_frame_matches_summarize_and_is_causal():
    df = rand_df()
    full = ta.series_frame(df)
    for i in (80, 200, 399):
        s = ta.summarize(df.iloc[: i + 1])
        row = full.iloc[i]
        assert row["trend"] == s["trend"]
        for k_s, k_f in (("ema20", "ema20"), ("rsi14", "rsi14"), ("atr14", "atr14")):
            assert row[k_f] == pytest.approx(s[k_s])
        # Cắt bỏ tương lai không làm đổi giá trị tại i
        assert ta.series_frame(df.iloc[: i + 1]).iloc[i]["ema20"] == pytest.approx(row["ema20"])
    assert (full["trend"].iloc[:59] == "INSUFFICIENT").all() and full["trend"].iloc[59] != "INSUFFICIENT"


# ---------- backtest: cơ chế mô phỏng (chiến lược được giả lập để kiểm soát từng nến) ----------
M15 = 900_000
N15 = 6500


def make_tf(ms, n, closes=None, opens=None, highs=None, lows=None):
    t = np.arange(n) * ms
    c = np.full(n, 100.0) if closes is None else closes
    o = c.copy() if opens is None else opens
    h = np.full(n, 100.2) if highs is None else highs
    l = np.full(n, 99.8) if lows is None else lows
    return pd.DataFrame({"open_time": t, "open": o, "high": h, "low": l, "close": c,
                         "volume": 1.0, "close_time": t + ms - 1})


def scenario(patches: dict[int, dict]):
    """15m phẳng ở 100; close mỗi nến khác nhau một chút để nhận diện nến. patches: {k: {field: value}}."""
    closes = 100 + np.arange(N15) * 1e-7
    df = make_tf(M15, N15, closes=closes, opens=np.full(N15, 100.0))
    for k, p in patches.items():
        for f, v in p.items():
            df.loc[k, f] = v
    return {"15m": df, "1h": make_tf(3_600_000, 1700), "4h": make_tf(14_400_000, 450),
            "1d": make_tf(86_400_000, 80)}, closes


def stub_setup(targets: set[float]):
    def build_setup(rep, filters, price, account=None):
        if any(abs(price - x) < 1e-12 for x in targets):
            return {"ok": True, "proposal": {"side": "BUY", "qty": 1.0, "stop": 98.0,
                                             "take_profit": 104.0, "risk_usdt": 5.0}}
        return {"ok": False, "reasons": ["stub"]}
    return build_setup


def run(monkeypatch, patches, ks):
    dfs, closes = scenario(patches)
    monkeypatch.setattr(bt, "build_setup", stub_setup({closes[k] for k in ks}))
    return bt.run_symbol("TESTUSDT", bt.prepare(dfs), {}, 1000.0)


K0 = 6200  # sau giai đoạn khởi động của khung 1d (60 ngày = 5760 nến 15m)
FEE, SLIP = settings.fee_rate, settings.slippage_pct / 100


def test_take_profit_fill_and_pnl(monkeypatch):
    r = run(monkeypatch, {K0 + 3: {"high": 104.5}}, [K0])
    t = r["trades"][0]
    assert t["reason"] == "take_profit" and t["exit"] == 104.0 and t["bars"] == 2
    fill = 100.0 * (1 + SLIP)  # khớp ở giá MỞ nến kế tiếp + trượt giá, không phải giá đóng nến tín hiệu
    assert t["entry"] == pytest.approx(fill)
    pnl = 104.0 - 104.0 * FEE - fill - fill * FEE
    assert t["pnl"] == pytest.approx(pnl) and t["r"] == pytest.approx(pnl / 5.0)
    assert r["equity_end"] == pytest.approx(1000 + pnl)


def test_stop_fill_with_slippage(monkeypatch):
    r = run(monkeypatch, {K0 + 3: {"low": 97.0}}, [K0])
    t = r["trades"][0]
    assert t["reason"] == "stop" and t["exit"] == pytest.approx(98.0 * (1 - SLIP)) and t["pnl"] < 0


def test_stop_wins_when_candle_touches_both(monkeypatch):
    r = run(monkeypatch, {K0 + 2: {"low": 97.0, "high": 105.0}}, [K0])
    assert r["trades"][0]["reason"] == "stop"  # giả định bi quan


def test_gap_skips_and_no_lookahead_entry(monkeypatch):
    r = run(monkeypatch, {K0 + 1: {"open": 97.0, "low": 96.0}}, [K0])
    assert r["trades"] == [] and r["skipped"]["gap_invalidated"] == 1
    r = run(monkeypatch, {K0 + 1: {"open": 100.5, "high": 100.6}}, [K0])  # lệch 0.5% > 0.3%
    assert r["trades"] == [] and r["skipped"]["gap_invalidated"] == 1


def test_two_sequential_trades_compound_cash(monkeypatch):
    r = run(monkeypatch, {K0 + 3: {"high": 104.5}, K0 + 53: {"high": 104.5}}, [K0, K0 + 50])
    assert [t["reason"] for t in r["trades"]] == ["take_profit", "take_profit"]
    assert r["trades"][0]["exit_time"] < r["trades"][1]["entry_time"]
    assert r["equity_end"] == pytest.approx(1000 + sum(t["pnl"] for t in r["trades"]))


def test_open_position_closed_at_end_flagged(monkeypatch):
    r = run(monkeypatch, {}, [N15 - 5])
    t = r["trades"][-1]
    assert t["reason"] == "end_of_data" and t.get("open_at_end")


def test_insufficient_history_returns_error():
    dfs = {tf: make_tf(ms, 10) for tf, ms in (("15m", M15), ("1h", 3_600_000), ("4h", 14_400_000), ("1d", 86_400_000))}
    assert "error" in bt.run_symbol("X", bt.prepare(dfs), {}, 1000.0)


def test_split_report_segments_and_regimes():
    tr = [{"entry_time": t, "pnl": p, "fees": 0.1, "r": p / 5, "regime": rg}
          for t, p, rg in [(10, 5, "UP"), (40, -5, "UP"), (60, 5, "SIDEWAYS"), (90, -5, "UP")]]
    rep = bt.split_report(tr, [], 0, 100)
    assert [rep["segments"][k]["trades"] for k in ("development", "validation", "out_of_sample")] == [2, 1, 1]
    assert [w["trades"] for w in rep["windows"]] == [1, 1, 1, 1]
    rg = bt.by_regime(tr)
    assert rg["UP"]["trades"] == 3 and rg["SIDEWAYS"]["trades"] == 1
    m = bt.metrics(tr, [(0, 100), (1, 120), (2, 90)])
    assert m["max_drawdown_pct"] == pytest.approx(25.0) and m["win_rate"] == 0.5


# ---------- tải lịch sử ----------
async def test_download_paginates_and_skips_unclosed():
    await init_db()
    now = int(time.time() * 1000)
    base = now - 3000 * 3_600_000
    calls = []

    def handler(req: httpx.Request):
        start = int(req.url.params["startTime"])
        calls.append(start)
        n = 1000 if len(calls) < 3 else 5
        rows = [[start + i * 3_600_000, "1", "2", "0.5", "1.5", "10", start + (i + 1) * 3_600_000 - 1]
                for i in range(n)]
        return httpx.Response(200, json=rows)

    async with httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler)) as c:
        n = await bt_service.download(BinanceRest(c), "PGUSDT", "1h", base, now, pause=0)
    assert len(calls) == 3 and calls[1] - calls[0] == 1000 * 3_600_000
    async with SessionLocal() as s:
        cnt = await s.scalar(select(func.count()).select_from(Candle).where(Candle.symbol == "PGUSDT"))
    assert cnt == n == 2005
    async with httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(handler)) as c:
        calls.clear()
        await bt_service.download(BinanceRest(c), "PGUSDT", "1h", base, now, pause=0)  # chạy lại: không trùng
    async with SessionLocal() as s:
        assert await s.scalar(select(func.count()).select_from(Candle).where(Candle.symbol == "PGUSDT")) == 2005


# ---------- paper trading ----------
def set_market(sym, bid, ask, last=None):
    market.set_symbols([sym])
    market.ws_connected = True
    now = time.time()
    market.update_ticker(sym, {"c": str(last or bid), "P": "0", "v": "1", "q": "1", "E": now * 1000}, now=now)
    market.update_book(sym, bid, ask, now=now)


PROP = {"side": "BUY", "qty": 2.0, "stop": 95.0, "take_profit": 110.0, "risk_usdt": 10.0}


async def test_paper_buy_stop_and_accounting():
    await init_db()
    await paper.reset()
    set_market("PAPUSDT", 100.0, 100.1)
    res = await paper.execute_proposal("PAPUSDT", PROP, None)
    fill = 100.1 * (1 + SLIP)
    assert res["ok"] and res["price"] == pytest.approx(fill)
    fee = 2 * fill * FEE
    er = await paper.equity_and_risk()
    assert er["free_quote"] == pytest.approx(1000 - 2 * fill - fee)
    assert (await paper.execute_proposal("PAPUSDT", PROP, None))["ok"] is False  # không mua thêm
    assert await paper.check_exits() == []  # giá còn trên stop
    set_market("PAPUSDT", 94.0, 94.1)
    done = await paper.check_exits()
    assert done[0]["ok"] and done[0]["price"] == pytest.approx(94.0 * (1 - SLIP))
    assert await paper.get_position("PAPUSDT") is None
    perf = await paper.performance_report()
    assert perf["closed_trades"] == 1 and perf["net_pnl"] < 0 and perf["avg_r"] < 0
    s = await paper.summary()
    assert s["realized_pnl"] == pytest.approx(perf["net_pnl"])
    assert s["equity"] == pytest.approx(1000 + perf["net_pnl"])  # tiền = vốn + PnL, không thất thoát


async def test_paper_take_profit_limit_and_stale_guard():
    await paper.reset()
    set_market("PAPUSDT", 100.0, 100.1)
    await paper.execute_proposal("PAPUSDT", PROP, None)
    set_market("PAPUSDT", 111.0, 111.1)
    market.symbols["PAPUSDT"].received_at -= 60  # dữ liệu cũ -> không hành động
    assert await paper.check_exits() == []
    set_market("PAPUSDT", 111.0, 111.1)
    done = await paper.check_exits()
    assert done[0]["price"] == 110.0  # TP là lệnh limit khớp đúng giá TP
    assert (await paper.performance_report())["net_pnl"] > 0


async def test_paper_rejects_stale_and_insufficient_cash():
    await paper.reset()
    set_market("PAPUSDT", 100.0, 100.1)
    market.symbols["PAPUSDT"].received_at -= 60
    assert (await paper.execute_proposal("PAPUSDT", PROP, None))["ok"] is False
    set_market("PAPUSDT", 100.0, 100.1)
    big = {**PROP, "qty": 50.0}  # ~5000 USDT > 1000 giả lập
    assert (await paper.execute_proposal("PAPUSDT", big, None))["reason"] == "Không đủ tiền giả lập"


# ---------- ngưỡng chấp nhận, chế độ ----------
def test_readiness_thresholds():
    good = {"profit_factor": 1.5, "closed_trades": 40, "expectancy": 0.5}
    oos = {"trades": 35, "profit_factor": 1.3}
    assert all(i["passed"] for i in evaluate(31, good, 5.0, oos))
    items = {i["id"]: i for i in evaluate(10, {"profit_factor": 1.1, "closed_trades": 5, "expectancy": -1}, 12.0, None)}
    assert not any(i["passed"] for i in items.values())
    assert not items["paper_days"]["passed"] and items["bt_trades"]["note"]


def test_mode_validation():
    with pytest.raises(ValidationError):
        Settings(trading_mode="LIVE")
    with pytest.raises(ValidationError):
        Settings(trading_mode="BINANCE_TESTNET", binance_env="mainnet")
    with pytest.raises(ValidationError):
        Settings(trading_mode="WHATEVER")
    s = Settings(trading_mode="BINANCE_TESTNET", binance_env="testnet")
    assert s.binance_rest_url == "https://testnet.binance.vision" and "testnet" in s.binance_ws_url
    assert s.trade_rest_url == "https://testnet.binance.vision"
    # Paper/Analysis: giá luôn là mainnet thật, kể cả khi key là key testnet
    p = Settings(trading_mode="PAPER_TRADING", binance_env="testnet")
    assert p.binance_rest_url == "https://api.binance.com" and "testnet" not in p.binance_ws_url
    assert p.trade_rest_url == "https://testnet.binance.vision"
    assert Settings(trading_mode="PAPER_TRADING", market_env="testnet").binance_rest_url.endswith("testnet.binance.vision")
