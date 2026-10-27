"""Đồng bộ tài khoản Binance (chỉ đọc), tính danh mục, rủi ro đang mở và drawdown."""
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from ..binance.account import BinanceAccount
from ..core.config import settings
from ..core.db import SessionLocal
from ..data.state import market
from ..models import EquitySnapshot, Position, Symbol, Trade
from .ledger import apply_trades, max_drawdown, normalize_binance_trade, performance

log = logging.getLogger("coin.portfolio")

# Trạng thái số dư gần nhất (trong bộ nhớ)
account_state: dict = {"balances": {}, "locked": {}, "at": None, "error": None}


def apply_account_info(info: dict) -> None:
    """Cập nhật số dư trong bộ nhớ từ phản hồi /api/v3/account."""
    account_state["balances"] = {b["asset"]: float(b["free"]) + float(b["locked"])
                                 for b in info["balances"] if float(b["free"]) + float(b["locked"]) > 0}
    account_state["locked"] = {b["asset"]: float(b["locked"]) for b in info["balances"]
                               if float(b["locked"]) > 0}
    account_state["at"] = time.time()


async def sync_account(acct: BinanceAccount) -> dict:
    """Kéo số dư + lịch sử khớp của các mã trong watchlist, cập nhật vị thế."""
    try:
        await acct.sync_time()
        info = await acct.account()
        apply_account_info(info)
        async with SessionLocal() as s:
            symbols = (await s.scalars(select(Symbol))).all()
        added = 0
        for sym in symbols:
            raw = await acct.my_trades(sym.symbol)
            async with SessionLocal() as s:
                for t in raw:
                    n = normalize_binance_trade(t, sym.base_asset, sym.quote_asset)
                    if await s.get(Trade, n["id"]):
                        continue
                    s.add(Trade(id=n["id"], symbol=n["symbol"], side=n["side"], qty=n["qty"],
                                price=n["price"], fee_usdt=n["fee_usdt"], fee_asset=n["fee_asset"],
                                fee_converted=n["fee_converted"],
                                time=datetime.fromtimestamp(n["time_ms"] / 1000, timezone.utc)))
                    added += 1
                await s.commit()
        await rebuild_positions()
        account_state.update(at=time.time(), error=None)
        return {"ok": True, "new_trades": added}
    except Exception as e:  # noqa: BLE001
        account_state["error"] = str(e)[:300]
        log.warning("account sync failed: %s", account_state["error"])
        return {"ok": False, "error": account_state["error"]}


async def _all_trades() -> list[dict]:
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Trade))).all()
    return [{"symbol": r.symbol, "side": r.side, "qty": r.qty, "price": r.price,
             "fee_usdt": r.fee_usdt, "time": r.time.timestamp(), "fee_converted": r.fee_converted}
            for r in rows]


async def rebuild_positions() -> None:
    led = apply_trades(await _all_trades())
    async with SessionLocal() as s:
        for sym, p in led["positions"].items():
            row = await s.get(Position, sym)
            if not row:
                row = Position(symbol=sym)
                s.add(row)
            row.qty, row.avg_cost = p["qty"], p["avg_cost"]
            row.realized_pnl, row.fees = p["realized_pnl"], p["fees"]
            row.updated_at = datetime.now(timezone.utc)
            if p["qty"] <= 0:  # đóng hết vị thế -> bỏ kế hoạch stop/TP cũ
                row.stop = row.take_profit = None
                row.protection_status, row.protect_list_id = "NONE", None
        await s.commit()


async def get_position(symbol: str) -> dict | None:
    async with SessionLocal() as s:
        r = await s.get(Position, symbol)
    if not r or r.qty <= 0:
        return None
    return {"symbol": r.symbol, "qty": r.qty, "avg_cost": r.avg_cost, "stop": r.stop,
            "take_profit": r.take_profit}


def _price(symbol: str) -> float | None:
    st = market.symbols.get(symbol)
    return st.last if st else None


def position_risk(qty: float, price: float | None, stop: float | None) -> float | None:
    """Rủi ro còn lại (USDT). Không có stop -> tính toàn bộ giá trị (xấu nhất)."""
    if price is None:
        return None
    return max(0.0, price - stop) * qty if stop else qty * price


async def equity_and_risk() -> dict:
    """Vốn, tiền khả dụng và rủi ro đang mở dùng cho tính khối lượng lệnh."""
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Position).where(Position.qty > 0))).all()
    open_risk = sum(position_risk(r.qty, _price(r.symbol), r.stop) or 0.0 for r in rows)
    if account_state["at"] is None:
        eq = settings.account_equity_usdt
        return {"equity": eq, "free_quote": eq, "open_risk": open_risk, "source": "manual"}
    holdings = sum(r.qty * (_price(r.symbol) or r.avg_cost) for r in rows)
    usdt = account_state["balances"].get("USDT", 0.0)
    free = usdt - account_state["locked"].get("USDT", 0.0)
    return {"equity": usdt + holdings, "free_quote": max(0.0, free), "open_risk": open_risk,
            "source": "binance"}


