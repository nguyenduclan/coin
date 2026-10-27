"""Tính khối lượng theo rủi ro. Hàm thuần, không I/O, để test kỹ.

Rủi ro mỗi đơn vị gồm khoảng cách tới stop, trượt giá hai chiều và phí hai chiều.
Số tiền "có nguy cơ mất" là kế hoạch; lỗ thực tế có thể lớn hơn do gap hoặc stop không khớp.
"""
import math
from decimal import Decimal, ROUND_DOWN, ROUND_UP

HARD_MAX_RISK_PCT = 1.0  # trần cứng; muốn vượt phải sửa code (cần phê duyệt)


def round_down(x: float, step: float) -> float:
    if step <= 0:
        return x
    d = Decimal(str(step))
    return float((Decimal(str(x)) / d).to_integral_value(rounding=ROUND_DOWN) * d)


def round_up(x: float, step: float) -> float:
    if step <= 0:
        return x
    d = Decimal(str(step))
    return float((Decimal(str(x)) / d).to_integral_value(rounding=ROUND_UP) * d)


def size_position(*, equity: float, risk_pct: float, entry: float, stop: float, take_profit: float,
                  fee_rate: float, slippage_pct: float, step_size: float, min_notional: float,
                  free_quote: float, max_alloc_pct: float, open_risk_usdt: float = 0.0,
                  max_total_risk_pct: float = 2.0) -> dict:
    """Trả về {"ok": bool, "reasons": [...], ...}. Từ chối thay vì tự nới giới hạn."""
    reasons: list[str] = []
    if not (stop < entry < take_profit):
        return {"ok": False, "reasons": ["Cần stop < entry < take-profit (Spot chỉ mua)"]}
    if risk_pct <= 0 or equity <= 0:
        return {"ok": False, "reasons": ["Vốn hoặc % rủi ro không hợp lệ"]}
    if risk_pct > HARD_MAX_RISK_PCT:
        return {"ok": False, "reasons": [f"Rủi ro {risk_pct}% vượt trần cứng {HARD_MAX_RISK_PCT}%"]}

    slip = slippage_pct / 100
    eff_entry = entry * (1 + slip)
    eff_stop = stop * (1 - slip)
    eff_tp = take_profit * (1 - slip)
    risk_per_unit = (eff_entry - eff_stop) + fee_rate * (eff_entry + eff_stop)
    reward_per_unit = (eff_tp - eff_entry) - fee_rate * (eff_tp + eff_entry)
    if reward_per_unit <= 0:
        return {"ok": False, "reasons": ["Lợi nhuận kỳ vọng sau phí và trượt giá không dương"]}

    budget = equity * risk_pct / 100
    total_cap = equity * max_total_risk_pct / 100
    if open_risk_usdt + budget > total_cap:
        return {"ok": False, "reasons": [
            f"Tổng rủi ro {open_risk_usdt + budget:.2f} USDT vượt giới hạn {total_cap:.2f} USDT"]}

    qty = budget / risk_per_unit
    # Giới hạn theo vốn mỗi coin và số dư khả dụng (đã tính phí mua)
    qty_alloc = equity * max_alloc_pct / 100 / eff_entry
    qty_cash = free_quote / (eff_entry * (1 + fee_rate))
    if qty_alloc < qty or qty_cash < qty:
        reasons.append("Khối lượng bị giới hạn bởi " + ("vốn tối đa mỗi coin" if qty_alloc <= qty_cash
                                                         else "số dư khả dụng"))
    qty = round_down(min(qty, qty_alloc, qty_cash), step_size)
    notional = qty * entry
    if qty <= 0 or notional < min_notional:
        return {"ok": False, "reasons": reasons + [
            f"Giá trị lệnh {notional:.2f} USDT dưới mức tối thiểu {min_notional:.2f} USDT"]}

    risk_usdt = qty * risk_per_unit
    return {
        "ok": True, "reasons": reasons, "qty": qty, "notional": notional,
        "risk_usdt": risk_usdt, "risk_pct_equity": risk_usdt / equity * 100,
        "rr_after_costs": reward_per_unit / risk_per_unit,
        "est_fees_roundtrip": fee_rate * qty * (entry + take_profit),
        "est_reward_usdt": qty * reward_per_unit,
        "total_risk_after_usdt": open_risk_usdt + risk_usdt,
    }


def valid_number(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
