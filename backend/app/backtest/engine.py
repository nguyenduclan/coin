"""Backtest chiến lược trend_pullback bằng CHÍNH hàm build_setup đang chạy thật.

Chống look-ahead:
- Đồng hồ là nến 15m; tại thời điểm đóng nến t chỉ dùng nến khung khác có close_time <= t.
- Chỉ báo tính tuần tự từ quá khứ (ta.series_frame).
- Tín hiệu tại lúc đóng nến i, lệnh khớp ở GIÁ MỞ nến i+1 (cộng trượt giá).
- Stop/TP kiểm tra bằng high/low; nếu một nến chạm cả hai, coi như stop khớp trước (bi quan).
Mọi tham số (rủi ro, phí, trượt giá, ngưỡng R:R) lấy từ settings, KHÔNG tối ưu theo dữ liệu quá khứ.
"""
from collections import Counter

import numpy as np
import pandas as pd

from ..core.config import settings
from ..indicators import ta
from ..portfolio.ledger import max_drawdown, performance
from ..trading.strategy import build_setup

MAX_HOLD_BARS = 672  # 7 ngày với nến 15m
LEVEL_WINDOW = 300
COLS = ("close", "ema20", "atr14", "rsi14")
LIMITS = [
    "Chỉ thoát toàn bộ (stop, TP, 1h&4h cùng giảm, quá 7 ngày); chưa mô phỏng REDUCE nửa vị thế",
    "Khớp giả định ở giá mở nến kế tiếp cộng trượt giá cố định; không mô phỏng order book/thanh khoản",
    "Nến chạm cả stop lẫn TP: tính stop khớp trước",
    "Chỉ báo tính trên toàn bộ lịch sử nên có sai khác rất nhỏ so với bản chạy thật (cửa sổ 500 nến)",
    "Không có tin tức/AI trong backtest",
    "Kết quả quá khứ không đảm bảo kết quả tương lai",
]


def prepare(dfs: dict[str, pd.DataFrame]) -> dict[str, dict]:
    out = {}
    for tf, df in dfs.items():
        df = df.sort_values("open_time").reset_index(drop=True)
        ind = ta.series_frame(df)
        out[tf] = {"df": df, "close_time": df["close_time"].to_numpy(),
                   "trend": ind["trend"].to_numpy(), **{c: ind[c].to_numpy() for c in COLS}}
    return out


def _snap(prep: dict, tf: str, t: int, levels: bool) -> dict | None:
    p = prep.get(tf)
    if p is None:
        return None
    j = int(np.searchsorted(p["close_time"], t, side="right")) - 1
    if j < 0:
        return None
    d = {"trend": p["trend"][j], "atr14": float(p["atr14"][j]), "ema20": float(p["ema20"][j]),
         "rsi14": float(p["rsi14"][j]), "support": [], "resistance": []}
    if levels:
        d.update(ta.swing_levels(p["df"].iloc[max(0, j - LEVEL_WINDOW + 1): j + 1]))
    return d


def _report(prep: dict, t: int) -> dict:
    tr = {tf: _snap(prep, tf, t, levels=False) for tf in prep}
    t1h, t4h, t15 = (tr.get(k) and tr[k]["trend"] for k in ("1h", "4h", "15m"))
    # Chỉ tính hỗ trợ/kháng cự khi qua cổng đầu tiên của chiến lược (tiết kiệm CPU)
    if t1h == "UP" and t4h != "DOWN" and t15 not in (None, "DOWN", "INSUFFICIENT"):
        for tf in ("15m", "1h"):
            tr[tf] = _snap(prep, tf, t, levels=True)
    return {"timeframes": {tf: v for tf, v in tr.items() if v}}


