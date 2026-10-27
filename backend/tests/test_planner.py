import os
import time

os.environ.update(ADMIN_PASSWORD="pw", SESSION_SECRET="x" * 40,
                  DATABASE_URL="sqlite+aiosqlite:////tmp/test_pl.db")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from app.analysis import engine as eng  # noqa: E402
from app.api import routes  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.data.state import market  # noqa: E402
from app.models import PaperAccount, PaperPosition, PaperTrade, Proposal, Symbol  # noqa: E402
from app.paper import service as paper  # noqa: E402
from app.portfolio import service as portfolio  # noqa: E402
from app.trading import planner  # noqa: E402
from app.trading.strategy import trailing_suggestion  # noqa: E402

FEE, SLIP = settings.fee_rate, settings.slippage_pct / 100


# ---------- hàm thuần ----------
def test_entry_deviation():
    assert planner.entry_deviation_notes(None, 100.0) == ([], True)
    assert planner.entry_deviation_notes(100.1, 100.0)[1] is True
    n, ok = planner.entry_deviation_notes(105.0, 100.0)
    assert not ok and "CAO hơn" in n[0]
    n, ok = planner.entry_deviation_notes(95.0, 100.0)
    assert not ok and "THẤP hơn" in n[0] and "lệnh chờ" in n[0]
    n, ok = planner.entry_deviation_notes(0.00002, 0.0000044)  # ví dụ thực tế: gấp ~4,5 lần giá thị trường
    assert not ok and any("số 0" in x for x in n)


def test_levels_default_and_with_support_resistance():
    lv = planner.compute_levels(100.0, 1.0, [], [], 0.0)
    assert lv["stop"] == pytest.approx(98.5) and lv["tp1"] == pytest.approx(103.0) and lv["tp2"] == pytest.approx(104.5)
    lv = planner.compute_levels(100.0, 1.0, [99.0], [], 0.0)  # hỗ trợ gần hơn 1,5 ATR -> stop ngay dưới hỗ trợ
    assert lv["stop"] == pytest.approx(98.75)
    lv = planner.compute_levels(100.0, 1.0, [], [102.0, 106.0], 0.0)  # 102 < 1.5R nên bỏ qua, lấy 106
    assert lv["tp1"] == 106.0 and lv["tp2"] == 106.0
    lv = planner.compute_levels(0.00000442, 0.00000005, [], [], 1e-8)  # làm tròn theo tick của PEPE
    assert lv["stop"] == 0.00000434 and lv["stop"] < 0.00000442 < lv["tp1"]


def test_costs_formula():
    q = 10.0
    c = planner.costs(100.0, 104.0, q)
    e, x = 100 * (1 + SLIP), 104 * (1 - SLIP)
    assert c == pytest.approx(q * (x - e) - FEE * q * (e + x))
    assert planner.costs(100.0, 100.0, q) < 0  # mua rồi bán cùng giá luôn lỗ phí + trượt giá


def test_trailing():
    pos = {"avg_cost": 100.0, "stop": 98.0}
    assert trailing_suggestion({"avg_cost": 100.0, "stop": None}, 110, 1.0) is None
    assert trailing_suggestion(pos, 101.0, 1.0) is None  # chưa lãi 1R
    s = trailing_suggestion(pos, 102.5, 1.0)
    assert s["suggested_stop"] == pytest.approx(100 * (1 + 2 * FEE + 2 * SLIP)) and "hòa vốn" in s["reason"]
    s = trailing_suggestion(pos, 110.0, 1.0)
    assert s["suggested_stop"] == pytest.approx(108.5)  # 110 - 1.5 ATR
    assert trailing_suggestion({"avg_cost": 100.0, "stop": 109.0}, 110.0, 1.0) is None  # không bao giờ hạ stop
    assert trailing_suggestion(pos, 110.0, None) is None


# ---------- plan() với chỉ báo giả lập ----------
SUMS = {}


def sums(t1h="UP", t4h="SIDEWAYS", t15="UP", ema20=100.0, rsi=52.0, atr=2.0):
    return {"5m": {"trend": "SIDEWAYS"},
            "15m": {"trend": t15, "ema20": ema20, "rsi14": rsi, "atr14": atr, "support": [], "resistance": [],
                    "macd_hist": 0.1},
            "1h": {"trend": t1h, "resistance": []}, "4h": {"trend": t4h}, "1d": {"trend": "UP"}}


@pytest.fixture
async def env(monkeypatch):
    await init_db()
    async with SessionLocal() as s:
        await s.execute(delete(Symbol).where(Symbol.symbol == "PLNUSDT"))
        s.add(Symbol(symbol="PLNUSDT", base_asset="PLN", quote_asset="USDT", tick_size=0.01, step_size=0.001,
                     min_notional=5))
        for m in (PaperPosition, PaperTrade, PaperAccount, Proposal):
            await s.execute(delete(m))
        await s.commit()
    portfolio.account_state.update(balances={}, locked={}, at=None)
    settings.trading_mode, settings.ai_provider, settings.account_equity_usdt = "ANALYSIS_ONLY", "none", 1000.0

    async def fake_load(symbol, tf, limit=500):
        return pd.DataFrame({"tf": [tf]})

    monkeypatch.setattr(eng, "load_df", fake_load)
    monkeypatch.setattr(planner.ta, "summarize", lambda df: SUMS["v"][df["tf"][0]])
    SUMS["v"] = sums()
    set_market()
    yield


def set_market(bid=100.1, ask=100.2, last=100.2):
    market.set_symbols(["PLNUSDT"])
    market.ws_connected = True
    now = time.time()
    market.update_ticker("PLNUSDT", {"c": str(last), "P": "0", "v": "1", "q": "1", "E": now * 1000}, now=now)
    market.update_book("PLNUSDT", bid, ask, now=now)


