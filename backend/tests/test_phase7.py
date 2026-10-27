import asyncio
import json
import os
import time
import uuid

os.environ.update(ADMIN_PASSWORD="pw", SESSION_SECRET="x" * 40,
                  DATABASE_URL="sqlite+aiosqlite:////tmp/test_p7.db")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.analysis import engine as analysis_engine  # noqa: E402
from app.binance.account import BinanceAccount  # noqa: E402
from app.core.config import LIVE_ACK, Settings, settings  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.data.state import market  # noqa: E402
from app.models import Analysis, Order, Position, Proposal, Symbol, Trade  # noqa: E402
from app.portfolio import service as portfolio  # noqa: E402
from app.trading import executor  # noqa: E402


class FakeBinance:
    """Sàn giả có trạng thái: ghi lại mọi lời gọi để kiểm tra thứ tự và số lần gửi lệnh."""

    def __init__(self, base="T1"):
        self.base = base
        self.calls: list[tuple[str, str]] = []
        self.balances = {"USDT": 1000.0, base: 0.0}
        self.locked = {base: 0.0}
        self.orders: dict[str, dict] = {}
        self.lists: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.order_mode = "fill"  # fill | partial | expire | reject | timeout_sent | timeout_unsent | timeout_unknown
        self.oco_mode = "ok"  # ok | reject
        self.test_fail = False
        self.account_fail = False
        self.cancel_fail = False
        self.can_trade = True
        self._tid = 0

    def n(self, method, path):
        return sum(1 for m, p in self.calls if (m, p) == (method, path))

    def _fill(self, q, qty):
        price = float(q["price"][0]) if isinstance(q["price"], list) else float(q["price"])
        side = q["side"]
        self._tid += 1
        fee = qty * 0.001
        if side == "BUY":
            self.balances["USDT"] -= qty * 99.99
            self.balances[self.base] += qty
        else:
            self.balances["USDT"] += qty * 100.9
            self.balances[self.base] -= qty
        self.trades.append({"symbol": q["symbol"], "id": self._tid, "qty": str(qty),
                            "price": "99.99" if side == "BUY" else "100.9", "commission": str(fee),
                            "commissionAsset": self.base if side == "BUY" else "USDT", "time": 1000 + self._tid,
                            "isBuyer": side == "BUY"})
        return {"price": "99.99", "qty": str(qty), "commission": str(fee),
                "commissionAsset": self.base if side == "BUY" else "USDT"}

    def handler(self, req: httpx.Request):
        q = dict(req.url.params)
        path, method = req.url.path, req.method
        self.calls.append((method, path))
        if path != "/api/v3/time" and "signature" not in q:
            return httpx.Response(400, json={"code": -1102, "msg": "unsigned"})
        if path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": int(time.time() * 1000)})
        if path == "/api/v3/account":
            if self.account_fail:
                return httpx.Response(503, text="down")
            b = [{"asset": a, "free": str(v - self.locked.get(a, 0.0)), "locked": str(self.locked.get(a, 0.0))}
                 for a, v in self.balances.items()]
            return httpx.Response(200, json={"canTrade": self.can_trade, "balances": b})
        if path == "/api/v3/myTrades":
            return httpx.Response(200, json=[t for t in self.trades if t["symbol"] == q["symbol"]])
        if path == "/api/v3/order/test":
            return httpx.Response(400, json={"code": -1013, "msg": "Filter failure: LOT_SIZE"}) if self.test_fail \
                else httpx.Response(200, json={})
        if path == "/api/v3/order" and method == "POST":
            return self.place(q)
        if path == "/api/v3/order" and method == "GET":
            o = self.orders.get(q["origClientOrderId"])
            if self.order_mode == "timeout_unknown":
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json=o) if o else httpx.Response(400, json={"code": -2013, "msg": "no order"})
        if path == "/api/v3/orderList/oco":
            if self.oco_mode == "reject":
                return httpx.Response(400, json={"code": -1013, "msg": "Filter failure: PERCENT_PRICE"})
            self.lists[q["listClientOrderId"]] = q
            self.locked[self.base] = float(q["quantity"])
            return httpx.Response(200, json={"orderListId": 7, "listClientOrderId": q["listClientOrderId"]})
        if path == "/api/v3/orderList" and method == "GET":
            if q["origClientOrderId"] in self.lists:
                return httpx.Response(200, json={"orderListId": 7, "listStatusType": "EXEC_STARTED",
                                                 "listOrderStatus": "EXECUTING"})
            return httpx.Response(400, json={"code": -2018, "msg": "no list"})
        if path == "/api/v3/orderList" and method == "DELETE":
            if self.cancel_fail:
                return httpx.Response(400, json={"code": -1000, "msg": "cancel failed"})
            if q["listClientOrderId"] not in self.lists:
                return httpx.Response(400, json={"code": -2011, "msg": "Unknown order sent."})
            del self.lists[q["listClientOrderId"]]
            self.locked[self.base] = 0.0
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"code": 0, "msg": "nf"})

    def place(self, q):
        if self.order_mode == "reject":
            return httpx.Response(400, json={"code": -2010, "msg": "Account has insufficient balance"})
        qty = float(q["quantity"])
        if self.order_mode == "timeout_unsent":
            raise httpx.ReadTimeout("timeout")
        if self.order_mode == "timeout_unknown":
            raise httpx.ReadTimeout("timeout")
        ex = {"fill": qty, "timeout_sent": qty, "partial": qty / 2, "expire": 0.0}[self.order_mode]
        fills = [self._fill(q, ex)] if ex > 0 else []
        resp = {"orderId": 99, "clientOrderId": q["newClientOrderId"], "executedQty": str(ex),
                "cummulativeQuoteQty": str(ex * 99.99), "status": "FILLED" if ex == qty else "EXPIRED",
                "fills": fills}
        self.orders[q["newClientOrderId"]] = resp
        if self.order_mode == "timeout_sent":
            raise httpx.ReadTimeout("timeout")  # lệnh đã vào sàn nhưng client không nhận được phản hồi
        return httpx.Response(200, json=resp)


