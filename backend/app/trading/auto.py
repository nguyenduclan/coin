"""Tự động mua/thoát cho BINANCE_TESTNET và LIVE (PAPER vốn đã tự động trong analysis.engine).

Không có đường tắt: đề xuất do engine tạo được đưa qua executor.confirm như khi người dùng bấm, nên mọi
kiểm tra (công tắc, readiness/preflight, lỗ ngày, giới hạn vốn, dữ liệu tươi, rủi ro, OCO xác minh) vẫn áp dụng.
Tự động chỉ chạy khi cả công tắc giao dịch VÀ công tắc tự động đều bật; tắt giao dịch là tự động tắt theo.
Chỉ MUA mới (BUY_SETUP) và THOÁT TOÀN BỘ (SELL_EXIT). "Giảm nửa" (REDUCE) vẫn cần người xác nhận.
"""
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..binance.account import BinanceAccount
from ..core.config import settings
from ..core.db import SessionLocal
from ..models import AuditLog, Proposal, Symbol
from ..risk.sizing import round_down
from . import executor

log = logging.getLogger("coin.auto")

_acct: BinanceAccount | None = None
_next_ok: dict[tuple[str, str], float] = {}  # (symbol, side) -> thời điểm sớm nhất được thử lại


def set_account(acct: BinanceAccount | None) -> None:
    global _acct
    _acct = acct


async def _buys_today() -> int:
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with SessionLocal() as s:
        return await s.scalar(select(func.count()).select_from(AuditLog)
                              .where(AuditLog.action == "auto_buy", AuditLog.at >= midnight)) or 0


async def _cap_buy(pid: str) -> dict | None:
    """Cắt khối lượng đề xuất xuống AUTO_MAX_ORDER_USDT (nếu vượt). Trả về data đã cập nhật, None nếu không mua được."""
    async with SessionLocal() as s:
        p = await s.get(Proposal, pid)
        sym = await s.get(Symbol, p.symbol)
        data = json.loads(p.data)
        entry, qty = data.get("entry") or p.ref_price, data["qty"]
        if entry * qty > settings.auto_max_order_usdt:
            new_qty = round_down(settings.auto_max_order_usdt / entry, sym.step_size if sym else 0.0)
            if new_qty <= 0 or (sym and new_qty * entry < sym.min_notional):
                return None
            ratio = new_qty / qty
            data.update(qty=new_qty, notional=new_qty * entry,
                        risk_usdt=data.get("risk_usdt", 0.0) * ratio, auto_capped=True)
            p.data = json.dumps(data)
            await s.commit()
        return data


async def maybe_execute(symbol: str, report: dict) -> dict | None:
    """Gọi sau khi engine lưu đề xuất. Không bao giờ ném lỗi ra ngoài (không được làm hỏng vòng phân tích)."""
    try:
        return await _maybe_execute(symbol, report)
    except Exception:  # noqa: BLE001
        log.exception("auto execute error for %s", symbol)
        return None


async def _maybe_execute(symbol: str, report: dict) -> dict | None:
    pid, prop = report.get("proposal_id"), report.get("proposal")
    if (settings.trading_mode not in ("BINANCE_TESTNET", "LIVE") or not pid or not prop or _acct is None
            or not await executor.auto_enabled() or not await executor.trading_enabled()):
        return None
    side = prop["side"]
    if side == "BUY":
        if prop.get("strategy") == "manual_plan" or report.get("state") != "BUY_SETUP":
            return None
    elif not (prop.get("strategy") == "position_exit" and report.get("state") == "SELL_EXIT"):
        return None  # REDUCE và mọi thứ khác: chỉ đề xuất
    key = (symbol, side)
    if time.time() < _next_ok.get(key, 0):
        return None
    if side == "BUY" and await _buys_today() >= settings.auto_max_buys_per_day:
        return None
    if side == "BUY":
        data = await _cap_buy(pid)
        if data is None:
            _next_ok[key] = time.time() + settings.auto_retry_s
            await executor.audit("auto_failed", f"{symbol} BUY: khối lượng sau khi cắt theo AUTO_MAX_ORDER_USDT quá nhỏ")
            return None
    else:
        data = prop
    token = executor.confirm_token(pid, side, data)
    _next_ok[key] = time.time() + settings.auto_retry_s  # đặt trước: lỗi cũng không được thử dồn dập
    try:
        out = await executor.confirm(_acct, pid, token)
    except executor.Reject as e:
        await executor.audit("auto_failed", f"{symbol} {side}: {e.msg}")
        return None
    status = out.get("status")
    if side == "BUY":
        if status in ("FILLED", "PARTIAL_FILLED"):
            _next_ok[key] = time.time() + settings.auto_cooldown_s
            await executor.audit("auto_buy", f"{symbol} {status} qty={out.get('executed_qty')} "
                                             f"avg={out.get('avg_price')} bảo vệ={out.get('protection', {}).get('status')}")
    elif status in ("FILLED", "PARTIAL_FILLED"):
        await executor.audit("auto_exit", f"{symbol} {status} qty={out.get('executed_qty')} "
                                          f"avg={out.get('avg_price')} lý do: {prop.get('exit_reason')}")
    else:
        await executor.audit("auto_failed", f"{symbol} {side}: {status}")
    return out
