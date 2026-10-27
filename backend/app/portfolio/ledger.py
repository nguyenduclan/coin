"""Sổ lệnh: giá vốn bình quân, PnL đã chốt, hiệu suất. Hàm thuần, dễ test.

Phương pháp giá vốn: bình quân gia quyền. Mua: phí cộng vào giá vốn. Bán: phí trừ vào PnL.
"""


def normalize_binance_trade(t: dict, base: str, quote: str) -> dict:
    """Chuyển myTrades của Binance sang dạng nội bộ, xử lý phí trả bằng quote/base/coin khác."""
    qty, price = float(t["qty"]), float(t["price"])
    fee, asset = float(t["commission"]), t["commissionAsset"]
    side = "BUY" if t["isBuyer"] else "SELL"
    fee_usdt, converted, net_qty = 0.0, True, qty
    if asset == quote:
        fee_usdt = fee
    elif asset == base:
        fee_usdt = fee * price
        if side == "BUY":
            net_qty = qty - fee  # phí trừ thẳng vào số coin nhận được
    elif fee > 0:
        converted = False  # vd BNB: chưa quy đổi, PnL sẽ thiếu phần phí này
    return {"id": f"binance:{t['symbol']}:{t['id']}", "symbol": t["symbol"], "side": side,
            "qty": net_qty, "price": price, "fee_usdt": fee_usdt,
            "fee_asset": asset, "fee_converted": converted, "time_ms": int(t["time"])}


def apply_trades(trades: list[dict]) -> dict:
    """trades: dict có symbol, side, qty, price, fee_usdt, time. Trả về
    {"positions": {sym: {...}}, "closed": [{"symbol","pnl","time"}]} (mỗi lệnh bán = 1 phần đã chốt)."""
    pos: dict[str, dict] = {}
    closed: list[dict] = []
    for t in sorted(trades, key=lambda x: x["time"]):
        p = pos.setdefault(t["symbol"], {"qty": 0.0, "avg_cost": 0.0, "realized_pnl": 0.0, "fees": 0.0})
        p["fees"] += t["fee_usdt"]
        if t["side"] == "BUY":
            cost = p["qty"] * p["avg_cost"] + t["qty"] * t["price"] + t["fee_usdt"]
            p["qty"] += t["qty"]
            p["avg_cost"] = cost / p["qty"] if p["qty"] else 0.0
        else:
            # Bán vượt số đang có (vd coin chuyển vào từ ngoài): chỉ tính phần biết giá vốn
            sold = min(t["qty"], p["qty"])
            pnl = (t["price"] - p["avg_cost"]) * sold - t["fee_usdt"]
            p["qty"] -= sold
            p["realized_pnl"] += pnl
            closed.append({"symbol": t["symbol"], "pnl": pnl, "time": t["time"]})
            if p["qty"] <= 1e-12:
                p["qty"], p["avg_cost"] = 0.0, 0.0
    return {"positions": pos, "closed": closed}


def performance(closed: list[dict]) -> dict:
    wins = [c["pnl"] for c in closed if c["pnl"] > 0]
    losses = [c["pnl"] for c in closed if c["pnl"] <= 0]
    n = len(closed)
    gross_loss = -sum(losses)
    return {
        "closed_trades": n, "net_pnl": sum(c["pnl"] for c in closed),
        "win_rate": len(wins) / n if n else None,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
        "expectancy": (sum(c["pnl"] for c in closed) / n) if n else None,
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "note": "Mẫu nhỏ thì các chỉ số này không có ý nghĩa thống kê" if n < 30 else "",
    }


def max_drawdown(equities: list[float]) -> dict:
    peak, mdd = 0.0, 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    cur = (peak - equities[-1]) / peak if equities and peak > 0 else 0.0
    return {"max_drawdown_pct": mdd * 100, "current_drawdown_pct": cur * 100,
            "peak": peak if equities else None}
