"""Đối soát định kỳ trạng thái trong DB với sàn (chỉ ĐỌC; không sửa lệnh trên sàn).

Phát hiện: vị thế lệch số dư thật, lệnh bảo vệ biến mất, lệnh lạ trên sàn, lệnh kẹt cần người xem,
và vị thế vừa đóng (stop/TP khớp). Mỗi vấn đề mới chỉ cảnh báo MỘT lần cho đến khi hết rồi tái diễn.
"""
import logging
import time

from sqlalchemy import select

from ..binance.account import AccountError, BinanceAccount
from ..core.db import SessionLocal
from ..models import Order, Position
from ..portfolio import service as portfolio
from . import executor

log = logging.getLogger("coin.reconcile")

state: dict = {"at": None, "issues": [], "error": None}
_alerted: set[str] = set()
_open_prev: set[str] = set()


def _issue(key: str, severity: str, text: str) -> dict:
    return {"key": key, "severity": severity, "text": text}


async def run(acct: BinanceAccount) -> dict:
    global _open_prev
    issues: list[dict] = []
    try:
        await acct.sync_time()
        exch_open = await acct.open_orders()
    except AccountError as e:
        state.update(at=time.time(), error=str(e)[:200])
        return state  # không đoán khi không đọc được sàn; giữ nguyên issues cũ
    async with SessionLocal() as s:
        positions = (await s.scalars(select(Position).where(Position.qty > 0))).all()
        orders = (await s.scalars(select(Order))).all()
    bal = portfolio.account_state["balances"]
    now_open = {p.symbol for p in positions}

    for sym in sorted(_open_prev - now_open):  # vừa đóng: thường do stop/TP khớp
        await executor.audit("position_closed", f"{sym} không còn vị thế (stop/TP khớp hoặc đã bán)")
    _open_prev = now_open

    for p in positions:
        base = p.symbol.removesuffix("USDT")
        have = bal.get(base)
        if have is not None and abs(have - p.qty) > max(1e-8, p.qty * 0.01):
            issues.append(_issue(f"mismatch:{p.symbol}", "block",
                                 f"{p.symbol}: DB {p.qty:g} nhưng số dư sàn {have:g}"))
        if p.protection_status in ("VERIFIED", "PLACE_UNVERIFIED") and p.protect_list_id:
            try:
                lst = await acct.get_order_list(p.protect_list_id)
                gone = lst.get("listStatusType") == "ALL_DONE"
            except AccountError as e:
                gone = e.code == -2018  # không có danh sách lệnh này
                if not gone:
                    continue  # lỗi tạm thời: không kết luận
            if gone:
                async with SessionLocal() as s:
                    row = await s.get(Position, p.symbol)
                    if row and row.qty > 0:
                        row.protection_status, row.protect_list_id = "NONE", None
                        await s.commit()
                issues.append(_issue(f"prot:{p.symbol}", "block",
                                     f"{p.symbol}: lệnh bảo vệ không còn chạy trên sàn nhưng vị thế vẫn mở"))
                await executor.audit("protection_missing", p.symbol)
        elif p.protection_status not in ("VERIFIED", "SIMULATED"):
            issues.append(_issue(f"noprot:{p.symbol}", "warn", f"{p.symbol}: vị thế chưa có lệnh bảo vệ trên sàn"))

    known_ids = {o.id for o in orders}
    known_lists = {o.exchange_id for o in orders if o.kind == "OCO" and o.exchange_id}
    for o in exch_open:
        if o.get("clientOrderId") in known_ids or str(o.get("orderListId")) in known_lists:
            continue
        issues.append(_issue(f"foreign:{o.get('symbol')}:{o.get('orderId')}", "warn",
                             f"Lệnh lạ trên sàn (không do app tạo): {o.get('symbol')} {o.get('side')} "
                             f"{o.get('origQty')} @ {o.get('price')}"))
    for o in orders:
        if o.status == "UNKNOWN_NEEDS_REVIEW":
            issues.append(_issue(f"unknown:{o.id}", "block",
                                 f"Lệnh {o.id} chưa xác định được kết quả, cần kiểm tra trên Binance"))

    keys = {i["key"] for i in issues}
    for i in issues:
        # 'prot' đã có audit riêng ở trên
        if i["key"] not in _alerted and not i["key"].startswith("prot:"):
            await executor.audit("reconcile_issue", i["text"])
    _alerted.intersection_update(keys)
    _alerted.update(keys)
    state.update(at=time.time(), issues=issues, error=None)
    return state