@pytest.fixture
async def env():
    await init_db()
    async with SessionLocal() as s:  # DB dùng chung giữa các test: dọn dữ liệu của mã thử
        for model in (Trade, Position, Order, Proposal, Analysis):
            await s.execute(delete(model).where(model.symbol == "T1USDT"))
        await s.execute(delete(Position))  # vị thế của module test khác làm lệch vốn/rủi ro
        await s.commit()
    portfolio.account_state.update(balances={}, locked={}, at=None)
    settings.trading_mode, settings.binance_env = "BINANCE_TESTNET", "testnet"
    settings.binance_api_key, settings.binance_api_secret = "k", "s"
    await executor.set_trading_enabled(True, "test")
    executor._fail_streak = 0
    fake = FakeBinance()
    client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(fake.handler))
    acct = BinanceAccount(client)
    yield fake, acct
    await client.aclose()


async def setup_symbol(sym="T1USDT", base="T1"):
    async with SessionLocal() as s:
        if not await s.get(Symbol, sym):
            s.add(Symbol(symbol=sym, base_asset=base, quote_asset="USDT", tick_size=0.01, step_size=0.001,
                         min_notional=5))
        # Phân tích mới nhất: BUY_SETUP, 1h UP
        s.add(Analysis(symbol=sym, state="BUY_SETUP", data_status="OK", ref_price=100.0,
                       report=json.dumps({"state": "BUY_SETUP", "timeframes": {"1h": {"trend": "UP"}}})))
        await s.commit()
    set_market(sym)
    analysis_engine._last[sym] = {"candles": {}, "price": 100.0, "at": time.time()}


def set_market(sym, bid=99.9, ask=100.0, last=100.0):
    market.set_symbols([sym])
    market.ws_connected = True
    now = time.time()
    market.update_ticker(sym, {"c": str(last), "P": "0", "v": "1", "q": "1", "E": now * 1000}, now=now)
    market.update_book(sym, bid, ask, now=now)


async def make_proposal(sym="T1USDT", side="BUY", qty=2.17, ref=100.0, ttl=300, **over):
    pid = str(uuid.uuid4())
    data = {"side": side, "qty": qty, "entry": ref, "stop": 98.0, "take_profit": 106.0, "risk_usdt": 5.0,
            "notional": qty * ref}
    if side == "SELL":
        data = {"side": "SELL", "qty": qty, "entry": ref, "exit_reason": "test"}
    async with SessionLocal() as s:
        s.add(Proposal(id=pid, symbol=sym, side=side, expires_at=time.time() + ttl, ref_price=ref,
                       data=json.dumps(data), **over))
        await s.commit()
    return pid, executor.confirm_token(pid, side, data)


async def status_of(pid):
    async with SessionLocal() as s:
        p = await s.get(Proposal, pid)
    return p.status, json.loads(p.data).get("result", {})


async def sym_orders(kind=None):
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Order))).all()
    return [o for o in rows if kind is None or o.kind == kind]


