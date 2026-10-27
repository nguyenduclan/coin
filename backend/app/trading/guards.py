"""Giới hạn vốn khi dùng tiền thật (LIVE). Mọi hàm trả về lý do từ chối; executor quyết định raise.

Mặc định an toàn: giới hạn <= 0 nghĩa là chặn hẳn, không phải "không giới hạn".
Lỗ ngày = vốn đầu ngày (điểm snapshot đầu tiên sau 00:00 UTC) - vốn hiện tại. Nạp/rút tiền làm lệch
con số này, và nếu backend khởi động giữa ngày thì mốc là snapshot đầu tiên sau khi khởi động.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..core.config import settings
from ..core.db import SessionLocal
from ..models import EquitySnapshot, Position
from ..portfolio import service as portfolio


def is_live() -> bool:
    return settings.trading_mode == "LIVE"


def limits() -> dict:
    return {"max_order_usdt": settings.live_max_order_usdt,
            "max_daily_loss_usdt": settings.live_max_daily_loss_usdt,
            "max_open_positions": settings.live_max_open_positions}


async def day_start_equity() -> float | None:
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with SessionLocal() as s:
        row = await s.scalar(select(EquitySnapshot).where(EquitySnapshot.at >= midnight,
                                                          EquitySnapshot.source == "binance")
                             .order_by(EquitySnapshot.at).limit(1))
    return row.equity if row else None


async def daily_loss() -> dict:
    """loss > 0 là đang lỗ. None nếu chưa đủ dữ liệu để tính."""
    er = await portfolio.equity_and_risk()
    start = await day_start_equity()
    if er["source"] != "binance" or start is None:
        return {"loss": None, "start_equity": start, "equity": er["equity"], "known": False}
    return {"loss": start - er["equity"], "start_equity": start, "equity": er["equity"], "known": True}


async def daily_loss_reason() -> str:
    """Rỗng nếu chưa chạm giới hạn lỗ ngày. LIVE chưa tính được lỗ ngày thì cũng chặn."""
    if not is_live():
        return ""
    lim = settings.live_max_daily_loss_usdt
    if lim <= 0:
        return "LIVE_MAX_DAILY_LOSS_USDT <= 0: giao dịch thật bị chặn"
    d = await daily_loss()
    if not d["known"]:
        return "Chưa có mốc vốn đầu ngày từ Binance, chưa kiểm soát được lỗ ngày"
    if d["loss"] >= lim:
        return f"Đã lỗ {d['loss']:.2f} USDT trong ngày, chạm giới hạn {lim:g} USDT"
    return ""


async def buy_limit_reasons(symbol: str, notional: float) -> list[str]:
    """Giới hạn riêng cho lệnh MUA mới (lệnh bán thoát luôn được phép)."""
    if not is_live():
        return []
    out = []
    mo = settings.live_max_order_usdt
    if mo <= 0 or notional > mo:
        out.append(f"Giá trị lệnh {notional:.2f} USDT vượt giới hạn LIVE_MAX_ORDER_USDT={mo:g}")
    mp = settings.live_max_open_positions
    async with SessionLocal() as s:
        n = await s.scalar(select(func.count()).select_from(Position).where(Position.qty > 0))
        has = await s.scalar(select(func.count()).select_from(Position)
                             .where(Position.qty > 0, Position.symbol == symbol))
    if mp <= 0 or (not has and n >= mp):
        out.append(f"Đã có {n} vị thế mở, giới hạn LIVE_MAX_OPEN_POSITIONS={mp}")
    return out
