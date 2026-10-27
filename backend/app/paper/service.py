"""Paper trading: sổ giả lập dùng giá thật (bid/ask từ WebSocket) + phí + trượt giá như hệ thống thật.

Giao diện hàm giống app.portfolio.service để engine/routes dùng chung qua trading.context.provider().
Không có tiền thật, nên đề xuất được tự khớp để đo hiệu quả chiến lược theo thời gian thực;
lệnh thật (Giai đoạn 7) vẫn bắt buộc xác nhận thủ công.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from ..core.config import settings
from ..core.db import SessionLocal
from ..data.state import market
from ..models import EquitySnapshot, PaperAccount, PaperPosition, PaperTrade, Proposal
from ..portfolio.ledger import max_drawdown, performance
from ..portfolio.service import position_risk

log = logging.getLogger("coin.paper")
_lock = asyncio.Lock()


async def _account(s) -> PaperAccount:
    acc = await s.get(PaperAccount, 1)
    if not acc:
        acc = PaperAccount(id=1, cash=settings.paper_start_equity, start_equity=settings.paper_start_equity)
        s.add(acc)
        await s.flush()
    return acc


def _last(symbol: str) -> float | None:
    st = market.symbols.get(symbol)
    return st.last if st else None


async def get_position(symbol: str) -> dict | None:
    async with SessionLocal() as s:
        r = await s.get(PaperPosition, symbol)
    if not r or r.qty <= 0:
        return None
    return {"symbol": r.symbol, "qty": r.qty, "avg_cost": r.avg_cost, "stop": r.stop,
            "take_profit": r.take_profit}


async def equity_and_risk() -> dict:
    async with SessionLocal() as s:
        acc = await _account(s)
        rows = (await s.scalars(select(PaperPosition).where(PaperPosition.qty > 0))).all()
        await s.commit()
    holdings = sum(r.qty * (_last(r.symbol) or r.avg_cost) for r in rows)
    risk = sum(position_risk(r.qty, _last(r.symbol), r.stop) or 0.0 for r in rows)
    return {"equity": acc.cash + holdings, "free_quote": acc.cash, "open_risk": risk, "source": "paper"}


def _fills(symbol: str) -> tuple[float | None, float | None]:
    st = market.symbols.get(symbol)
    if not st:
        return None, None
    return (st.bid or st.last), (st.ask or st.last)


async def _mark(proposal_id: str | None, status: str) -> None:
    if not proposal_id:
        return
    async with SessionLocal() as s:
        p = await s.get(Proposal, proposal_id)
        if p and p.status == "PROPOSED":  # chống khớp lặp: chỉ khớp đề xuất đang chờ
            p.status = status
            await s.commit()


async def execute_proposal(symbol: str, proposal: dict, proposal_id: str | None) -> dict:
    if market.status(symbol)["state"] != "OK":
        await _mark(proposal_id, "PAPER_REJECTED")
        return {"ok": False, "reason": "Dữ liệu thị trường không tin cậy"}
    async with _lock:
        if proposal_id:
            async with SessionLocal() as s:
                p = await s.get(Proposal, proposal_id)
                if not p or p.status != "PROPOSED":
                    return {"ok": False, "reason": "Đề xuất không còn ở trạng thái chờ"}
        bid, ask = _fills(symbol)
        slip = settings.slippage_pct / 100
        if proposal["side"] == "BUY":
            res = await _buy(symbol, proposal, ask, slip, proposal_id)
        else:
            res = await _sell(symbol, proposal["qty"], bid, slip, proposal.get("exit_reason", "exit"),
                              proposal_id)
    await _mark(proposal_id, "PAPER_FILLED" if res["ok"] else "PAPER_REJECTED")
    return res


async def _buy(symbol, p, ask, slip, proposal_id) -> dict:
    async with SessionLocal() as s:
        acc = await _account(s)
        if await s.get(PaperPosition, symbol):
            return {"ok": False, "reason": "Đã có vị thế (không bình quân giá)"}
        fill = ask * (1 + slip)
        cost = p["qty"] * fill
        fee = cost * settings.fee_rate
        if cost + fee > acc.cash:
            return {"ok": False, "reason": "Không đủ tiền giả lập"}
        acc.cash -= cost + fee
        s.add(PaperPosition(symbol=symbol, qty=p["qty"], avg_cost=(cost + fee) / p["qty"], stop=p["stop"],
                            take_profit=p["take_profit"], planned_risk=p.get("risk_usdt", 0.0), fees=fee))
        s.add(PaperTrade(symbol=symbol, side="BUY", qty=p["qty"], price=fill, fee=fee,
                         reason="proposal", proposal_id=proposal_id))
        await s.commit()
    return {"ok": True, "price": fill, "qty": p["qty"]}


async def _sell(symbol, qty, bid, slip, reason, proposal_id, price_override=None) -> dict:
    async with SessionLocal() as s:
        acc = await _account(s)
        pos = await s.get(PaperPosition, symbol)
        if not pos or pos.qty <= 0:
            return {"ok": False, "reason": "Không có vị thế"}
        qty = min(qty, pos.qty)
        fill = price_override if price_override is not None else bid * (1 - slip)
        proceeds = qty * fill
        fee = proceeds * settings.fee_rate
        pnl = proceeds - fee - qty * pos.avg_cost
        acc.cash += proceeds - fee
        pos.qty -= qty
        pos.realized_pnl += pnl
        pos.fees += fee
        r = None
        if pos.qty <= 1e-12:
            r = pos.realized_pnl / pos.planned_risk if pos.planned_risk else None
        s.add(PaperTrade(symbol=symbol, side="SELL", qty=qty, price=fill, fee=fee, pnl=pnl,
                         r_multiple=r, reason=reason[:60], proposal_id=proposal_id))
        if pos.qty <= 1e-12:
            await s.delete(pos)
        await s.commit()
    return {"ok": True, "price": fill, "qty": qty, "pnl": pnl}


async def check_exits() -> list[dict]:
    """Stop (market, trượt giá) và TP (limit) mô phỏng bằng giá thật. Bỏ qua khi dữ liệu không tin cậy."""
    done = []
    async with SessionLocal() as s:
        rows = (await s.scalars(select(PaperPosition).where(PaperPosition.qty > 0))).all()
    for r in rows:
        if market.status(r.symbol)["state"] != "OK":
            continue
        bid, _ = _fills(r.symbol)
        async with _lock:
            if r.stop and bid <= r.stop:
                done.append(await _sell(r.symbol, r.qty, bid, settings.slippage_pct / 100, "stop", None))
            elif r.take_profit and bid >= r.take_profit:
                done.append(await _sell(r.symbol, r.qty, bid, 0, "take_profit", None,
                                        price_override=r.take_profit))
    return done


async def set_protection(symbol: str, stop: float | None, take_profit: float | None) -> dict:
    async with SessionLocal() as s:
        r = await s.get(PaperPosition, symbol)
        if not r or r.qty <= 0:
            raise ValueError("Không có vị thế đang mở cho mã này")
        if (stop is not None and stop <= 0) or (take_profit is not None and take_profit <= 0):
            raise ValueError("Giá phải dương")
        if stop is not None and take_profit is not None and not stop < take_profit:
            raise ValueError("Cần stop < take-profit")
        r.stop, r.take_profit = stop, take_profit
        await s.commit()
    return {"symbol": symbol, "stop": stop, "take_profit": take_profit,
            "protection_status": "SIMULATED"}


async def summary() -> dict:
    async with SessionLocal() as s:
        acc = await _account(s)
        rows = (await s.scalars(select(PaperPosition).where(PaperPosition.qty > 0))).all()
        sells = (await s.scalars(select(PaperTrade).where(PaperTrade.side == "SELL"))).all()
        fees = await s.scalar(select(func.coalesce(func.sum(PaperTrade.fee), 0.0))) or 0.0
        snaps = (await s.scalars(select(EquitySnapshot).where(EquitySnapshot.source == "paper")
                                 .order_by(EquitySnapshot.at))).all()
        await s.commit()
    positions = []
    for r in rows:
        price = _last(r.symbol)
        positions.append({
            "symbol": r.symbol, "qty": r.qty, "avg_cost": r.avg_cost, "price": price,
            "value": r.qty * price if price else None,
            "unrealized_pnl": (price - r.avg_cost) * r.qty if price else None,
            "unrealized_pct": (price / r.avg_cost - 1) * 100 if price else None,
            "realized_pnl": r.realized_pnl, "fees": r.fees, "stop": r.stop, "take_profit": r.take_profit,
            "protection_status": "SIMULATED" if (r.stop or r.take_profit) else "NONE",
            "remaining_risk_usdt": position_risk(r.qty, price, r.stop),
            "risk_note": "" if r.stop else "Chưa có stop: tính toàn bộ giá trị là rủi ro",
            "balance_qty": None, "balance_mismatch": False})
    er = await equity_and_risk()
    return {
        "source": "paper", "equity": er["equity"], "usdt_balance": acc.cash, "free_quote": acc.cash,
        "holdings_value": sum(p["value"] or 0 for p in positions),
        "capital_in_use": sum(p["qty"] * p["avg_cost"] for p in positions),
        "unrealized_pnl": sum(p["unrealized_pnl"] or 0 for p in positions),
        "realized_pnl": sum(t.pnl or 0 for t in sells), "fees": fees,
        "open_positions": len(positions), "open_risk_usdt": er["open_risk"],
        "drawdown": max_drawdown([x.equity for x in snaps]), "positions": positions,
        "unvalued_assets": [], "balances_at": None, "sync_error": None,
        "warnings": ["PAPER TRADING: số liệu giả lập, không phải tiền thật. Khớp lệnh dùng bid/ask thật + phí + trượt giá"],
        "start_equity": acc.start_equity, "started_at": acc.started_at,
    }


async def trade_history(limit: int = 200) -> list[dict]:
    async with SessionLocal() as s:
        rows = (await s.scalars(select(PaperTrade).order_by(PaperTrade.time.desc()).limit(limit))).all()
    return [{"id": f"paper:{r.id}", "symbol": r.symbol, "side": r.side, "qty": r.qty, "price": r.price,
             "fee_usdt": r.fee, "fee_asset": "USDT", "fee_converted": True, "time": r.time,
             "pnl": r.pnl, "r_multiple": r.r_multiple, "reason": r.reason} for r in rows]


async def performance_report() -> dict:
    async with SessionLocal() as s:
        sells = (await s.scalars(select(PaperTrade).where(PaperTrade.side == "SELL"))).all()
    closed = [{"pnl": t.pnl or 0.0} for t in sells]
    out = performance(closed)
    rs = [t.r_multiple for t in sells if t.r_multiple is not None]
    out["avg_r"] = sum(rs) / len(rs) if rs else None
    return out


async def record_snapshot() -> None:
    er = await equity_and_risk()
    async with SessionLocal() as s:
        s.add(EquitySnapshot(equity=er["equity"], source="paper"))
        await s.commit()


async def reset() -> None:
    """Xóa toàn bộ dữ liệu paper (chỉ dữ liệu giả lập). Route yêu cầu xác nhận tường minh."""
    async with _lock, SessionLocal() as s:
        await s.execute(delete(PaperTrade))
        await s.execute(delete(PaperPosition))
        await s.execute(delete(PaperAccount))
        await s.execute(delete(EquitySnapshot).where(EquitySnapshot.source == "paper"))
        await s.commit()