# ---------- đường đi đúng ----------
async def test_buy_happy_path_places_and_verifies_protection(env):
    fake, acct = env
    await setup_symbol()
    pid, tok = await make_proposal()
    res = await executor.confirm(acct, pid, tok)
    assert res["status"] == "FILLED" and res["protection"]["status"] == "VERIFIED"
    assert fake.n("POST", "/api/v3/order") == 1
    assert fake.calls.index(("POST", "/api/v3/order/test")) < fake.calls.index(("POST", "/api/v3/order"))
    assert fake.balances["USDT"] < 1000
    st, result = await status_of(pid)
    assert st == "FILLED" and result["expected_price"] == 100.0 and result["avg_price"] == pytest.approx(99.99)
    async with SessionLocal() as s:
        pos = await s.get(Position, "T1USDT")
    assert pos.protection_status == "VERIFIED" and pos.protect_list_id and pos.stop == 98.0
    lst = list(fake.lists.values())[0]
    assert float(lst["quantity"]) == pytest.approx(2.167)  # đã trừ phí trả bằng coin, làm tròn xuống
    assert lst["belowStopPrice"] == "98.00" and lst["abovePrice"] == "106.00"
    kinds = {o.kind: o.status for o in await sym_orders()}
    assert kinds["ENTRY"] == "FILLED" and kinds["OCO"] == "VERIFIED"


async def test_double_confirm_sequential_and_concurrent(env):
    fake, acct = env
    await setup_symbol()
    pid, tok = await make_proposal()
    await executor.confirm(acct, pid, tok)
    with pytest.raises(executor.Reject):
        await executor.confirm(acct, pid, tok)
    assert fake.n("POST", "/api/v3/order") == 1
    pid2, tok2 = await make_proposal()
    results = await asyncio.gather(executor.confirm(acct, pid2, tok2), executor.confirm(acct, pid2, tok2),
                                   return_exceptions=True)
    assert sum(isinstance(r, executor.Reject) for r in results) == 1  # chỉ một lần được thực thi
    assert fake.n("POST", "/api/v3/order") == 2  # 1 từ proposal đầu + 1 từ proposal hai


# ---------- bị chặn: tuyệt đối không có lệnh nào lên sàn ----------
@pytest.mark.parametrize("case", ["mode", "disabled", "token", "expired", "drift", "stale_data", "stale_analysis",
                                  "trend", "state", "account_down", "cant_trade", "no_cash", "test_order_fail",
                                  "no_keys"])
async def test_blocked_cases_send_no_order(env, case):
    fake, acct = env
    await setup_symbol()
    pid, tok = await make_proposal(ttl=-1 if case == "expired" else 300)
    if case == "mode":
        settings.trading_mode = "ANALYSIS_ONLY"
    elif case == "disabled":
        await executor.set_trading_enabled(False, "test")
    elif case == "token":
        tok = "deadbeef"
    elif case == "drift":
        set_market("T1USDT", 100.9, 101.0, 101.0)  # lệch 1% > 0.3%
    elif case == "stale_data":
        market.symbols["T1USDT"].received_at -= 60
    elif case == "stale_analysis":
        analysis_engine._last["T1USDT"]["candles"] = {"1h": 1}
    elif case == "trend":
        async with SessionLocal() as s:
            a = await s.scalar(select(Analysis).where(Analysis.symbol == "T1USDT")
                               .order_by(Analysis.created_at.desc()).limit(1))
            a.report = json.dumps({"state": "BUY_SETUP", "timeframes": {"1h": {"trend": "DOWN"}}})
            await s.commit()
    elif case == "state":
        async with SessionLocal() as s:
            a = await s.scalar(select(Analysis).where(Analysis.symbol == "T1USDT")
                               .order_by(Analysis.created_at.desc()).limit(1))
            a.report = json.dumps({"state": "WAIT", "timeframes": {"1h": {"trend": "UP"}}})
            await s.commit()
    elif case == "account_down":
        fake.account_fail = True
    elif case == "cant_trade":
        fake.can_trade = False
    elif case == "no_cash":
        fake.balances["USDT"] = 10.0
    elif case == "test_order_fail":
        fake.test_fail = True
    elif case == "no_keys":
        settings.binance_api_key = ""
    with pytest.raises(executor.Reject):
        await executor.confirm(acct, pid, tok)
    assert fake.n("POST", "/api/v3/order") == 0
    assert fake.n("POST", "/api/v3/orderList/oco") == 0
    st, _ = await status_of(pid)
    assert st in ("PROPOSED", "FAILED_PRECHECK")  # chặn sớm giữ nguyên, chặn muộn ghi FAILED_PRECHECK
    if st == "FAILED_PRECHECK":  # đã đóng, không xác nhận lại được
        with pytest.raises(executor.Reject):
            await executor.confirm(acct, pid, tok)