async def summary() -> dict:
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Position))).all()
        snaps = (await s.scalars(select(EquitySnapshot).order_by(EquitySnapshot.at))).all()
    positions, realized, fees = [], 0.0, 0.0
    for r in rows:
        realized += r.realized_pnl
        fees += r.fees
        if r.qty <= 0:
            continue
        price = _price(r.symbol)
        value = r.qty * price if price else None
        unreal = (price - r.avg_cost) * r.qty if price else None
        base = r.symbol.removesuffix("USDT")
        bal = account_state["balances"].get(base)
        positions.append({
            "symbol": r.symbol, "qty": r.qty, "avg_cost": r.avg_cost, "price": price, "value": value,
            "unrealized_pnl": unreal,
            "unrealized_pct": (price / r.avg_cost - 1) * 100 if price and r.avg_cost else None,
            "realized_pnl": r.realized_pnl, "fees": r.fees, "stop": r.stop, "take_profit": r.take_profit,
            "protection_status": r.protection_status,
            "remaining_risk_usdt": position_risk(r.qty, price, r.stop),
            "risk_note": "Chưa có stop: tính toàn bộ giá trị là rủi ro" if not r.stop else "",
            "balance_qty": bal,
            "balance_mismatch": bal is not None and abs(bal - r.qty) > max(1e-8, r.qty * 0.01),
        })
    er = await equity_and_risk()
    holdings = sum(p["value"] or 0.0 for p in positions)
    usdt = account_state["balances"].get("USDT", 0.0) if er["source"] == "binance" else None
    dd = max_drawdown([x.equity for x in snaps])
    unvalued = [a for a in account_state["balances"]
                if a not in ("USDT",) and a + "USDT" not in market.symbols] if er["source"] == "binance" else []
    return {
        "source": er["source"], "equity": er["equity"], "usdt_balance": usdt,
        "free_quote": er["free_quote"], "holdings_value": holdings,
        "capital_in_use": sum(p["qty"] * p["avg_cost"] for p in positions),
        "unrealized_pnl": sum(p["unrealized_pnl"] or 0.0 for p in positions),
        "realized_pnl": realized, "fees": fees, "open_positions": len(positions),
        "open_risk_usdt": er["open_risk"], "drawdown": dd, "positions": positions,
        "unvalued_assets": unvalued,
        "balances_at": account_state["at"], "sync_error": account_state["error"],
        "warnings": (["Chưa kết nối Binance: vốn là giá trị nhập tay, chưa có vị thế thật"]
                     if er["source"] == "manual" else []),
    }


async def trade_history(limit: int = 200) -> list[dict]:
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Trade).order_by(Trade.time.desc()).limit(limit))).all()
    return [{"id": r.id, "symbol": r.symbol, "side": r.side, "qty": r.qty, "price": r.price,
             "fee_usdt": r.fee_usdt, "fee_asset": r.fee_asset, "fee_converted": r.fee_converted,
             "time": r.time} for r in rows]


async def performance_report() -> dict:
    return performance(apply_trades(await _all_trades())["closed"])


async def record_snapshot() -> None:
    er = await equity_and_risk()
    if er["source"] != "binance":
        return
    async with SessionLocal() as s:
        s.add(EquitySnapshot(equity=er["equity"], source="binance"))
        await s.commit()


async def set_protection(symbol: str, stop: float | None, take_profit: float | None) -> dict:
    async with SessionLocal() as s:
        r = await s.get(Position, symbol)
        if not r or r.qty <= 0:
            raise ValueError("Không có vị thế đang mở cho mã này")
        if stop is not None and stop <= 0 or take_profit is not None and take_profit <= 0:
            raise ValueError("Giá phải dương")
        if stop is not None and take_profit is not None and not stop < take_profit:
            raise ValueError("Cần stop < take-profit")
        changed = (r.stop, r.take_profit) != (stop, take_profit)
        if r.protection_status in ("VERIFIED", "PLACE_UNVERIFIED", "STALE_ON_EXCHANGE") and r.protect_list_id:
            # Lệnh OCO trên sàn vẫn giữ giá cũ: không được coi là đã bảo vệ theo kế hoạch mới
            if changed:
                r.protection_status = "STALE_ON_EXCHANGE"
        else:
            r.protection_status = "PLANNED_NOT_PLACED" if (stop or take_profit) else "NONE"
        r.stop, r.take_profit = stop, take_profit
        status_ = r.protection_status
        await s.commit()
    return {"symbol": symbol, "stop": stop, "take_profit": take_profit, "protection_status": status_}
