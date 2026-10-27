"""Kế hoạch giao dịch theo yêu cầu: người dùng nhập số tiền (và giá mua dự kiến), hệ thống trả về
kết luận mua/chờ/không mua, giá cắt lỗ, giá chốt lời, rủi ro bằng tiền và quy tắc thoát.

Mọi con số do code tính từ nến đã đóng (cùng quy tắc với chiến lược). AI chỉ diễn giải bằng lời,
không được đổi số. Hệ thống KHÔNG dự đoán giá: tín hiệu thoát chỉ là điều kiện đo được.
"""
import json
import logging
import time

from ..ai import providers
from ..core.config import settings
from ..core.db import SessionLocal
from ..data.state import market
from ..indicators import ta
from ..models import Symbol
from ..risk.sizing import round_down, round_up, size_position, valid_number
from .context import provider
from .strategy import build_setup

log = logging.getLogger("coin.planner")

PLAN_PROMPT = (
    "Bạn là trợ lý phân tích crypto. CHỈ dùng dữ liệu JSON được cung cấp, không bịa tin, không đổi các con số "
    "trong plan, không hứa hẹn lợi nhuận, không đưa xác suất thắng. Giải thích ngắn gọn bằng tiếng Việt vì sao "
    "kết luận như vậy và những rủi ro chính. Trả về DUY NHẤT một JSON: "
    '{"summary": string, "why": [string], "risks": [string], "exit_advice": string}.'
)


def entry_deviation_notes(user_entry: float | None, ask: float) -> tuple[list[str], bool]:
    """Cảnh báo khi giá nhập lệch thị trường. Trả về (cảnh báo, có thể tạo đề xuất ngay)."""
    if user_entry is None:
        return [], True
    dev = (user_entry / ask - 1) * 100
    notes = []
    if abs(dev) > 20:
        notes.append(f"Giá nhập lệch {dev:+.0f}% so với giá thị trường hiện tại ({ask:.10g}). Kiểm tra lại số 0 sau dấu phẩy.")
    if dev > settings.proposal_max_move_pct:
        notes.append("Giá nhập CAO hơn thị trường: lệnh mua sẽ khớp ở giá thị trường, không phải giá bạn nhập.")
        return notes, False
    if dev < -settings.proposal_max_move_pct:
        notes.append("Giá nhập THẤP hơn thị trường: hệ thống chưa hỗ trợ lệnh chờ. Hãy chờ giá về gần mức này rồi tạo lại.")
        return notes, False
    return notes, True


def compute_levels(entry: float, atr: float, supports: list[float], resistances: list[float],
                   tick: float) -> dict:
    """Stop theo 1,5 ATR (hoặc ngay dưới hỗ trợ gần hơn), TP1 theo kháng cự 1h hoặc 2R, TP2 = 3R."""
    stop = entry - 1.5 * atr
    sup = [s for s in supports if entry - 1.5 * atr < s < entry - 0.5 * atr]
    if sup:
        stop = max(sup) - 0.25 * atr
    risk = entry - stop
    # Chi phí khứ hồi ước tính trên mỗi đơn vị (phí + trượt giá hai chiều): TP phải đủ xa để R:R SAU PHÍ >= min_rr
    c = entry * (2 * settings.fee_rate + 2 * settings.slippage_pct / 100)
    need = entry + settings.min_rr * (risk + c) + c
    res = [x for x in resistances if x >= need]
    tp1 = min(res) if res else max(entry + 2 * risk, need)
    tp2 = max(entry + 3 * risk, tp1)
    if tick:
        # Stop làm tròn xuống, TP làm tròn LÊN để R:R sau làm tròn không thấp hơn dự tính (tick của coin giá nhỏ rất thô)
        stop, tp1, tp2 = round_down(stop, tick), round_up(tp1, tick), round_up(tp2, tick)
    return {"stop": stop, "tp1": tp1, "tp2": tp2}


def costs(entry: float, exit_: float, qty: float) -> float:
    """Lãi/lỗ ròng sau phí hai chiều và trượt giá hai chiều."""
    slip = settings.slippage_pct / 100
    e, x = entry * (1 + slip), exit_ * (1 - slip)
    return qty * (x - e) - settings.fee_rate * qty * (e + x)


