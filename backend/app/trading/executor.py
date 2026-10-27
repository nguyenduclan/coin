"""Thực thi lệnh lên Binance SAU KHI người dùng xác nhận. Đây là module duy nhất được phép gửi lệnh.

Nguyên tắc:
- Mỗi lệnh gắn một đề xuất; chuyển PROPOSED -> CONFIRMING bằng compare-and-set nên không thể gửi trùng.
- Backend kiểm tra lại toàn bộ (quyền, chế độ, công tắc, hạn đề xuất, độ tươi dữ liệu, giá lệch,
  số dư thật từ sàn, rủi ro, bộ lọc sàn, /order/test) trước khi gửi.
- Lệnh được ghi vào bảng orders TRƯỚC khi gửi; lỗi mơ hồ (timeout/5xx) được đối soát bằng client order id.
- Chỉ tuyên bố "được bảo vệ" khi Binance đã chấp nhận VÀ truy vấn lại xác nhận lệnh OCO đang chạy.
- Lỗi sàn liên tiếp tự tắt công tắc giao dịch.
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update

from .. import alerts
from ..binance.account import AccountError, BinanceAccount
from ..core.config import LIVE_ACK, settings
from ..core.db import SessionLocal
from ..data.state import market
from ..models import Analysis, AuditLog, Order, Position, Proposal, Setting, Symbol
from ..portfolio import service as portfolio
from ..risk.sizing import round_down, size_position
from . import guards
from .strategy import check_validity

log = logging.getLogger("coin.executor")
_fail_streak = 0


class Reject(Exception):
    """Từ chối trước khi gửi bất cứ lệnh nào lên sàn (hoặc sau khi tuyên bố an toàn là chưa gửi)."""

    def __init__(self, msg: str, http: int = 409):
        super().__init__(msg)
        self.msg, self.http = msg, http


class UnknownOutcome(Reject):
    """Lệnh CÓ THỂ đã vào sàn nhưng không xác định được: cần con người kiểm tra trên Binance."""


# ---------- công tắc và điều kiện cho phép ----------
async def _get_setting(key: str, default: str) -> str:
    async with SessionLocal() as s:
        row = await s.get(Setting, key)
    return row.value if row else default


async def audit(action: str, detail: str = "") -> None:
    async with SessionLocal() as s:
        s.add(AuditLog(action=action, detail=detail[:500]))
        await s.commit()
    if action in alerts.ALERT_ACTIONS:
        await alerts.notify(f"{action}: {detail}"[:500])


async def trading_enabled() -> bool:
    return (await _get_setting("trading_enabled", "false")) == "true"


async def set_trading_enabled(enabled: bool, why: str = "") -> None:
    async with SessionLocal() as s:
        row = await s.get(Setting, "trading_enabled")
        if row:
            row.value = "true" if enabled else "false"
        else:
            s.add(Setting(key="trading_enabled", value="true" if enabled else "false"))
        if not enabled:  # tắt giao dịch thì chế độ tự động cũng tắt, và phải bật lại có chủ ý
            auto_row = await s.get(Setting, "auto_trading_enabled")
            if auto_row:
                auto_row.value = "false"
        await s.commit()
    await audit("trading_enabled" if enabled else "trading_disabled", why)


async def auto_enabled() -> bool:
    return (await _get_setting("auto_trading_enabled", "false")) == "true"


async def set_auto_enabled(enabled: bool, why: str = "") -> None:
    async with SessionLocal() as s:
        row = await s.get(Setting, "auto_trading_enabled")
        if row:
            row.value = "true" if enabled else "false"
        else:
            s.add(Setting(key="auto_trading_enabled", value="true" if enabled else "false"))
        await s.commit()
    await audit("auto_enabled" if enabled else "auto_disabled", why)


async def can_trade() -> tuple[bool, str]:
    mode = settings.trading_mode
    if mode not in ("BINANCE_TESTNET", "LIVE"):
        return False, f"Chế độ {mode} không gửi lệnh lên sàn"
    if not BinanceAccount.enabled():
        return False, "Chưa cấu hình BINANCE_API_KEY/BINANCE_API_SECRET"
    if not await trading_enabled():
        return False, "Giao dịch đang TẮT. Bật ở trang Cài đặt trước khi xác nhận lệnh"
    if mode == "LIVE":
        if settings.live_trading_ack != LIVE_ACK:
            return False, "Thiếu LIVE_TRADING_ACK"
        from .. import validation
        r = await validation.readiness()
        if not r["all_passed"]:
            return False, "LIVE bị chặn: chưa đạt toàn bộ ngưỡng paper trading/backtest"
        why = await guards.daily_loss_reason()
        if why:
            return False, "LIVE bị chặn: " + why
    return True, ""


async def status() -> dict:
    ok, why = await can_trade()
    out = {"mode": settings.trading_mode, "env": settings.binance_env, "enabled": await trading_enabled(),
           "can_trade": ok, "blocked_reason": why, "fail_streak": _fail_streak,
           "max_order_failures": settings.max_order_failures, "auto_enabled": await auto_enabled(),
           "auto_limits": {"max_order_usdt": settings.auto_max_order_usdt,
                           "max_buys_per_day": settings.auto_max_buys_per_day,
                           "cooldown_s": settings.auto_cooldown_s}}
    if guards.is_live():
        out["live_limits"] = guards.limits()
        out["daily_loss"] = await guards.daily_loss()
    return out


async def enforce_daily_loss() -> bool:
    """Gọi định kỳ: chạm giới hạn lỗ ngày thì TỰ TẮT công tắc. True nếu vừa tắt."""
    if not guards.is_live() or not await trading_enabled():
        return False
    why = await guards.daily_loss_reason()
    if not why:
        return False
    await set_trading_enabled(False, "Tự tắt: " + why)
    await audit("daily_loss_limit", why)
    return True


async def kill_switch(acct: BinanceAccount, cancel_orders: bool = False) -> dict:
    """Dừng khẩn cấp: TẮT công tắc ngay (luôn thành công, không phụ thuộc sàn).
    cancel_orders=True còn huỷ mọi lệnh mở, KỂ CẢ OCO bảo vệ nên các vị thế sẽ không còn stop trên sàn."""
    await set_trading_enabled(False, "KILL SWITCH")
    await audit("kill_switch", f"cancel_orders={cancel_orders}")
    out: dict = {"trading_enabled": False, "canceled": [], "errors": []}
    if not cancel_orders:
        return out
    async with SessionLocal() as s:
        symbols = set((await s.scalars(select(Symbol.symbol))).all())
    try:
        await acct.sync_time()
        symbols |= {o["symbol"] for o in await acct.open_orders()}
    except AccountError as e:
        out["errors"].append(f"Không liệt kê được lệnh mở: {e}")
    for sym in sorted(symbols):
        try:
            if await acct.open_orders(sym):
                await acct.cancel_open_orders(sym)
                out["canceled"].append(sym)
        except AccountError as e:
            out["errors"].append(f"{sym}: {e}")
    async with SessionLocal() as s:
        for row in (await s.scalars(select(Position).where(Position.symbol.in_(out["canceled"])))).all():
            row.protection_status, row.protect_list_id = "NONE", None
        await s.commit()
    if out["canceled"]:
        await audit("protection_missing", "Kill switch đã huỷ lệnh mở (gồm OCO): " + ",".join(out["canceled"]))
    return out


async def _note_failure(why: str) -> None:
    global _fail_streak
    _fail_streak += 1
    if _fail_streak >= settings.max_order_failures:
        await set_trading_enabled(False, f"Tự tắt sau {_fail_streak} lỗi liên tiếp: {why}")


def _note_success() -> None:
    global _fail_streak
    _fail_streak = 0


# ---------- tiện ích ----------
def confirm_token(pid: str, side: str, data: dict) -> str:
    """Người dùng phải gửi lại token này: chứng minh họ đã thấy đúng nội dung đề xuất."""
    raw = f"{pid}|{side}|{data.get('qty')}|{data.get('entry')}|{data.get('stop')}|{data.get('take_profit')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def fmt_step(x: float, step: float) -> str:
    d = max(0, -Decimal(str(step)).normalize().as_tuple().exponent) if step else 8
    return format(Decimal(str(x)), f".{d}f")


def client_id(pid: str, prefix: str) -> str:
    return f"{prefix}{pid.replace('-', '')[:30]}"  # <= 36 ký tự theo quy định của sàn


async def _cas(pid: str, from_: str, to: str) -> bool:
    async with SessionLocal() as s:
        res = await s.execute(update(Proposal).where(Proposal.id == pid, Proposal.status == from_)
                              .values(status=to))
        await s.commit()
    return res.rowcount == 1


async def _finish(pid: str, status_: str, result: dict) -> None:
    async with SessionLocal() as s:
        p = await s.get(Proposal, pid)
        data = json.loads(p.data)
        data["result"] = result
        p.status, p.data = status_, json.dumps(data, default=str)
        await s.commit()
    await audit("proposal_" + status_.lower(), f"{pid} {json.dumps(result, default=str)[:300]}")


async def _order_row(**kw) -> None:
    async with SessionLocal() as s:
        s.add(Order(env=settings.binance_env, **kw))
        await s.commit()


async def _order_update(oid: str, **kw) -> None:
    async with SessionLocal() as s:
        o = await s.get(Order, oid)
        for k, v in kw.items():
            setattr(o, k, v)
        o.updated_at = datetime.now(timezone.utc)
        await s.commit()


def _analysis_trend_1h(report: dict) -> str | None:
    return report.get("timeframes", {}).get("1h", {}).get("trend")


async def _latest_report(symbol: str) -> dict | None:
    async with SessionLocal() as s:
        a = await s.scalar(select(Analysis).where(Analysis.symbol == symbol)
                           .order_by(Analysis.created_at.desc()).limit(1))
    return json.loads(a.report) if a else None


async def precheck(p: Proposal, data: dict) -> list[str]:
    """Kiểm tra tươi trước khi đổi trạng thái. [] = đạt."""
    from ..analysis.engine import analysis_is_current
    st = market.symbols.get(p.symbol)
    reasons = check_validity(
        {"expires_at": p.expires_at, "ref_price": p.ref_price, "stop": data.get("stop", 0)},
        now=time.time(), price=st.last if st else None, data_state=market.status(p.symbol)["state"],
        side=p.side, trend_1h=None)
    if p.side == "BUY" and data.get("strategy") != "manual_plan":  # kế hoạch thủ công đã được người dùng xem kết luận
        rep = await _latest_report(p.symbol)
        if not analysis_is_current(p.symbol) or not rep:
            reasons.append("Phân tích chưa cập nhật theo nến mới nhất, chờ vài chục giây rồi thử lại")
        elif _analysis_trend_1h(rep) != "UP":
            reasons.append("Xu hướng 1h không còn UP")
        elif rep["state"] != "BUY_SETUP":
            reasons.append(f"Trạng thái hiện tại là {rep['state']}, không còn BUY SETUP")
    return reasons


def _fees_usdt(resp: dict, base: str, quote: str) -> tuple[float, float]:
    """(phí quy ra USDT, phí trả bằng coin cơ sở)."""
    fee, base_fee = 0.0, 0.0
    for f in resp.get("fills", []):
        c = float(f["commission"])
        if f["commissionAsset"] == quote:
            fee += c
        elif f["commissionAsset"] == base:
            fee += c * float(f["price"])
            base_fee += c
    return fee, base_fee


# ---------- luồng chính ----------
async def confirm(acct: BinanceAccount, pid: str, token: str) -> dict:
    ok, why = await can_trade()
    if not ok:
        raise Reject(why, 403)
    async with SessionLocal() as s:
        p = await s.get(Proposal, pid)
    if not p:
        raise Reject("Không có đề xuất này", 404)
    data = json.loads(p.data)
    if token != confirm_token(p.id, p.side, data):
        raise Reject("Nội dung đề xuất không khớp với nội dung bạn đã xem")
    if p.status != "PROPOSED":
        raise Reject(f"Đề xuất đã ở trạng thái {p.status}, không thể xác nhận lại")
    reasons = await precheck(p, data)
    if reasons:
        raise Reject("; ".join(reasons))
    if not await _cas(pid, "PROPOSED", "CONFIRMING"):
        raise Reject("Đề xuất đang hoặc đã được xử lý")  # chống gửi trùng khi bấm hai lần
    await audit("proposal_confirmed", f"{pid} {p.symbol} {p.side}")
    try:
        return await _execute(acct, p, data)
    except UnknownOutcome as e:
        await _finish(pid, "UNKNOWN_NEEDS_REVIEW", {"error": e.msg})
        raise
    except Reject as e:  # chưa có lệnh nào được gửi
        await _finish(pid, "FAILED_PRECHECK", {"error": e.msg})
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("execute failed")
        await _finish(pid, "FAILED", {"error": repr(e)[:300]})
        await _note_failure(repr(e)[:100])
        raise Reject(f"Lỗi không mong đợi: {e!r}"[:300], 500) from e


async def reject(pid: str) -> dict:
    if not await _cas(pid, "PROPOSED", "REJECTED"):
        raise Reject("Đề xuất không còn ở trạng thái chờ")
    await audit("proposal_rejected", pid)
    return {"id": pid, "status": "REJECTED"}


async def _execute(acct: BinanceAccount, p: Proposal, data: dict) -> dict:
    async with SessionLocal() as s:
        sym = await s.get(Symbol, p.symbol)
    if not sym:
        raise Reject("Mã không còn trong danh sách")
    try:
        await acct.sync_time()
        info = await acct.account()
    except AccountError as e:
        raise Reject(f"Không xác minh được tài khoản: {e}") from e
    if not info.get("canTrade", False):
        raise Reject("API key không có quyền giao dịch Spot")
    portfolio.apply_account_info(info)
    st = market.symbols.get(p.symbol)
    if not st or not st.bid or not st.ask:
        raise Reject("Chưa có giá bid/ask")
    free = {b["asset"]: float(b["free"]) for b in info["balances"]}
    if p.side == "BUY":
        return await _buy(acct, p, data, sym, st, free)
    return await _sell(acct, p, data, sym, st, free)


async def _send_order(acct, p, kind, side, qty, limit, sym) -> tuple[dict | None, dict]:
    """Trả về (phản hồi sàn | None nếu bị từ chối, thông tin). Raise Reject nếu chưa gửi."""
    cid = client_id(p.id, "cbe" if kind == "ENTRY" else "cbx")
    params = dict(symbol=p.symbol, side=side, type="LIMIT", timeInForce="IOC",
                  quantity=fmt_step(qty, sym.step_size), price=fmt_step(limit, sym.tick_size),
                  newClientOrderId=cid, newOrderRespType="FULL")
    try:
        await acct.test_order(**params)
    except AccountError as e:
        raise Reject(f"Sàn từ chối khi kiểm tra lệnh: {e}") from e
    await _order_row(id=cid, proposal_id=p.id, kind=kind, symbol=p.symbol, side=side, qty=qty,
                     limit_price=limit, expected_price=p.ref_price, status="SENDING")
    try:
        resp = await acct.new_order(**params)
    except AccountError as e:
        if not e.outcome_unknown:
            await _order_update(cid, status="REJECTED_BY_EXCHANGE", error=str(e)[:400])
            await _note_failure(str(e)[:100])
            return None, {"order_id": cid, "error": str(e)}
        resp = await _reconcile(acct, p.symbol, cid)  # lệnh có thể đã vào sàn
        if resp is None:
            await _order_update(cid, status="FAILED_NOT_SENT", error=str(e)[:400])
            await _note_failure(str(e)[:100])
            return None, {"order_id": cid, "error": f"Không gửi được lệnh: {e}"}
    base, quote = sym.base_asset, sym.quote_asset
    ex = float(resp.get("executedQty", 0))
    fee, base_fee = _fees_usdt(resp, base, quote)
    avg = float(resp["cummulativeQuoteQty"]) / ex if ex > 0 else None
    await _order_update(cid, status=resp.get("status", "UNKNOWN"), executed_qty=ex, avg_price=avg,
                        fee_usdt=fee, exchange_id=str(resp.get("orderId")))
    _note_success()
    return resp, {"order_id": cid, "executed_qty": ex, "avg_price": avg, "fee_usdt": fee,
                  "base_fee": base_fee, "exchange_status": resp.get("status")}


async def _reconcile(acct, symbol: str, cid: str) -> dict | None:
    """Lỗi mơ hồ: hỏi sàn lệnh này có tồn tại không. None = chắc chắn chưa vào sàn."""
    try:
        return await acct.get_order(symbol, cid)
    except AccountError as e:
        if e.code == -2013:  # Order does not exist
            return None
        await _order_update(cid, status="UNKNOWN_NEEDS_REVIEW", error=str(e)[:400])
        await set_trading_enabled(False, f"Không xác định được trạng thái lệnh {cid}")
        raise UnknownOutcome(f"Không xác định được lệnh {cid} có vào sàn hay không. Giao dịch đã bị TẮT, "
                             "hãy kiểm tra trực tiếp trên Binance", 500) from e


async def _buy(acct, p, data, sym, st, free) -> dict:
    er = await portfolio.equity_and_risk()
    ask = st.ask
    sized = size_position(
        equity=er["equity"], risk_pct=settings.risk_pct, entry=ask, stop=data["stop"],
        take_profit=data["take_profit"], fee_rate=settings.fee_rate, slippage_pct=settings.slippage_pct,
        step_size=sym.step_size, min_notional=sym.min_notional, free_quote=free.get(sym.quote_asset, 0.0),
        max_alloc_pct=settings.max_alloc_pct_per_coin, open_risk_usdt=er["open_risk"],
        max_total_risk_pct=settings.max_total_risk_pct)
    if not sized["ok"]:
        raise Reject("Kiểm tra rủi ro lúc xác nhận không đạt: " + "; ".join(sized["reasons"]))
    if sized["rr_after_costs"] < settings.min_rr and data.get("strategy") != "manual_plan":
        raise Reject(f"R:R sau phí {sized['rr_after_costs']:.2f} dưới mức tối thiểu {settings.min_rr}")
    qty = round_down(min(data["qty"], sized["qty"]), sym.step_size)
    limit = round_down(ask * (1 + settings.order_slip_buffer_pct / 100), sym.tick_size)
    cap = round_down(p.ref_price * (1 + settings.proposal_max_move_pct / 100), sym.tick_size)
    limit = min(limit, cap)
    if ask > limit:
        raise Reject("Giá ask đã vượt ngưỡng cho phép so với giá tham chiếu")
    if qty <= 0 or qty * limit < sym.min_notional:
        raise Reject("Giá trị lệnh dưới mức tối thiểu của sàn")
    if free.get(sym.quote_asset, 0.0) < qty * limit * (1 + settings.fee_rate):
        raise Reject("Số dư khả dụng không đủ")
    limit_reasons = await guards.buy_limit_reasons(p.symbol, qty * limit)
    if limit_reasons:
        raise Reject("Vượt giới hạn vốn LIVE: " + "; ".join(limit_reasons))
    resp, info = await _send_order(acct, p, "ENTRY", "BUY", qty, limit, sym)
    if resp is None:
        await _finish(p.id, "REJECTED_BY_EXCHANGE", info)
        return {"status": "REJECTED_BY_EXCHANGE", **info}
    ex = info["executed_qty"]
    if ex <= 0:
        await _finish(p.id, "NOT_FILLED", info)
        return {"status": "NOT_FILLED", **info}
    status_ = "FILLED" if ex >= qty * 0.999 else "PARTIAL_FILLED"
    await portfolio.sync_account(acct)
    net = round_down(ex - info["base_fee"], sym.step_size)  # phí trả bằng coin làm giảm số coin nhận
    prot = await place_protection(acct, p.symbol, net, data["stop"], data["take_profit"], sym, p.id)
    result = {**info, "requested_qty": qty, "limit_price": limit, "expected_price": p.ref_price,
              "protection": prot}
    await _finish(p.id, status_, result)
    return {"status": status_, **result}


async def _sell(acct, p, data, sym, st, free) -> dict:
    async with SessionLocal() as s:
        pos = await s.get(Position, p.symbol)
    if not pos or pos.qty <= 0:
        raise Reject("Không có vị thế để bán")
    if pos.protect_list_id and pos.protection_status in ("VERIFIED", "PLACE_UNVERIFIED", "STALE_ON_EXCHANGE"):
        try:  # OCO đang khóa số coin: phải hủy trước khi bán
            await acct.cancel_order_list(p.symbol, pos.protect_list_id)
        except AccountError as e:
            if e.code != -2011:
                raise Reject(f"Không hủy được lệnh bảo vệ hiện có, chưa bán gì: {e}") from e
        await _order_update_oco(pos.protect_list_id, "CANCELED_FOR_EXIT")
        async with SessionLocal() as s:
            row = await s.get(Position, p.symbol)
            row.protection_status, row.protect_list_id = "NONE", None
            await s.commit()
        await audit("protection_canceled_for_exit", p.symbol)
        info = await acct.account()
        portfolio.apply_account_info(info)
        free = {b["asset"]: float(b["free"]) for b in info["balances"]}
    qty = round_down(min(data["qty"], pos.qty, free.get(sym.base_asset, 0.0)), sym.step_size)
    bid = st.bid
    limit = round_down(bid * (1 - settings.exit_slip_buffer_pct / 100), sym.tick_size)
    if qty <= 0 or qty * limit < sym.min_notional:
        raise Reject("Khối lượng khả dụng để bán dưới mức tối thiểu của sàn")
    resp, info = await _send_order(acct, p, "EXIT", "SELL", qty, limit, sym)
    if resp is None:
        await _finish(p.id, "REJECTED_BY_EXCHANGE", {**info, "warning": "Vị thế có thể đang KHÔNG có lệnh bảo vệ"})
        return {"status": "REJECTED_BY_EXCHANGE", **info}
    ex = info["executed_qty"]
    status_ = "NOT_FILLED" if ex <= 0 else ("FILLED" if ex >= qty * 0.999 else "PARTIAL_FILLED")
    await portfolio.sync_account(acct)
    remaining = await portfolio.get_position(p.symbol)
    result = {**info, "requested_qty": qty, "limit_price": limit, "expected_price": p.ref_price,
              "remaining_qty": remaining["qty"] if remaining else 0.0}
    if remaining and remaining["qty"] * bid >= sym.min_notional:  # bỏ qua bụi nhỏ hơn mức tối thiểu của sàn
        result["warning"] = ("Vị thế còn lại chưa có lệnh bảo vệ trên sàn. "
                             "Hãy đặt lại ở trang Portfolio.")
    await _finish(p.id, status_, result)
    return {"status": status_, **result}


async def _order_update_oco(list_id: str, status_: str) -> None:
    async with SessionLocal() as s:
        o = await s.get(Order, list_id)
        if o:
            o.status, o.updated_at = status_, datetime.now(timezone.utc)
            await s.commit()


async def place_protection(acct, symbol: str, qty: float, stop: float | None, tp: float | None,
                           sym, pid: str | None, suffix: str | None = None) -> dict:
    """Đặt OCO (TP + stop-loss-limit) và XÁC MINH bằng truy vấn lại. Không bao giờ ném lỗi ra ngoài:
    thất bại được trả về để giao diện cảnh báo, và vị thế KHÔNG được tuyên bố là đã bảo vệ."""
    async def mark(status_: str, list_id: str | None = None):
        async with SessionLocal() as s:
            row = await s.get(Position, symbol)
            if row:
                row.stop, row.take_profit = stop, tp
                row.protection_status, row.protect_list_id = status_, list_id
                await s.commit()

    if not stop or not tp:
        await mark("NONE")
        return {"status": "NONE", "error": "Thiếu stop hoặc take-profit"}
    qty = round_down(qty, sym.step_size)
    st = market.symbols.get(symbol)
    price = st.last if st else None
    if qty <= 0 or qty * stop < sym.min_notional:
        await mark("PLACE_FAILED")
        return {"status": "PLACE_FAILED", "error": "Khối lượng quá nhỏ để đặt lệnh bảo vệ"}
    if price is None or price <= stop * 1.0005:
        await mark("PLACE_FAILED")
        await audit("protection_failed", f"{symbol} giá sát/thủng stop")
        return {"status": "PLACE_FAILED", "error": "Giá hiện tại sát hoặc thấp hơn stop, không đặt được lệnh stop. "
                                                    "Cân nhắc thoát vị thế"}
    lid = client_id(pid or "", "cbo") if pid and not suffix else client_id(suffix or "", "cbp")
    stop_limit = round_down(stop * (1 - 0.001), sym.tick_size)
    await _order_row(id=lid, proposal_id=pid, kind="OCO", symbol=symbol, side="SELL", qty=qty,
                     limit_price=tp, expected_price=stop, status="SENDING")
    try:
        try:
            await acct.new_oco(symbol, fmt_step(qty, sym.step_size), fmt_step(tp, sym.tick_size),
                               fmt_step(stop, sym.tick_size), fmt_step(stop_limit, sym.tick_size), lid)
        except AccountError as e:
            if not e.outcome_unknown:
                raise
            await acct.get_order_list(lid)  # mơ hồ: nếu truy vấn được thì lệnh đã tồn tại
        lst = await acct.get_order_list(lid)
    except AccountError as e:
        await _order_update(lid, status="PLACE_FAILED", error=str(e)[:400])
        await mark("PLACE_FAILED")
        await audit("protection_failed", f"{symbol} {e}")
        return {"status": "PLACE_FAILED", "error": str(e)}
    if lst.get("listStatusType") == "EXEC_STARTED" or lst.get("listOrderStatus") == "EXECUTING":
        await _order_update(lid, status="VERIFIED", exchange_id=str(lst.get("orderListId")))
        await mark("VERIFIED", lid)
        await audit("protection_verified", f"{symbol} {lid}")
        return {"status": "VERIFIED", "list_id": lid, "qty": qty, "stop": stop, "take_profit": tp}
    await _order_update(lid, status="PLACE_UNVERIFIED", error=json.dumps(lst)[:300])
    await mark("PLACE_UNVERIFIED", lid)
    return {"status": "PLACE_UNVERIFIED", "error": "Sàn nhận lệnh nhưng trạng thái chưa xác nhận đang chạy",
            "list_id": lid}


async def reprotect(acct: BinanceAccount, symbol: str) -> dict:
    """Người dùng chủ động bấm đặt lại lệnh bảo vệ (đã có stop/TP kế hoạch)."""
    ok, why = await can_trade()
    if not ok:
        raise Reject(why, 403)
    async with SessionLocal() as s:
        pos = await s.get(Position, symbol)
        sym = await s.get(Symbol, symbol)
    if not pos or pos.qty <= 0 or not sym:
        raise Reject("Không có vị thế đang mở")
    if not pos.stop or not pos.take_profit:
        raise Reject("Cần nhập stop và take-profit trước")
    try:
        await acct.sync_time()
        if pos.protect_list_id:
            try:
                await acct.cancel_order_list(symbol, pos.protect_list_id)
            except AccountError as e:
                if e.code != -2011:
                    raise Reject(f"Không hủy được lệnh bảo vệ cũ: {e}") from e
            await _order_update_oco(pos.protect_list_id, "CANCELED_FOR_REPLACE")
        info = await acct.account()
    except AccountError as e:
        raise Reject(f"Không xác minh được tài khoản: {e}") from e
    portfolio.apply_account_info(info)
    free = {b["asset"]: float(b["free"]) for b in info["balances"]}
    qty = min(pos.qty, free.get(sym.base_asset, 0.0))
    await audit("reprotect_requested", symbol)
    return await place_protection(acct, symbol, qty, pos.stop, pos.take_profit, sym, None,
                                  suffix=str(int(time.time() * 1000)))


async def reconcile_orders(acct: BinanceAccount) -> int:
    """Đối soát lệnh kẹt ở SENDING (vd backend sập giữa chừng). Trả về số lệnh đã xử lý."""
    cutoff = datetime.fromtimestamp(time.time() - 30, timezone.utc)
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Order).where(Order.status == "SENDING", Order.kind != "OCO",
                                                    Order.created_at < cutoff))).all()
    n = 0
    for o in rows:
        try:
            resp = await acct.get_order(o.symbol, o.id)
            ex = float(resp.get("executedQty", 0))
            await _order_update(o.id, status=resp.get("status", "UNKNOWN"), executed_qty=ex,
                                exchange_id=str(resp.get("orderId")),
                                avg_price=(float(resp["cummulativeQuoteQty"]) / ex) if ex > 0 else None)
            await audit("order_reconciled", f"{o.id} {resp.get('status')}")
        except AccountError as e:
            if e.code == -2013:
                await _order_update(o.id, status="FAILED_NOT_SENT", error="Không có trên sàn sau khi đối soát")
            else:
                continue
        n += 1
    return n
