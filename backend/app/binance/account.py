"""Client Binance có ký (HMAC): đọc tài khoản và gửi/huỷ lệnh.

Chỉ dùng API key tắt quyền rút tiền. Việc gọi các hàm ghi (new_order, new_oco, cancel...) chỉ được
thực hiện bởi app.trading.executor sau khi người dùng xác nhận và các kiểm tra phía backend đạt.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx

from ..core.config import settings


class AccountError(Exception):
    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status, self.code = status, code

    @property
    def outcome_unknown(self) -> bool:
        """Không có phản hồi, 5xx hoặc -1007 (timeout): lệnh CÓ THỂ đã vào sàn."""
        return self.status is None or self.status >= 500 or self.code == -1007


def sign(query: str, secret: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class BinanceAccount:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(base_url=settings.trade_rest_url or settings.binance_rest_url, timeout=15.0)
        self._offset_ms = 0

    @staticmethod
    def enabled() -> bool:
        return bool(settings.binance_api_key and settings.binance_api_secret)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def sync_time(self) -> None:
        r = await self._client.get("/api/v3/time")
        r.raise_for_status()
        self._offset_ms = r.json()["serverTime"] - int(time.time() * 1000)

    async def _signed(self, method: str, path: str, **params):
        if not self.enabled():
            raise AccountError("Chưa cấu hình BINANCE_API_KEY/BINANCE_API_SECRET")
        params = {k: v for k, v in params.items() if v is not None}
        params["timestamp"] = int(time.time() * 1000) + self._offset_ms
        params["recvWindow"] = 5000
        q = urlencode(params)
        try:
            r = await self._client.request(method, f"{path}?{q}&signature={sign(q, settings.binance_api_secret)}",
                                           headers={"X-MBX-APIKEY": settings.binance_api_key})
        except httpx.HTTPError as e:
            raise AccountError(f"Lỗi mạng: {type(e).__name__}") from e  # không có phản hồi
        if r.status_code >= 400:
            code = msg = None
            try:
                j = r.json()
                code, msg = j.get("code"), j.get("msg")
            except (ValueError, json.JSONDecodeError):
                pass
            # Không đưa URL (có chữ ký) vào thông báo lỗi
            raise AccountError(f"Binance HTTP {r.status_code} code={code}: {(msg or r.text)[:200]}",
                               status=r.status_code, code=code)
        return r.json()

    # ----- đọc -----
    async def account(self) -> dict:
        return await self._signed("GET", "/api/v3/account")

    async def my_trades(self, symbol: str, limit: int = 1000) -> list[dict]:
        return await self._signed("GET", "/api/v3/myTrades", symbol=symbol, limit=limit)

    async def open_orders(self, symbol: str | None = None) -> list[dict]:
        return await self._signed("GET", "/api/v3/openOrders", symbol=symbol)

    async def get_order(self, symbol: str, client_order_id: str) -> dict:
        return await self._signed("GET", "/api/v3/order", symbol=symbol, origClientOrderId=client_order_id)

    async def get_order_list(self, client_list_id: str) -> dict:
        return await self._signed("GET", "/api/v3/orderList", origClientOrderId=client_list_id)

    # ----- ghi (chỉ executor được gọi) -----
    async def test_order(self, **p) -> dict:
        return await self._signed("POST", "/api/v3/order/test", **p)

    async def new_order(self, **p) -> dict:
        return await self._signed("POST", "/api/v3/order", **p)

    async def new_oco(self, symbol: str, qty: str, tp_price: str, stop_price: str, stop_limit: str,
                      list_client_id: str) -> dict:
        """OCO bán: TP (LIMIT_MAKER) + stop-loss-limit. Thử endpoint mới, nếu 404 thì endpoint cũ."""
        try:
            return await self._signed(
                "POST", "/api/v3/orderList/oco", symbol=symbol, side="SELL", quantity=qty,
                aboveType="LIMIT_MAKER", abovePrice=tp_price,
                belowType="STOP_LOSS_LIMIT", belowStopPrice=stop_price, belowPrice=stop_limit,
                belowTimeInForce="GTC", listClientOrderId=list_client_id, newOrderRespType="FULL")
        except AccountError as e:
            if e.status != 404:
                raise
        return await self._signed(
            "POST", "/api/v3/order/oco", symbol=symbol, side="SELL", quantity=qty, price=tp_price,
            stopPrice=stop_price, stopLimitPrice=stop_limit, stopLimitTimeInForce="GTC",
            listClientOrderId=list_client_id)

    async def cancel_order_list(self, symbol: str, client_list_id: str) -> dict:
        return await self._signed("DELETE", "/api/v3/orderList", symbol=symbol,
                                  listClientOrderId=client_list_id)

    # ----- Giai đoạn 8 -----
    @property
    def clock_skew_ms(self) -> int:
        """Độ lệch đồng hồ máy so với sàn, đo ở lần sync_time gần nhất."""
        return self._offset_ms

    async def api_restrictions(self) -> dict:
        """Quyền thực của API key (chỉ có trên mainnet)."""
        return await self._signed("GET", "/sapi/v1/account/apiRestrictions")

    async def cancel_open_orders(self, symbol: str) -> list:
        """Huỷ MỌI lệnh đang mở của một mã (gồm cả OCO bảo vệ). Chỉ dùng cho kill switch có chủ ý."""
        return await self._signed("DELETE", "/api/v3/openOrders", symbol=symbol)
