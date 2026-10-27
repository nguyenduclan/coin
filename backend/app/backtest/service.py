"""Tải lịch sử nến, chạy backtest nền, lưu kết quả."""
import asyncio
import json
import logging
import time
import uuid

import pandas as pd
from sqlalchemy import select

from ..binance.rest import BinanceRest
from ..binance.stream import save_candles
from ..core.db import SessionLocal
from ..models import BacktestRun, Symbol
from . import engine

log = logging.getLogger("coin.backtest")

TFS = ["15m", "1h", "4h", "1d"]
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
WARMUP_DAYS = 30  # 4h cần 60 nến ~ 10 ngày; dư để chỉ báo hội tụ
jobs: dict[str, dict] = {}
_tasks: set = set()  # giữ tham chiếu để task nền không bị thu gom


async def download(rest: BinanceRest, symbol: str, tf: str, start_ms: int, end_ms: int,
                   pause: float = 0.2) -> int:
    """Tải nến đã đóng theo trang 1000 nến; idempotent (bỏ qua nến đã có)."""
    cursor, total = start_ms, 0
    while cursor < end_ms:
        raw = await rest.klines(symbol, tf, 1000, start_time=cursor)
        if not raw:
            break
        now_ms = int(time.time() * 1000)
        rows = [{"symbol": symbol, "tf": tf, "open_time": int(k[0]), "open": float(k[1]),
                 "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                 "volume": float(k[5]), "close_time": int(k[6])}
                for k in raw if int(k[6]) < now_ms and int(k[0]) < end_ms]
        await save_candles(rows)
        total += len(rows)
        cursor = int(raw[-1][0]) + INTERVAL_MS[tf]
        if len(raw) < 1000:
            break
        await asyncio.sleep(pause)
    return total


async def load_dfs(symbol: str, start_ms: int, end_ms: int) -> dict[str, pd.DataFrame]:
    from ..models import Candle
    out = {}
    async with SessionLocal() as s:
        for tf in TFS:
            rows = (await s.execute(
                select(Candle.open_time, Candle.open, Candle.high, Candle.low, Candle.close,
                       Candle.volume, Candle.close_time)
                .where(Candle.symbol == symbol, Candle.tf == tf, Candle.open_time >= start_ms,
                       Candle.open_time <= end_ms).order_by(Candle.open_time))).all()
            out[tf] = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close",
                                                  "volume", "close_time"])
    return out


def _job(kind: str) -> tuple[str, dict]:
    jid = str(uuid.uuid4())
    jobs[jid] = {"id": jid, "kind": kind, "status": "running", "message": "", "result_id": None,
                 "started_at": time.time()}
    return jid, jobs[jid]


async def download_job(rest: BinanceRest, symbols: list[str], days: int) -> str:
    jid, job = _job("download")

    async def run():
        try:
            end = int(time.time() * 1000)
            start = end - (days + WARMUP_DAYS) * 86_400_000
            for sym in symbols:
                for tf in TFS:
                    job["message"] = f"{sym} {tf}"
                    await download(rest, sym, tf, start, end)
            job.update(status="done", message=f"Đã tải {len(symbols)} mã, {days} ngày (+{WARMUP_DAYS} ngày khởi động)")
        except Exception as e:  # noqa: BLE001
            job.update(status="error", message=str(e)[:300])
            log.exception("download job failed")

    t = asyncio.create_task(run())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return jid


def _compute(per_sym_dfs: dict, filters: dict, equity: float, start_ms: int, end_ms: int) -> dict:
    prepared = {s: engine.prepare(d) for s, d in per_sym_dfs.items()}
    return engine.run_all(prepared, filters, equity, start_ms, end_ms)


async def backtest_job(symbols: list[str], days: int, equity: float) -> str:
    jid, job = _job("backtest")

    async def run():
        try:
            end = int(time.time() * 1000)
            start = end - days * 86_400_000
            dfs, filters = {}, {}
            async with SessionLocal() as s:
                rows = {r.symbol: r for r in (await s.scalars(select(Symbol).where(Symbol.symbol.in_(symbols))))}
            for sym in symbols:
                job["message"] = f"Đọc dữ liệu {sym}"
                dfs[sym] = await load_dfs(sym, start - WARMUP_DAYS * 86_400_000, end)
                r = rows.get(sym)
                filters[sym] = {"tick_size": r.tick_size, "step_size": r.step_size,
                                "min_notional": r.min_notional} if r else {}
            job["message"] = "Đang mô phỏng"
            result = await asyncio.to_thread(_compute, dfs, filters, equity, start, end)
            rid = str(uuid.uuid4())
            async with SessionLocal() as s:
                s.add(BacktestRun(id=rid, symbols=",".join(symbols), days=days, equity=equity,
                                  result=json.dumps(result, default=float)))
                await s.commit()
            job.update(status="done", result_id=rid, message="Hoàn tất")
        except Exception as e:  # noqa: BLE001
            job.update(status="error", message=str(e)[:300])
            log.exception("backtest job failed")

    t = asyncio.create_task(run())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return jid
