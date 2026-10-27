"""Client REST công khai của Binance Spot (không cần API key)."""
import httpx

from ..core.config import settings


class SymbolInfo(dict):
    pass


def parse_symbol_info(raw: dict) -> dict:
    """Rút gọn exchangeInfo thành các trường và bộ lọc cần cho tính toán lệnh."""
    filters = {f["filterType"]: f for f in raw.get("filters", [])}
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    return {
        "symbol": raw["symbol"],
        "status": raw.get("status"),
        "base_asset": raw["baseAsset"],
        "quote_asset": raw["quoteAsset"],
        "is_spot_trading_allowed": raw.get("isSpotTradingAllowed", False),
        "tick_size": float(filters.get("PRICE_FILTER", {}).get("tickSize", 0)),
        "step_size": float(filters.get("LOT_SIZE", {}).get("stepSize", 0)),
        "min_notional": float(notional.get("minNotional", 0)),
    }


class BinanceRest:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(
            base_url=settings.binance_rest_url, timeout=10.0
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params):
        r = await self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def server_time(self) -> int:
        return (await self._get("/api/v3/time"))["serverTime"]

    async def symbol_info(self, symbol: str) -> dict | None:
        """None nếu mã không tồn tại trên Binance Spot."""
        try:
            data = await self._get("/api/v3/exchangeInfo", symbol=symbol)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:  # Invalid symbol
                return None
            raise
        syms = data.get("symbols", [])
        return parse_symbol_info(syms[0]) if syms else None

    async def klines(self, symbol: str, interval: str, limit: int = 500,
                     start_time: int | None = None) -> list[list]:
        if start_time is not None:
            return await self._get("/api/v3/klines", symbol=symbol, interval=interval,
                                   limit=limit, startTime=start_time)
        return await self._get(
            "/api/v3/klines", symbol=symbol, interval=interval, limit=limit
        )

    async def ticker_24h(self, symbol: str) -> dict:
        return await self._get("/api/v3/ticker/24hr", symbol=symbol)