def run_symbol(symbol: str, prep: dict, filters: dict, equity0: float,
               start_ms: int | None = None, end_ms: int | None = None) -> dict:
    base = prep["15m"]
    df, ct = base["df"], base["close_time"]
    op, hi, lo, cl = (df[c].to_numpy() for c in ("open", "high", "low", "close"))
    n = len(df)
    slip, fee = settings.slippage_pct / 100, settings.fee_rate
    # Bỏ giai đoạn khởi động: mọi khung phải có đủ nến tối thiểu
    warm = max((p["close_time"][ta.MIN_CANDLES - 1] for p in prep.values()
                if len(p["close_time"]) >= ta.MIN_CANDLES), default=None)
    if warm is None or any(len(p["close_time"]) < ta.MIN_CANDLES for p in prep.values()):
        return {"symbol": symbol, "error": "Không đủ nến lịch sử ở một số khung", "trades": []}
    lo_t = max(warm, start_ms or 0)
    hi_t = end_ms or int(ct[-1])

    cash, pos, pending_entry, pending_exit = equity0, None, None, None
    last_k, first_k = 0, None
    trades, curve, skipped = [], [], Counter()

    def close_pos(k: int, price: float, reason: str, t_exit: int):
        nonlocal cash, pos
        proceeds = pos["qty"] * price
        f = proceeds * fee
        cash += proceeds - f
        pnl = proceeds - f - pos["cost"] - pos["entry_fee"]
        trades.append({"symbol": symbol, "entry_time": pos["t"], "exit_time": t_exit,
                       "entry": pos["fill"], "exit": price, "qty": pos["qty"],
                       "fees": f + pos["entry_fee"], "pnl": pnl,
                       "r": pnl / pos["risk"] if pos["risk"] else None, "reason": reason,
                       "regime": pos["regime"], "bars": k - pos["k"]})
        pos = None

    for k in range(n):
        t = int(ct[k])
        if t < lo_t:
            continue
        if t > hi_t:
            break
        last_k = k
        first_k = k if first_k is None else first_k
        if pos and pending_exit:
            close_pos(k, op[k] * (1 - slip), pending_exit, int(df["open_time"][k]))
            pending_exit = None
        if pending_entry and not pos:
            p = pending_entry["proposal"]
            fill = op[k] * (1 + slip)
            cost = p["qty"] * fill
            if abs(op[k] / pending_entry["ref"] - 1) * 100 > settings.proposal_max_move_pct:
                skipped["gap_invalidated"] += 1
            elif op[k] <= p["stop"]:
                skipped["open_below_stop"] += 1
            elif cost * (1 + fee) > cash:
                skipped["no_cash"] += 1
            else:
                pos = {"qty": p["qty"], "fill": fill, "cost": cost, "entry_fee": cost * fee,
                       "stop": p["stop"], "tp": p["take_profit"], "risk": p["risk_usdt"],
                       "t": int(df["open_time"][k]), "k": k, "regime": pending_entry["regime"]}
                cash -= cost + pos["entry_fee"]
        pending_entry = None
        if pos and pos["k"] <= k:
            if lo[k] <= pos["stop"]:  # stop trước (bi quan); gap xuống dưới stop khớp ở giá mở
                px = min(op[k], pos["stop"]) * (1 - slip)
                close_pos(k, px, "stop", t)
            elif hi[k] >= pos["tp"]:
                close_pos(k, pos["tp"], "take_profit", t)
            elif k - pos["k"] >= MAX_HOLD_BARS:
                pending_exit = "timeout"
        curve.append((t, cash + (pos["qty"] * cl[k] if pos else 0.0)))
        if k >= n - 1:
            break
        rep = _report(prep, t)
        if pos and not pending_exit:
            tr = {tf: v["trend"] for tf, v in rep["timeframes"].items()}
            if tr.get("1h") == "DOWN" and tr.get("4h") == "DOWN":
                pending_exit = "trend_exit"
        elif not pos:
            eq = cash
            res = build_setup(rep, filters, float(cl[k]),
                              {"equity": eq, "free_quote": cash, "open_risk": 0.0})
            if res["ok"]:
                d1 = _snap(prep, "1d", t, levels=False)
                pending_entry = {"proposal": res["proposal"], "ref": float(cl[k]),
                                 "regime": d1["trend"] if d1 else "UNKNOWN"}
            else:
                skipped["no_setup"] += 1

    if pos:  # còn mở cuối dữ liệu: đóng theo giá đóng cuối, đánh dấu riêng
        close_pos(last_k, cl[last_k] * (1 - slip), "end_of_data", int(ct[last_k]))
        trades[-1]["open_at_end"] = True
        curve[-1] = (curve[-1][0], cash)
    first, last = (curve[0][1], curve[-1][1]) if curve else (equity0, equity0)
    return {"symbol": symbol, "trades": trades, "curve": curve, "skipped": dict(skipped),
            "start": curve[0][0] if curve else None, "end": curve[-1][0] if curve else None,
            "benchmark_buy_hold_pct": ((cl[last_k] / cl[first_k] - 1) * 100 if curve else None),
            "equity_start": first, "equity_end": last}


