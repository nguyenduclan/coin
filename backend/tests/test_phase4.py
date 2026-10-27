import os

os.environ.update(ADMIN_PASSWORD="pw", SESSION_SECRET="x" * 40,
                  DATABASE_URL="sqlite+aiosqlite:////tmp/test_p4.db")

import pytest  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.risk.sizing import HARD_MAX_RISK_PCT, round_down, size_position  # noqa: E402
from app.trading.strategy import build_setup, check_validity  # noqa: E402

BASE = dict(equity=1000, risk_pct=0.5, entry=100.0, stop=98.0, take_profit=104.0, fee_rate=0.001,
            slippage_pct=0.05, step_size=0.001, min_notional=5, free_quote=1000, max_alloc_pct=50)


def test_round_down_exact():
    assert round_down(0.123456, 0.001) == 0.123
    assert round_down(1.0, 0.1) == 1.0  # không lỗi float kiểu 0.9999
    assert round_down(5.5, 0) == 5.5


def test_sizing_matches_formula():
    r = size_position(**BASE)
    assert r["ok"]
    slip = 0.0005
    ee, es, et = 100 * (1 + slip), 98 * (1 - slip), 104 * (1 - slip)
    rpu = (ee - es) + 0.001 * (ee + es)
    assert r["qty"] == pytest.approx(round_down(5.0 / rpu, 0.001))
    assert r["risk_usdt"] <= 5.0 + 1e-9  # làm tròn xuống không bao giờ vượt ngân sách
    assert r["risk_pct_equity"] <= 0.5
    rew = (et - ee) - 0.001 * (et + ee)
    assert r["rr_after_costs"] == pytest.approx(rew / rpu)
    assert r["rr_after_costs"] < 2.0  # phí + trượt giá làm R:R thô (2.0) giảm


def test_sizing_caps_by_alloc_and_cash():
    r = size_position(**{**BASE, "max_alloc_pct": 5})  # 50 USDT -> ~0.5 coin
    assert r["ok"] and r["notional"] <= 50 and any("vốn tối đa" in x for x in r["reasons"])
    r = size_position(**{**BASE, "free_quote": 30})
    assert r["ok"] and r["notional"] <= 30 and any("số dư" in x for x in r["reasons"])


def test_sizing_rejections():
    assert not size_position(**{**BASE, "risk_pct": HARD_MAX_RISK_PCT + 0.1})["ok"]
    assert not size_position(**{**BASE, "stop": 101})["ok"]
    assert not size_position(**{**BASE, "free_quote": 3})["ok"]  # dưới min notional
    r = size_position(**{**BASE, "open_risk_usdt": 19.0})  # 19 + 5 > 20 (2% vốn)
    assert not r["ok"] and "Tổng rủi ro" in r["reasons"][0]
    assert not size_position(**{**BASE, "take_profit": 100.05})["ok"]  # lãi không bù phí


def report(**over):
    tf = {"15m": {"trend": "UP", "atr14": 1.0, "ema20": 100.0, "rsi14": 52.0, "support": [98.9]},
          "1h": {"trend": "UP", "resistance": [110.0]}, "4h": {"trend": "SIDEWAYS"}}
    for k, v in over.items():
        tf[k] = v
    return {"timeframes": tf}


F = {"tick_size": 0.01, "step_size": 0.001, "min_notional": 5}


def test_setup_ok_and_stop_uses_support():
    s = build_setup(report(), F, 100.2)
    assert s["ok"], s
    p = s["proposal"]
    assert p["stop"] == pytest.approx(98.65)  # hỗ trợ 98.9 - 0.25 ATR
    assert p["take_profit"] == 110.0  # kháng cự 1h đủ xa
    assert p["stop"] < p["entry"] < p["take_profit"] and p["rr_after_costs"] >= settings.min_rr
    assert p["risk_pct_equity"] <= settings.risk_pct


def test_setup_rejections():
    assert not build_setup(report(**{"1h": {"trend": "DOWN"}}), F, 100.0)["ok"]
    assert not build_setup(report(), F, 103.0)["ok"]  # đuổi giá, cách EMA20 > 0.5 ATR
    r = report(); r["timeframes"]["15m"]["rsi14"] = 80.0
    assert not build_setup(r, F, 100.0)["ok"]
    r = report(); r["timeframes"]["4h"] = {"trend": "DOWN"}
    assert not build_setup(r, F, 100.0)["ok"]
    r = report(); del r["timeframes"]["15m"]["atr14"]
    assert "Thiếu chỉ báo 15m" in build_setup(r, F, 100.0)["reasons"]


def test_validity():
    p = {"expires_at": 1000.0, "ref_price": 100.0, "stop": 98.0}
    assert check_validity(p, now=900, price=100.1, data_state="OK", trend_1h="UP") == []
    assert "Đề xuất đã hết hạn" in check_validity(p, now=1001, price=100, data_state="OK", trend_1h=None)
    assert check_validity(p, now=900, price=101, data_state="OK", trend_1h=None)  # lệch 1% > 0.3%
    assert check_validity(p, now=900, price=100, data_state="STALE", trend_1h=None)
    assert check_validity(p, now=900, price=97.9, data_state="OK", trend_1h=None)
    assert check_validity(p, now=900, price=100, data_state="OK", trend_1h="DOWN")
