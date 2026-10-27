import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from .analysis.scheduler import account_loop, analysis_loop, news_loop, paper_loop
from .binance.account import BinanceAccount
from .api.routes import router
from .binance.rest import BinanceRest
from .binance.stream import IngestManager
from .core.config import settings
from .core.db import SessionLocal, init_db
from .trading import auto
from .models import Symbol

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # URL ký của Binance chứa chữ ký, không ghi vào log


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.admin_password or len(settings.session_secret) < 32:
        raise RuntimeError("Cần đặt ADMIN_PASSWORD và SESSION_SECRET (>= 32 ký tự) trong .env")
    await init_db()
    app.state.rest = BinanceRest()
    app.state.ingest = IngestManager(app.state.rest)
    app.state.account = BinanceAccount()
    auto.set_account(app.state.account)
    async with SessionLocal() as s:
        active = list(await s.scalars(select(Symbol.symbol).where(Symbol.active.is_(True))))
    await app.state.ingest.set_symbols(active)
    tasks = [asyncio.create_task(analysis_loop()), asyncio.create_task(news_loop()),
             asyncio.create_task(account_loop(app.state.account))]
    if settings.trading_mode == "PAPER_TRADING":
        tasks.append(asyncio.create_task(paper_loop()))
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await app.state.ingest.stop()
    await app.state.rest.aclose()
    await app.state.account.aclose()


app = FastAPI(title="Coin Assistant", lifespan=lifespan)
app.include_router(router)