def metrics(trades: list[dict], curve: list[tuple[int, float]] | None = None) -> dict:
    m = performance([{"pnl": t["pnl"]} for t in trades])
    rs = [t["r"] for t in trades if t.get("r") is not None]
    m.update(trades=len(trades), fees=sum(t["fees"] for t in trades),
             avg_r=(sum(rs) / len(rs)) if rs else None)
    if curve:
        m["max_drawdown_pct"] = max_drawdown([v for _, v in curve])["max_drawdown_pct"]
    return m


def _slice(items, key, lo, hi):
    return [x for x in items if lo <= key(x) < hi]


def split_report(trades: list[dict], curve: list[tuple[int, float]], start: int, end: int) -> dict:
    """Tập phát triển 50% / validation 25% / out-of-sample 25% theo thời gian, và 4 cửa sổ liên tiếp.
    Chiến lược không có tham số nào được tinh chỉnh, nên đây là kiểm tra độ ổn định theo thời gian
    (không phải tối ưu hóa)."""
    span = max(1, end - start)
    cuts = [start, start + span * 0.5, start + span * 0.75, end + 1]
    names = ["development", "validation", "out_of_sample"]
    seg = {nm: metrics(_slice(trades, lambda x: x["entry_time"], cuts[i], cuts[i + 1]),
                       _slice(curve, lambda x: x[0], cuts[i], cuts[i + 1]))
           for i, nm in enumerate(names)}
    w = [start + span * i / 4 for i in range(5)]
    w[-1] = end + 1
    windows = [metrics(_slice(trades, lambda x: x["entry_time"], w[i], w[i + 1]),
                       _slice(curve, lambda x: x[0], w[i], w[i + 1])) for i in range(4)]
    return {"segments": seg, "windows": windows}


def by_regime(trades: list[dict]) -> dict:
    out = {}
    for r in sorted({t["regime"] for t in trades}):
        out[r] = metrics([t for t in trades if t["regime"] == r])
    return out


def run_all(per_symbol: dict[str, dict], filters: dict[str, dict], equity0: float,
            start_ms: int | None = None, end_ms: int | None = None) -> dict:
    """per_symbol: {symbol: prepared}. Mỗi mã chạy độc lập với cùng vốn ban đầu equity0."""
    runs = {s: run_symbol(s, p, filters.get(s, {}), equity0, start_ms, end_ms)
            for s, p in per_symbol.items()}
    ok = {s: r for s, r in runs.items() if "error" not in r}
    all_trades = [t for r in ok.values() for t in r["trades"]]
    out = {"params": {"equity_per_symbol": equity0, "risk_pct": settings.risk_pct,
                      "fee_rate": settings.fee_rate, "slippage_pct": settings.slippage_pct,
                      "min_rr": settings.min_rr, "max_alloc_pct_per_coin": settings.max_alloc_pct_per_coin},
           "limits": LIMITS, "symbols": {}, "errors": {s: r["error"] for s, r in runs.items() if "error" in r}}
    for s, r in ok.items():
        sr = split_report(r["trades"], r["curve"], r["start"], r["end"]) if r["curve"] else {}
        out["symbols"][s] = {"metrics": metrics(r["trades"], r["curve"]), "skipped": r["skipped"],
                             "period": [r["start"], r["end"]],
                             "benchmark_buy_hold_pct": r["benchmark_buy_hold_pct"],
                             "return_pct": (r["equity_end"] / equity0 - 1) * 100,
                             "by_regime": by_regime(r["trades"]), **sr}
    out["combined"] = {"metrics": metrics(all_trades), "by_regime": by_regime(all_trades),
                       "note": "Tổng hợp từ các mã chạy độc lập với cùng vốn ban đầu; không phải một danh mục dùng chung vốn"}
    if ok:
        starts, ends = [r["start"] for r in ok.values() if r["start"]], [r["end"] for r in ok.values() if r["end"]]
        if starts:
            cs = split_report(all_trades, [], min(starts), max(ends))
            out["combined"]["segments"] = cs["segments"]
            out["combined"]["windows"] = cs["windows"]
    out["trades"] = sorted(all_trades, key=lambda x: x["entry_time"])[-500:]
    return out
