"""Chiến lược duy nhất giai đoạn này: pullback theo xu hướng (chỉ mua Spot).

Điều kiện: 1h UP, 4h không DOWN, 15m không DOWN, giá gần EMA20 của 15m (hồi về, không đuổi giá),
RSI 15m trong vùng trung tính. Stop dựa trên ATR và hỗ trợ gần nhất; TP là kháng cự 1h nếu đủ
xa, ngược lại 2R. Đây là giả thuyết cần backtest (Giai đoạn 6), KHÔNG phải lời hứa lợi nhuận.
"""
from ..core.config import settings
from ..risk.sizing import size_position, valid_number


def build_setup(report: dict, filters: dict, ref_price: float, account: dict | None = None) -> dict:
    """Trả về {"ok": True, "proposal": {...}} hoặc {"ok": False, "reasons": [...]}."""
    acc = account or {"equity": settings.account_equity_usdt, "free_quote": settings.account_equity_usdt, "open_risk": 0.0}
    tf = report["timeframes"]
    t = {k: v.get("trend") for k, v in tf.items()}
    r: list[str] = []
    if t.get("1h") != "UP":
        r.append(f"Xu hướng 1h là {t.get('1h')}, cần UP")
    if t.get("4h") == "DOWN":
        r.append("Khung 4h đang giảm")
    if t.get("15m") in (None, "DOWN", "INSUFFICIENT"):
        r.append(f"Khung 15m là {t.get('15m')}, cần không giảm")
    m15 = tf.get("15m", {})
    atr, ema20, rsi = m15.get("atr14"), m15.get("ema20"), m15.get("rsi14")
    if not all(valid_number(x) for x in (atr, ema20, rsi)) or atr <= 0:
        r.append("Thiếu chỉ báo 15m")
    if r:
        return {"ok": False, "reasons": r}

    if abs(ref_price - ema20) > 0.5 * atr or ref_price < ema20 - 0.5 * atr:
        r.append("Giá chưa hồi về vùng EMA20 15m (cách quá 0,5 ATR)")
    if not 40 <= rsi <= 65:
        r.append(f"RSI 15m = {rsi:.0f}, ngoài vùng 40–65")
    if r:
        return {"ok": False, "reasons": r}

    entry = ref_price
    stop = entry - 1.5 * atr
    sup = [s for s in m15.get("support", []) if entry - 1.5 * atr < s < entry - 0.5 * atr]
    if sup:  # hỗ trợ gần hơn stop ATR -> đặt stop ngay dưới hỗ trợ
        stop = max(sup) - 0.25 * atr
    risk_dist = entry - stop
    res = [x for x in tf.get("1h", {}).get("resistance", []) if x >= entry + settings.min_rr * risk_dist]
    take_profit = min(res) if res else entry + 2 * risk_dist

    tick = filters.get("tick_size") or 0
    if tick:
        from ..risk.sizing import round_down
        entry, stop, take_profit = (round_down(x, tick) for x in (entry, stop, take_profit))

    sized = size_position(
        equity=acc["equity"], risk_pct=settings.risk_pct, entry=entry, stop=stop,
        take_profit=take_profit, fee_rate=settings.fee_rate, slippage_pct=settings.slippage_pct,
        step_size=filters.get("step_size") or 0, min_notional=filters.get("min_notional") or 0,
        free_quote=acc["free_quote"], max_alloc_pct=settings.max_alloc_pct_per_coin,
        open_risk_usdt=acc["open_risk"], max_total_risk_pct=settings.max_total_risk_pct)
    if not sized["ok"]:
        return {"ok": False, "reasons": sized["reasons"]}
    if sized["rr_after_costs"] < settings.min_rr:
        return {"ok": False, "reasons": [
            f"R:R sau phí {sized['rr_after_costs']:.2f} < tối thiểu {settings.min_rr}"]}

    return {"ok": True, "proposal": {
        "strategy": "trend_pullback", "side": "BUY", "entry": entry, "stop": stop,
        "take_profit": take_profit, **{k: v for k, v in sized.items() if k != "ok"},
        "confirm_conditions": ["Nến 15m kế tiếp đóng trên EMA20", "Dữ liệu còn tươi (OK)"],
        "invalidation": [f"Giá <= {stop} (chạm stop)", "Xu hướng 1h không còn UP",
                         f"Giá lệch quá {settings.proposal_max_move_pct}% so với giá tham chiếu",
                         f"Quá {settings.proposal_ttl_s}s kể từ khi tạo"],
        "limits": ["Lỗ thực tế có thể lớn hơn dự kiến do gap/trượt giá/stop không khớp",
                   "Chiến lược chưa được backtest"],
    }}


