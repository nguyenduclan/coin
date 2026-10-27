import asyncio
import logging

from sqlalchemy import select

from ..core.config import settings
from ..core.db import SessionLocal
from ..models import Symbol
from ..news.rss import fetch_news
from .engine import analyze, expire_proposals, needs_refresh

log = logging.getLogger("coin.scheduler")


async def analysis_loop() -> None:
    while True:
        try:
            async with SessionLocal() as s:
                rows = (await s.scalars(select(Symbol).where(Symbol.active.is_(True)))).all()
            await expire_proposals()
            for r in rows:
                if needs_refresh(r.symbol):
                    await analyze(r.symbol, r.base_asset)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("analysis loop error")
        await asyncio.sleep(settings.analysis_interval_s)


async def news_loop() -> None:
    while True:
        try:
            res = await fetch_news()
            if res["new"] or res["errors"]:
                log.info("news: %s", res)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("news loop error")
        await asyncio.sleep(settings.news_interval_s)


async def account_loop(acct) -> None:
    """Đồng bộ số dư/lịch sử khớp (chỉ đọc) và ghi điểm vốn để tính drawdown."""
    from ..portfolio import service
    while True:
        try:
            if acct.enabled() and settings.trading_mode != "PAPER_TRADING":  # paper dùng sổ giả lập, không đọc ví
                from ..trading import executor
                await executor.reconcile_orders(acct)
                await service.sync_account(acct)
                await service.record_snapshot()
                if settings.trading_mode in ("BINANCE_TESTNET", "LIVE"):
                    from ..trading import reconcile
                    await reconcile.run(acct)
                    await executor.enforce_daily_loss()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("account loop error")
        await asyncio.sleep(60)


async def paper_loop() -> None:
    """Chế độ PAPER_TRADING: kiểm tra stop/TP giả lập mỗi 3 giây, ghi điểm vốn mỗi 60 giây."""
    from ..paper import service
    ticks = 0
    while True:
        try:
            await service.check_exits()
            if ticks % 20 == 0:
                await service.record_snapshot()
            ticks += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("paper loop error")
        await asyncio.sleep(3)
