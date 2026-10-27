"""Trạng thái thị trường trong bộ nhớ + đánh giá độ tươi của dữ liệu."""
import time
from dataclasses import dataclass, field

from ..core.config import settings


@dataclass
class SymbolState:
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    change_pct_24h: float | None = None
    volume_24h: float | None = None
    quote_volume_24h: float | None = None
    received_at: float | None = None  # epoch giây, lúc backend nhận
    source_ts: float | None = None  # epoch giây, thời điểm tại nguồn nếu có
    last_closed_candle: dict[str, int] = field(default_factory=dict)  # tf -> open_time


class MarketState:
    def __init__(self) -> None:
        self.symbols: dict[str, SymbolState] = {}
        self.ws_connected: bool = False
        self.ws_last_message_at: float | None = None
        self.ws_reconnects: int = 0

    def set_symbols(self, symbols: list[str]) -> None:
        for s in symbols:
            self.symbols.setdefault(s, SymbolState(s))
        for s in list(self.symbols):
            if s not in symbols:
                del self.symbols[s]

    def update_book(self, symbol: str, bid: float, ask: float, now: float | None = None) -> None:
        st = self.symbols.get(symbol)
        if not st:
            return
        now = now or time.time()
        st.bid, st.ask = bid, ask
        st.received_at = now
        self.ws_last_message_at = now

    def update_ticker(self, symbol: str, d: dict, now: float | None = None) -> None:
        st = self.symbols.get(symbol)
        if not st:
            return
        now = now or time.time()
        st.last = float(d["c"])
        st.change_pct_24h = float(d["P"])
        st.volume_24h = float(d["v"])
        st.quote_volume_24h = float(d["q"])
        st.source_ts = d.get("E", 0) / 1000 or None
        st.received_at = now
        self.ws_last_message_at = now

    def status(self, symbol: str, now: float | None = None) -> dict:
        """Phân biệt rõ: DATA_UNAVAILABLE (chưa có) / STALE (cũ) / OK."""
        now = now or time.time()
        st = self.symbols.get(symbol)
        if not st or st.received_at is None or st.last is None:
            return {"state": "DATA_UNAVAILABLE", "reason": "Chưa nhận dữ liệu từ Binance",
                    "age_s": None, "latency_ms": None}
        age = now - st.received_at
        latency = (st.received_at - st.source_ts) * 1000 if st.source_ts else None
        if not self.ws_connected:
            return {"state": "STALE", "reason": "WebSocket mất kết nối", "age_s": age,
                    "latency_ms": latency}
        if age > settings.stale_after_seconds:
            return {"state": "STALE", "reason": f"Dữ liệu cũ {age:.0f}s", "age_s": age,
                    "latency_ms": latency}
        return {"state": "OK", "reason": "", "age_s": age, "latency_ms": latency}

    def snapshot(self, now: float | None = None) -> list[dict]:
        out = []
        for sym, st in self.symbols.items():
            out.append({
                "symbol": sym, "bid": st.bid, "ask": st.ask, "last": st.last,
                "change_pct_24h": st.change_pct_24h, "quote_volume_24h": st.quote_volume_24h,
                "received_at": st.received_at, "source_ts": st.source_ts,
                "data": self.status(sym, now),
            })
        return out


market = MarketState()