def check_validity(p: dict, *, now: float, price: float | None, data_state: str, side: str = "BUY",
                   trend_1h: str | None) -> list[str]:
    """Lý do đề xuất KHÔNG còn hợp lệ ([] = còn hợp lệ). Dùng lại ở Giai đoạn 7 trước khi gửi lệnh."""
    out = []
    if now > p["expires_at"]:
        out.append("Đề xuất đã hết hạn")
    if data_state != "OK" or price is None:
        out.append(f"Dữ liệu thị trường không tin cậy ({data_state})")
    else:
        if abs(price / p["ref_price"] - 1) * 100 > settings.proposal_max_move_pct:
            out.append("Giá đã lệch quá ngưỡng so với giá tham chiếu")
        if side == "BUY" and price <= p["stop"]:
            out.append("Giá đã chạm hoặc thủng stop-loss")
    if side == "BUY" and trend_1h is not None and trend_1h != "UP":
        out.append("Xu hướng 1h không còn UP")
    return out


def evaluate_position(pos: dict, report: dict, price: float, step_size: float, min_notional: float) -> dict:
    """Quy tắc cho vị thế đang giữ: HOLD / REDUCE / SELL_EXIT. Bán không mặc định được phép
    chỉ vì đã xác nhận mua: đây chỉ là đề xuất, cần xác nhận riêng (Giai đoạn 7)."""
    from ..risk.sizing import round_down
    t = {k: v.get("trend") for k, v in report["timeframes"].items()}
    reason, fraction = None, 1.0
    if pos.get("stop") and price <= pos["stop"]:
        state, reason = "SELL_EXIT", f"Giá {price} đã chạm/thủng stop {pos['stop']}"
    elif pos.get("take_profit") and price >= pos["take_profit"]:
        state, reason = "SELL_EXIT", f"Giá {price} đã đạt take-profit {pos['take_profit']}"
    elif t.get("1h") == "DOWN" and t.get("4h") == "DOWN":
        state, reason = "SELL_EXIT", "Xu hướng 1h và 4h cùng giảm"
    elif t.get("1h") == "DOWN":
        state, reason, fraction = "REDUCE", "Xu hướng 1h chuyển giảm: cân nhắc giảm một nửa vị thế", 0.5
    else:
        return {"state": "HOLD", "reason": "Chưa có điều kiện thoát", "proposal": None}
    qty = round_down(pos["qty"] * fraction, step_size)
    if fraction < 1 and (pos["qty"] - qty) * price < min_notional:
        qty, state = round_down(pos["qty"], step_size), "SELL_EXIT"  # phần còn lại quá nhỏ -> thoát hết
    if qty <= 0 or qty * price < min_notional:
        return {"state": "HOLD", "reason": "Khối lượng bán dưới mức tối thiểu của sàn", "proposal": None}
    fee = settings.fee_rate * qty * price
    return {"state": state, "reason": reason, "proposal": {
        "strategy": "position_exit", "side": "SELL", "qty": qty, "entry": price,
        "notional": qty * price, "exit_reason": reason,
        "est_pnl_usdt": (price - pos["avg_cost"]) * qty - fee, "est_fees": fee,
        "invalidation": [f"Quá {settings.proposal_ttl_s}s kể từ khi tạo",
                         f"Giá lệch quá {settings.proposal_max_move_pct}% so với giá tham chiếu"],
        "confirm_conditions": ["Người dùng xác nhận bán riêng cho đề xuất này"],
        "limits": ["Giá khớp thực tế có thể khác giá tham chiếu"]}}


def trailing_suggestion(pos: dict, price: float, atr: float | None) -> dict | None:
    """Gợi ý dời stop lên khi đang có lãi (chỉ gợi ý, người dùng/đề xuất riêng mới thực hiện)."""
    stop = pos.get("stop")
    if not stop or not valid_number(atr) or atr <= 0 or not valid_number(price):
        return None
    cost = pos["avg_cost"]
    r = cost - stop
    if r <= 0:
        return None
    new, why = stop, None
    if price >= cost + r:
        new, why = max(new, cost * (1 + 2 * settings.fee_rate + 2 * settings.slippage_pct / 100)), \
            "Lãi từ 1R: dời stop về hòa vốn sau phí"
    if price >= cost + 2 * r:
        new, why = max(new, price - 1.5 * atr), "Lãi từ 2R: stop bám theo giá - 1,5 ATR"
    if new > stop * 1.0005:
        return {"suggested_stop": new, "current_stop": stop, "reason": why}
    return None