# ---------- khớp một phần, không khớp, bị sàn từ chối ----------
async def test_partial_fill_protects_only_executed_qty(env):
    fake, acct = env
    await setup_symbol()
    fake.order_mode = "partial"
    pid, tok = await make_proposal()
    res = await executor.confirm(acct, pid, tok)
    assert res["status"] == "PARTIAL_FILLED"
    assert float(list(fake.lists.values())[0]["quantity"]) == pytest.approx(1.082, abs=0.002)  # ~ 2.17/2 trừ phí


async def test_ioc_not_filled_places_nothing_else(env):
    fake, acct = env
    await setup_symbol()
    fake.order_mode = "expire"
    pid, tok = await make_proposal()
    assert (await executor.confirm(acct, pid, tok))["status"] == "NOT_FILLED"
    assert fake.n("POST", "/api/v3/orderList/oco") == 0


async def test_exchange_rejection_and_auto_disable(env):
    fake, acct = env
    await setup_symbol()
    fake.order_mode = "reject"
    for i in range(settings.max_order_failures):
        pid, tok = await make_proposal()
        res = await executor.confirm(acct, pid, tok)
        assert res["status"] == "REJECTED_BY_EXCHANGE"
    assert not await executor.trading_enabled()  # tự tắt sau lỗi liên tiếp
    pid, tok = await make_proposal()
    with pytest.raises(executor.Reject, match="TẮT"):
        await executor.confirm(acct, pid, tok)


async def test_protection_rejected_is_reported_not_claimed(env):
    fake, acct = env
    await setup_symbol()
    fake.oco_mode = "reject"
    pid, tok = await make_proposal()
    res = await executor.confirm(acct, pid, tok)
    assert res["status"] == "FILLED" and res["protection"]["status"] == "PLACE_FAILED"
    async with SessionLocal() as s:
        pos = await s.get(Position, "T1USDT")
    assert pos.protection_status == "PLACE_FAILED" and not pos.protect_list_id
    fake.oco_mode = "ok"  # người dùng bấm đặt lại
    out = await executor.reprotect(acct, "T1USDT")
    assert out["status"] == "VERIFIED"
    async with SessionLocal() as s:
        assert (await s.get(Position, "T1USDT")).protection_status == "VERIFIED"


async def test_protection_not_placed_when_price_at_stop(env):
    fake, acct = env
    await setup_symbol()
    pid, tok = await make_proposal()
    set_market("T1USDT", 97.9, 98.0, 98.0)  # giá rơi sát stop sau khi khớp
    pos = await executor.place_protection(acct, "T1USDT", 2.0, 98.0, 106.0, await _sym(), pid)
    assert pos["status"] == "PLACE_FAILED" and fake.n("POST", "/api/v3/orderList/oco") == 0


async def _sym():
    async with SessionLocal() as s:
        return await s.get(Symbol, "T1USDT")


# ---------- lỗi mơ hồ: timeout ----------
async def test_timeout_after_order_reached_exchange_is_reconciled(env):
    fake, acct = env
    await setup_symbol()
    fake.order_mode = "timeout_sent"
    pid, tok = await make_proposal()
    res = await executor.confirm(acct, pid, tok)
    assert res["status"] == "FILLED"  # đối soát bằng client order id, không gửi lại
    assert fake.n("POST", "/api/v3/order") == 1


async def test_timeout_and_order_absent_is_failed_not_sent(env):
    fake, acct = env
    await setup_symbol()
    fake.order_mode = "timeout_unsent"
    pid, tok = await make_proposal()
    res = await executor.confirm(acct, pid, tok)
    assert res["status"] == "REJECTED_BY_EXCHANGE"
    assert [o.status for o in await sym_orders("ENTRY") if o.id.endswith(pid.replace("-", "")[:30])] == ["FAILED_NOT_SENT"]
    assert fake.n("POST", "/api/v3/order") == 1  # không tự gửi lại


async def test_timeout_unknown_state_disables_trading(env):
    fake, acct = env
    await setup_symbol()
    fake.order_mode = "timeout_unknown"
    pid, tok = await make_proposal()
    with pytest.raises(executor.UnknownOutcome):
        await executor.confirm(acct, pid, tok)
    assert (await status_of(pid))[0] == "UNKNOWN_NEEDS_REVIEW"
    assert not await executor.trading_enabled()


# ---------- bán ----------
async def _open_position(fake, acct):
    await setup_symbol()
    pid, tok = await make_proposal()
    await executor.confirm(acct, pid, tok)