def exit_rules(entry: float, stop: float, tp1: float, atr: float, qty: float) -> list[dict]:
    r = entry - stop
    be = entry * (1 + 2 * settings.fee_rate + 2 * settings.slippage_pct / 100)
    return [
        {"if": f"Giá <= {stop:.10g}", "do": "BÁN CẮT LỖ (lỗ tối đa theo kế hoạch)"},
        {"if": f"Giá >= {entry + r:.10g} (lãi 1R)", "do": f"Dời stop lên {be:.10g} (hòa vốn sau phí)"},
        {"if": f"Giá >= {tp1:.10g}", "do": "Chốt lời một phần hoặc toàn bộ; phần còn lại dời stop theo (giá - 1,5 ATR)"},
        {"if": "Xu hướng 1h chuyển DOWN", "do": "Cân nhắc giảm nửa vị thế (hệ thống sẽ đề xuất REDUCE)"},
        {"if": "Xu hướng 1h và 4h cùng DOWN", "do": "Thoát toàn bộ (hệ thống sẽ đề xuất SELL / EXIT)"},
    ]


async def plan(symbol: str, amount: float, user_entry: float | None = None, use_ai: bool = True) -> dict:
    from ..analysis.engine import _ai_budget_ok, _ai_calls, _extract_json, load_df, recent_news
    sym = symbol.upper()
    async with SessionLocal() as s:
        row = await s.get(Symbol, sym)
    if not row:
        return {"verdict": "ERROR", "error": "Mã chưa có trong danh sách theo dõi"}
    if not valid_number(amount) or amount <= 0:
        return {"verdict": "ERROR", "error": "Số tiền phải lớn hơn 0"}
    if user_entry is not None and (not valid_number(user_entry) or user_entry <= 0):
        return {"verdict": "ERROR", "error": "Giá mua dự kiến không hợp lệ"}

    status = market.status(sym)
    st = market.symbols.get(sym)
    out = {"symbol": sym, "amount_usdt": amount, "data_status": status, "generated_at": time.time(),
           "mode": settings.trading_mode, "limits": [
               "Đây là kế hoạch dựa trên số liệu đã đo, không phải dự đoán hay cam kết lợi nhuận",
               "Lỗ thực tế có thể lớn hơn kế hoạch do gap, trượt giá hoặc stop không khớp"]}
    if status["state"] != "OK" or not st or not st.ask or not st.bid:
        out.update(verdict="DATA_UNAVAILABLE", reasons=[status["reason"] or "Chưa có giá bid/ask"])
        return out

    tfs = {tf: ta.summarize(await load_df(sym, tf)) for tf in settings.tf_list}
    out["trends"] = {tf: v.get("trend") for tf, v in tfs.items()}
    m15, h1 = tfs.get("15m", {}), tfs.get("1h", {})
    ask, bid = st.ask, st.bid
    out.update(price={"bid": bid, "ask": ask, "last": st.last})
    if not all(valid_number(m15.get(k)) for k in ("atr14", "ema20", "rsi14")):
        out.update(verdict="DATA_UNAVAILABLE", reasons=["Chưa đủ nến đã đóng để tính chỉ báo"])
        return out

    notes, can_propose = entry_deviation_notes(user_entry, ask)
    entry = user_entry or ask
    lv = compute_levels(entry, m15["atr14"], m15.get("support", []), h1.get("resistance", []), row.tick_size)
    stop, tp1, tp2 = lv["stop"], lv["tp1"], lv["tp2"]

    # Kết luận: dùng đúng cổng của chiến lược (giả lập tài khoản rất lớn để không bị chặn bởi khối lượng)
    report = {"timeframes": tfs}
    gate = build_setup(report, {"tick_size": row.tick_size, "step_size": row.step_size, "min_notional": 0.0}, ask,
                       {"equity": 1e12, "free_quote": 1e12, "open_risk": 0.0})
    t = out["trends"]
    if gate["ok"]:
        verdict, reasons = "BUY_NOW", ["Thỏa điều kiện chiến lược trend_pullback (xu hướng 1h tăng, giá hồi về EMA20 15m)"]
    elif t.get("1h") != "UP" or t.get("4h") == "DOWN" or t.get("15m") in (None, "DOWN", "INSUFFICIENT"):
        verdict, reasons = "NO_TRADE", gate["reasons"]
    else:
        verdict, reasons = "WAIT_PULLBACK", gate["reasons"]
        lo, hi = m15["ema20"] - 0.5 * m15["atr14"], m15["ema20"] + 0.5 * m15["atr14"]
        out["wait_zone"] = {"low": lo, "high": hi, "note": "Vùng hồi về EMA20 15m (±0,5 ATR) mà chiến lược chấp nhận vào lệnh"}
    out.update(verdict=verdict, reasons=reasons)

    # Khối lượng theo số tiền, rồi chặn bởi giới hạn rủi ro/vốn của hệ thống
    slip, fee = settings.slippage_pct / 100, settings.fee_rate
    qty = round_down(amount / (ask * (1 + slip) * (1 + fee)), row.step_size)
    plan_notes = list(notes)
    acct = await provider().equity_and_risk()
    cap = size_position(equity=acct["equity"], risk_pct=settings.risk_pct, entry=entry, stop=stop, take_profit=tp1,
                        fee_rate=fee, slippage_pct=settings.slippage_pct, step_size=row.step_size,
                        min_notional=0.0, free_quote=acct["free_quote"], max_alloc_pct=settings.max_alloc_pct_per_coin,
                        open_risk_usdt=acct["open_risk"], max_total_risk_pct=settings.max_total_risk_pct)
    if cap["ok"] and cap["qty"] < qty:
        plan_notes.append(f"Khối lượng bị giảm từ {qty:.10g} xuống {cap['qty']:.10g} theo giới hạn rủi ro "
                          f"{settings.risk_pct}% vốn / vốn tối đa mỗi coin ({acct['source']}: vốn {acct['equity']:.2f} USDT)")
        qty = cap["qty"]
    elif not cap["ok"]:
        plan_notes += [f"Giới hạn hệ thống: {r}" for r in cap["reasons"]]
        can_propose = False
    notional = qty * ask
    if notional < row.min_notional:
        plan_notes.append(f"Giá trị lệnh {notional:.2f} USDT dưới mức tối thiểu của sàn ({row.min_notional:.2f} USDT)")
        can_propose = False
    if qty <= 0:
        plan_notes.append("Số tiền quá nhỏ để mua được một đơn vị hợp lệ của sàn")
        can_propose = False

    risk_usdt = -costs(entry, stop, qty)
    out["plan"] = {
        "entry": entry, "qty": qty, "notional": notional, "stop": stop, "take_profit": tp1, "take_profit_2": tp2,
        "risk_usdt": risk_usdt, "risk_pct_of_amount": risk_usdt / amount * 100 if amount else None,
        "risk_pct_equity": risk_usdt / acct["equity"] * 100 if acct["equity"] else None,
        "reward_usdt_tp1": costs(entry, tp1, qty), "reward_usdt_tp2": costs(entry, tp2, qty),
        "rr_tp1": costs(entry, tp1, qty) / risk_usdt if risk_usdt > 0 else None,
        "rr_tp2": costs(entry, tp2, qty) / risk_usdt if risk_usdt > 0 else None,
        "stop_distance_pct": (entry - stop) / entry * 100, "tp1_distance_pct": (tp1 / entry - 1) * 100,
        "fees_roundtrip_est": fee * qty * (entry + tp1),
    }
    out["exit_rules"] = exit_rules(entry, stop, tp1, m15["atr14"], qty)
    out["notes"] = plan_notes
    out["can_propose"] = can_propose and out["plan"]["rr_tp1"] is not None
    out["news"] = await recent_news(row.base_asset, 5)
    out["ai"], out["ai_error"] = None, ""
    if use_ai and settings.ai_provider != "none":
        if not _ai_budget_ok():
            out["ai_error"] = "Vượt giới hạn số lần gọi AI trong ngày"
        else:
            try:
                _ai_calls["n"] += 1
                facts = {k: out[k] for k in ("symbol", "verdict", "reasons", "trends", "price", "plan", "notes",
                                             "news", "wait_zone") if k in out}
                facts["indicators_15m"] = {k: m15.get(k) for k in ("rsi14", "atr14", "ema20", "macd_hist")}
                out["ai"] = _extract_json(await providers.complete(PLAN_PROMPT, json.dumps(facts, ensure_ascii=False)))
            except Exception as e:  # noqa: BLE001  — AI lỗi không được làm mất kế hoạch
                out["ai_error"] = str(e)[:300]
                log.warning("planner AI failed: %r", e)
    return out
