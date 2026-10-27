"""Ingest WebSocket Binance: bookTicker, ticker 24h, kline. Có reconnect + backoff."""
import asyncio
import json
import logging
import random
import time

import websockets
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..core.config import settings
from ..core.db import SessionLocal
from ..data.state import market
from ..models import Candle
from .rest import BinanceRest

log = logging.getLogger("coin.stream")


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff có jitter."""
    return min(cap, base * (2 ** attempt)) * (0.5 + random.random() / 2)


def build_stream_url(symbols: list[str], tfs: list[str]) -> str:
    names = []
    for s in symbols:
        s = s.lower()
        names += [f"{s}@bookTicker", f"{s}@ticker"] + [f"{s}@kline_{tf}" for tf in tfs]
    return f"{settings.binance_ws_url}/stream?streams={'/'.join(names)}"


def kline_row(symbol: str, k: list) -> dict:
    return {
        "symbol": symbol, "tf": "", "open_time": int(k[0]), "open": float(k[1]),
        "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
        "volume": float(k[5]), "close_time": int(k[6]),
    }


async def save_candles(rows: list[dict]) -> None:
    if not rows:
        return
    async with SessionLocal() as s:
        # Bỏ qua nến đã có (UNIQUE symbol, tf, open_time)
        if s.bind.dialect.name == "sqlite":
            stmt = sqlite_insert(Candle).values(rows).on_conflict_do_nothing()
        else:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = pg_insert(Candle).values(rows).on_conflict_do_nothing()
        await s.execute(stmt)
        await s.commit()


async def backfill(rest: BinanceRest, symbol: str, tfs: list[str]) -> None:
    now_ms = int(time.time() * 1000)
    for tf in tfs:
        raw = await rest.klines(symbol, tf, settings.backfill_limit)
        rows = []
        for k in raw:
            if int(k[6]) >= now_ms:  # nến đang chạy, chưa hoàn chỉnh
                continue
            r = kline_row(symbol, k)
            r["tf"] = tf
            rows.append(r)
        await save_candles(rows)
        if rows:
            market.symbols[symbol].last_closed_candle[tf] = rows[-1]["open_time"]


async def handle_message(msg: dict) -> None:
    data = msg.get("data", {})
    ev = data.get("e")
    if "b" in data and "a" in data and ev is None:  # bookTicker không có trường "e"
        market.update_book(data["s"], float(data["b"]), float(data["a"]))
    elif ev == "24hrTicker":
        market.update_ticker(data["s"], data)
    elif ev == "kline" and data["k"]["x"]:  # chỉ nến đã đóng
        k = data["k"]
        row = {"symbol": data["s"], "tf": k["i"], "open_time": int(k["t"]),
               "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
               "close": float(k["c"]), "volume": float(k["v"]), "close_time": int(k["T"])}
        await save_candles([row])
        if data["s"] in market.symbols:
            market.symbols[data["s"]].last_closed_candle[k["i"]] = row["open_time"]


class IngestManager:
    """Giữ một kết nối WS cho tập mã đang kích hoạt; restart khi tập mã đổi."""

    def __init__(self, rest: BinanceRest):
        self.rest = rest
        self._symbols: list[str] = []
        self._task: asyncio.Task | None = None

    async def set_symbols(self, symbols: list[str]) -> None:
        symbols = sorted(symbols)
        if symbols == self._symbols:
            return
        await self.stop()
        self._symbols = symbols
        market.set_symbols(symbols)
        if symbols:
            self._task = asyncio.create_task(self._run(symbols))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        market.ws_connected = False

    async def _run(self, symbols: list[str]) -> None:
        tfs = settings.tf_list
        attempt = 0
        while True:
            try:
                for s in symbols:
                    await backfill(self.rest, s, tfs)
                async with websockets.connect(
                    build_stream_url(symbols, tfs), ping_interval=20, ping_timeout=20
                ) as ws:
                    market.ws_connected = True
                    attempt = 0
                    log.info("WS connected: %s", symbols)
                    async for raw in ws:
                        await handle_message(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                market.ws_connected = False
                market.ws_reconnects += 1
                delay = backoff_delay(attempt)
                attempt += 1
                log.warning("WS error %r; reconnect in %.1fs", e, delay)
                await asyncio.sleep(delay)