async def test_sell_cancels_protection_first_then_sells(env):
    fake, acct = env
    await _open_position(fake, acct)
    set_market("T1USDT", 101.0, 101.1, 101.0)
    pid, tok = await make_proposal(side="SELL", qty=2.167, ref=101.0)
    res = await executor.confirm(acct, pid, tok)
    assert res["status"] == "FILLED" and res["remaining_qty"] < 0.001 and "warning" not in res
    cancel_i = fake.calls.index(("DELETE", "/api/v3/orderList"))
    sell_i = len(fake.calls) - 1 - fake.calls[::-1].index(("POST", "/api/v3/order"))
    assert cancel_i < sell_i and not fake.lists


async def test_sell_aborts_when_cancel_fails(env):
    fake, acct = env
    await _open_position(fake, acct)
    fake.cancel_fail = True
    set_market("T1USDT", 101.0, 101.1, 101.0)
    orders_before = fake.n("POST", "/api/v3/order")
    pid, tok = await make_proposal(side="SELL", qty=2.167, ref=101.0)
    with pytest.raises(executor.Reject, match="bảo vệ"):
        await executor.confirm(acct, pid, tok)
    assert fake.n("POST", "/api/v3/order") == orders_before  # chưa bán gì
    async with SessionLocal() as s:
        assert (await s.get(Position, "T1USDT")).protection_status == "VERIFIED"


async def test_editing_plan_marks_exchange_protection_stale(env):
    fake, acct = env
    await _open_position(fake, acct)
    out = await portfolio.set_protection("T1USDT", 97.0, 106.0)
    assert out["protection_status"] == "STALE_ON_EXCHANGE"


# ---------- LIVE ----------
async def test_live_blocked_without_ack_and_readiness(env):
    settings.trading_mode, settings.binance_env = "LIVE", "mainnet"
    settings.live_trading_ack = ""
    ok, why = await executor.can_trade()
    assert not ok and "LIVE_TRADING_ACK" in why
    settings.live_trading_ack = LIVE_ACK
    ok, why = await executor.can_trade()
    assert not ok and "ngưỡng" in why  # chưa đạt paper/backtest
    settings.live_trading_ack = ""


def test_live_config_validation():
    with pytest.raises(Exception):
        Settings(trading_mode="LIVE")
    with pytest.raises(Exception):
        Settings(trading_mode="LIVE", live_trading_ack=LIVE_ACK, binance_env="testnet")
    assert Settings(trading_mode="LIVE", live_trading_ack=LIVE_ACK).trading_mode == "LIVE"


async def test_manual_plan_proposal_confirms_without_buy_setup(env):
    """Kế hoạch thủ công: người dùng đã xem kết luận nên không bắt buộc BUY_SETUP/1h UP, nhưng mọi kiểm tra
    an toàn khác (dữ liệu, giá lệch, số dư, bộ lọc sàn, xác nhận) vẫn áp dụng."""
    fake, acct = env
    await setup_symbol()
    async with SessionLocal() as s:
        a = await s.scalar(select(Analysis).where(Analysis.symbol == "T1USDT").order_by(Analysis.created_at.desc()).limit(1))
        a.report = json.dumps({"state": "WAIT", "timeframes": {"1h": {"trend": "DOWN"}}})
        await s.commit()
    analysis_engine._last["T1USDT"]["candles"] = {"1h": 1}  # phân tích cũ
    pid = str(uuid.uuid4())
    data = {"strategy": "manual_plan", "side": "BUY", "qty": 0.1, "entry": 100.0, "stop": 98.0, "take_profit": 104.0,
            "risk_usdt": 0.3}
    async with SessionLocal() as s:
        s.add(Proposal(id=pid, symbol="T1USDT", side="BUY", expires_at=time.time() + 300, ref_price=100.0,
                       data=json.dumps(data)))
        await s.commit()
    res = await executor.confirm(acct, pid, executor.confirm_token(pid, "BUY", data))
    assert res["status"] == "FILLED" and fake.n("POST", "/api/v3/order") == 1
    # ... nhưng giá lệch thì vẫn bị chặn
    pid2 = str(uuid.uuid4())
    async with SessionLocal() as s:
        s.add(Proposal(id=pid2, symbol="T1USDT", side="BUY", expires_at=time.time() + 300, ref_price=90.0,
                       data=json.dumps(data)))
        await s.commit()
    with pytest.raises(executor.Reject):
        await executor.confirm(acct, pid2, executor.confirm_token(pid2, "BUY", data))
    assert fake.n("POST", "/api/v3/order") == 1