async def test_plan_buy_now_numbers(env):
    p = await planner.plan("PLNUSDT", 10.0)
    assert p["verdict"] == "BUY_NOW" and p["can_propose"]
    pl = p["plan"]
    assert pl["stop"] < pl["entry"] < pl["take_profit"] <= pl["take_profit_2"]
    assert pl["notional"] <= 10.0 and pl["notional"] >= 9.5  # dùng gần đủ số tiền, làm tròn xuống theo lot size
    assert pl["risk_usdt"] > 0 and pl["rr_tp1"] > 1.5
    assert pl["risk_usdt"] == pytest.approx(-planner.costs(pl["entry"], pl["stop"], pl["qty"]))
    assert len(p["exit_rules"]) == 5 and p["limits"]


async def test_plan_no_trade_still_gives_numbers_but_flags(env):
    SUMS["v"] = sums(t1h="DOWN", t4h="DOWN")
    p = await planner.plan("PLNUSDT", 10.0)
    assert p["verdict"] == "NO_TRADE" and p["plan"]["stop"] < p["plan"]["entry"] and p["reasons"]


async def test_plan_wait_pullback_has_zone(env):
    set_market(103.0, 103.1, 103.1)  # cách EMA20 (100) hơn 0,5 ATR
    p = await planner.plan("PLNUSDT", 10.0)
    assert p["verdict"] == "WAIT_PULLBACK"
    assert p["wait_zone"]["low"] == pytest.approx(99.0) and p["wait_zone"]["high"] == pytest.approx(101.0)


async def test_plan_user_entry_and_limits(env):
    p = await planner.plan("PLNUSDT", 10.0, user_entry=95.0)
    assert not p["can_propose"] and any("lệnh chờ" in n for n in p["notes"])
    p = await planner.plan("PLNUSDT", 3.0)  # dưới min notional 5
    assert not p["can_propose"] and any("tối thiểu" in n for n in p["notes"])
    p = await planner.plan("PLNUSDT", 5000.0)  # vượt giới hạn rủi ro/vốn mỗi coin của hệ thống
    assert p["plan"]["notional"] < 5000 and any("giảm" in n for n in p["notes"])
    assert p["plan"]["risk_pct_equity"] <= settings.risk_pct + 1e-6


async def test_plan_errors_and_missing_data(env):
    assert (await planner.plan("NOPEUSDT", 10))["verdict"] == "ERROR"
    assert (await planner.plan("PLNUSDT", -1))["verdict"] == "ERROR"
    assert (await planner.plan("PLNUSDT", 10, user_entry=0))["verdict"] == "ERROR"
    market.symbols["PLNUSDT"].received_at -= 60
    assert (await planner.plan("PLNUSDT", 10))["verdict"] == "DATA_UNAVAILABLE"


# ---------- tạo đề xuất từ kế hoạch ----------
async def test_propose_requires_ack_and_paper_fills(env):
    settings.trading_mode = "PAPER_TRADING"
    SUMS["v"] = sums(t1h="DOWN", t4h="DOWN")
    with pytest.raises(HTTPException) as e:  # kết luận xấu mà chưa xác nhận đã đọc cảnh báo
        await routes.plan_propose(routes.PlanProposeIn(symbol="PLNUSDT", amount=10.0))
    assert e.value.status_code == 409 and "NO_TRADE" in e.value.detail
    r = await routes.plan_propose(routes.PlanProposeIn(symbol="PLNUSDT", amount=10.0, acknowledge=True))
    assert r["paper_fill"]["ok"] and r["verdict"] == "NO_TRADE"
    pos = await paper.get_position("PLNUSDT")
    assert pos and pos["stop"] and pos["take_profit"]
    async with SessionLocal() as s:
        pr = await s.get(Proposal, r["proposal_id"])
    assert pr.status == "PAPER_FILLED"


async def test_propose_blocked_cases(env):
    settings.trading_mode = "ANALYSIS_ONLY"
    with pytest.raises(HTTPException, match="ANALYSIS_ONLY"):
        await routes.plan_propose(routes.PlanProposeIn(symbol="PLNUSDT", amount=10.0))
    settings.trading_mode = "PAPER_TRADING"
    with pytest.raises(HTTPException):  # giá nhập thấp hơn thị trường -> không có lệnh chờ
        await routes.plan_propose(routes.PlanProposeIn(symbol="PLNUSDT", amount=10.0, entry_price=90.0, acknowledge=True))
    market.symbols["PLNUSDT"].received_at -= 60
    with pytest.raises(HTTPException):
        await routes.plan_propose(routes.PlanProposeIn(symbol="PLNUSDT", amount=10.0, acknowledge=True))


async def test_propose_live_modes_wait_for_confirmation(env):
    settings.trading_mode = "BINANCE_TESTNET"
    r = await routes.plan_propose(routes.PlanProposeIn(symbol="PLNUSDT", amount=10.0))
    assert r["verdict"] == "BUY_NOW" and "paper_fill" not in r and r["confirm_token"]
    async with SessionLocal() as s:
        assert (await s.get(Proposal, r["proposal_id"])).status == "PROPOSED"  # chưa gửi gì, chờ bấm xác nhận
    r2 = await routes.plan_propose(routes.PlanProposeIn(symbol="PLNUSDT", amount=10.0))
    async with SessionLocal() as s:
        assert (await s.get(Proposal, r["proposal_id"])).status == "SUPERSEDED"  # chỉ giữ một đề xuất chờ
        assert (await s.get(Proposal, r2["proposal_id"])).status == "PROPOSED"
