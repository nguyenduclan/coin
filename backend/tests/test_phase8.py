"""Giai đoạn 8: giới hạn vốn LIVE, kill switch, đối soát, preflight, cảnh báo."""
import httpx
import pytest
from sqlalchemy import delete

from app import alerts, validation
from app.core.config import LIVE_ACK, settings
from app.core.db import SessionLocal
from app.models import AuditLog, EquitySnapshot, Order, Position
from app.portfolio import service as portfolio
from app.trading import executor, guards, preflight, reconcile

from .test_phase7 import (FakeBinance, env, make_proposal, setup_symbol,  # noqa: F401
                          status_of, sym_orders)


class Fake8(FakeBinance):
    """Thêm /openOrders và /apiRestrictions."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.open_list: list[dict] = []
        self.restrictions = {"enableWithdrawals": False, "ipRestrict": True, "enableReading": True}

    def handler(self, req: httpx.Request):
        if req.url.path == "/api/v3/openOrders":
            self.calls.append((req.method, req.url.path))
            if req.method == "DELETE":
                self.open_list = []
                self.lists.clear()
                return httpx.Response(200, json=[])
            sym = req.url.params.get("symbol")
            return httpx.Response(200, json=[o for o in self.open_list if not sym or o["symbol"] == sym])
        if req.url.path == "/sapi/v1/account/apiRestrictions":
            return httpx.Response(200, json=self.restrictions)
        return super().handler(req)


@pytest.fixture
async def live(env, monkeypatch):  # noqa: F811
    fake, acct = env
    saved = {k: getattr(settings, k) for k in ("trading_mode", "binance_env", "live_trading_ack",
                                               "live_max_order_usdt", "live_max_daily_loss_usdt",
                                               "live_max_open_positions", "telegram_bot_token",
                                               "telegram_chat_id")}
    f8 = Fake8()
    acct._client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(f8.handler))
    settings.trading_mode, settings.binance_env, settings.live_trading_ack = "LIVE", "mainnet", LIVE_ACK
    settings.live_max_order_usdt, settings.live_max_daily_loss_usdt, settings.live_max_open_positions = 50, 20, 2
    settings.telegram_bot_token = settings.telegram_chat_id = ""

    async def ready():
        return {"all_passed": True, "criteria": []}
    monkeypatch.setattr(validation, "readiness", ready)
    async with SessionLocal() as s:
        await s.execute(delete(EquitySnapshot))
        await s.commit()
    reconcile.state.update(at=None, issues=[], error=None)
    reconcile._alerted.clear()
    reconcile._open_prev.clear()
    yield f8, acct
    for k, v in saved.items():
        setattr(settings, k, v)


async def snapshot(equity: float):
    async with SessionLocal() as s:
        s.add(EquitySnapshot(equity=equity, source="binance"))
        await s.commit()


def set_balance(usdt: float):
    portfolio.account_state.update(balances={"USDT": usdt}, locked={}, at=1.0)


# ---------- giới hạn vốn ----------
async def test_order_cap_rejects_before_any_order_is_sent(live):
    fake, acct = live
    await snapshot(1000.0)
    set_balance(1000.0)
    await setup_symbol()
    pid, tok = await make_proposal()  # ~217 USDT > 50
    with pytest.raises(executor.Reject) as e:
        await executor.confirm(acct, pid, tok)
    assert "LIVE_MAX_ORDER_USDT" in e.value.msg
    assert fake.n("POST", "/api/v3/order") == 0
    assert (await status_of(pid))[0] == "FAILED_PRECHECK"


async def test_order_cap_allows_small_order(live):
    fake, acct = live
    await snapshot(1000.0)
    set_balance(1000.0)
    await setup_symbol()
    pid, tok = await make_proposal(qty=0.4)  # ~40 USDT
    out = await executor.confirm(acct, pid, tok)
    assert out["status"] in ("FILLED", "PARTIAL_FILLED")
    assert fake.n("POST", "/api/v3/order") == 1


async def test_limits_zero_means_blocked_not_unlimited(live):
    settings.live_max_order_usdt = 0
    assert await guards.buy_limit_reasons("T1USDT", 1.0)
    settings.live_max_order_usdt, settings.live_max_open_positions = 50, 0
    assert await guards.buy_limit_reasons("T1USDT", 1.0)


async def test_max_open_positions(live):
    settings.live_max_open_positions = 1
    async with SessionLocal() as s:
        s.add(Position(symbol="ZZZUSDT", qty=1.0, avg_cost=1.0))
        await s.commit()
    assert await guards.buy_limit_reasons("T1USDT", 10.0)  # mã mới vượt số vị thế
    assert not await guards.buy_limit_reasons("ZZZUSDT", 10.0)  # thêm vào mã đang giữ thì được


async def test_limits_ignored_outside_live(live):
    settings.trading_mode = "BINANCE_TESTNET"
    assert await guards.buy_limit_reasons("T1USDT", 1e9) == []


# ---------- lỗ ngày ----------
async def test_daily_loss_blocks_and_auto_disables(live):
    fake, acct = live
    await snapshot(1000.0)
    set_balance(985.0)
    assert await guards.daily_loss_reason() == ""
    set_balance(970.0)  # lỗ 30 >= 20
    assert "chạm giới hạn" in await guards.daily_loss_reason()
    ok, why = await executor.can_trade()
    assert not ok and "LIVE bị chặn" in why
    assert await executor.enforce_daily_loss() is True
    assert not await executor.trading_enabled()
    assert await executor.enforce_daily_loss() is False  # đã tắt, không lặp


async def test_daily_loss_unknown_baseline_blocks_live(live):
    set_balance(1000.0)  # không có snapshot nào hôm nay
    assert "mốc vốn" in await guards.daily_loss_reason()


# ---------- kill switch ----------
async def test_kill_switch_disables_without_touching_protection(live):
    fake, acct = live
    out = await executor.kill_switch(acct)
    assert out["trading_enabled"] is False and not await executor.trading_enabled()
    assert fake.n("DELETE", "/api/v3/openOrders") == 0


async def test_kill_switch_cancel_orders_marks_unprotected(live):
    fake, acct = live
    async with SessionLocal() as s:
        s.add(Position(symbol="T1USDT", qty=1.0, avg_cost=100.0, protection_status="VERIFIED",
                       protect_list_id="cbo1"))
        await s.commit()
    fake.open_list = [{"symbol": "T1USDT", "orderId": 5, "clientOrderId": "x", "orderListId": 7}]
    out = await executor.kill_switch(acct, cancel_orders=True)
    assert "T1USDT" in out["canceled"]
    async with SessionLocal() as s:
        assert (await s.get(Position, "T1USDT")).protection_status == "NONE"


# ---------- đối soát ----------
async def test_reconcile_detects_missing_protection_and_foreign_order(live):
    fake, acct = live
    set_balance(1000.0)
    portfolio.account_state["balances"]["T1"] = 1.0
    async with SessionLocal() as s:
        s.add(Position(symbol="T1USDT", qty=1.0, avg_cost=100.0, protection_status="VERIFIED",
                       protect_list_id="cbo-gone"))
        await s.commit()
    fake.open_list = [{"symbol": "ABCUSDT", "orderId": 1, "clientOrderId": "manual", "orderListId": -1,
                       "side": "BUY", "origQty": "1", "price": "1"}]
    st = await reconcile.run(acct)
    keys = {i["key"] for i in st["issues"]}
    assert "prot:T1USDT" in keys and "foreign:ABCUSDT:1" in keys
    async with SessionLocal() as s:
        assert (await s.get(Position, "T1USDT")).protection_status == "NONE"
    await reconcile.run(acct)  # lần 2: 'prot' đã chuyển thành 'noprot' (vấn đề mới, cảnh báo một lần)
    n = await _audit_count("reconcile_issue")
    await reconcile.run(acct)  # lần 3: không cảnh báo lại cùng vấn đề
    assert await _audit_count("reconcile_issue") == n


async def test_reconcile_detects_balance_mismatch(live):
    fake, acct = live
    set_balance(1000.0)
    portfolio.account_state["balances"]["T1"] = 0.2
    async with SessionLocal() as s:
        s.add(Position(symbol="T1USDT", qty=1.0, avg_cost=100.0, protection_status="SIMULATED"))
        await s.commit()
    st = await reconcile.run(acct)
    assert any(i["key"] == "mismatch:T1USDT" and i["severity"] == "block" for i in st["issues"])


async def test_reconcile_keeps_previous_state_when_exchange_unreadable(live):
    fake, acct = live
    reconcile.state["issues"] = [{"key": "k", "severity": "block", "text": "t"}]
    fake.account_fail = True
    orig = fake.handler

    def boom(req):
        if req.url.path == "/api/v3/openOrders":
            return httpx.Response(503, text="down")
        return orig(req)
    acct._client = httpx.AsyncClient(base_url="https://x.test", transport=httpx.MockTransport(boom))
    st = await reconcile.run(acct)
    assert st["error"] and st["issues"]  # không xoá vấn đề cũ khi không đọc được sàn


async def _audit_count(action: str) -> int:
    from sqlalchemy import func, select
    async with SessionLocal() as s:
        return await s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == action))


# ---------- preflight ----------
async def test_preflight_passes_when_everything_is_in_order(live):
    fake, acct = live
    await snapshot(1000.0)
    set_balance(1000.0)
    fake.balances["USDT"] = 1000.0
    await reconcile.run(acct)
    r = await preflight.run(acct)
    failed = [i["id"] for i in r["items"] if not i["passed"] and i["severity"] == "block"]
    assert r["all_passed"], failed


async def test_preflight_blocks_withdraw_permission_and_missing_ip_restrict(live):
    fake, acct = live
    fake.restrictions = {"enableWithdrawals": True, "ipRestrict": False}
    await reconcile.run(acct)
    r = await preflight.run(acct)
    bad = {i["id"] for i in r["items"] if not i["passed"]}
    assert {"no_withdraw", "ip_restrict"} <= bad and not r["all_passed"]


async def test_preflight_blocks_when_readiness_fails_or_limits_zero(live, monkeypatch):
    fake, acct = live

    async def not_ready():
        return {"all_passed": False, "criteria": [{"passed": False}]}
    monkeypatch.setattr(validation, "readiness", not_ready)
    settings.live_max_daily_loss_usdt = 0
    r = await preflight.run(acct)
    bad = {i["id"] for i in r["items"] if not i["passed"]}
    assert {"readiness", "limits"} <= bad


async def test_preflight_blocks_on_clock_skew(live):
    fake, acct = live
    orig = acct.sync_time

    async def skewed():
        await orig()
        acct._offset_ms = 5000
    acct.sync_time = skewed
    r = await preflight.run(acct)
    assert not next(i for i in r["items"] if i["id"] == "clock")["passed"]


# ---------- cảnh báo ----------
async def test_alert_sends_telegram_and_never_raises(live):
    sent = []

    def handler(req: httpx.Request):
        sent.append((str(req.url), req.content))
        return httpx.Response(200, json={"ok": True})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await alerts.notify("x", client) is False  # chưa cấu hình
    settings.telegram_bot_token, settings.telegram_chat_id = "TOK", "42"
    assert await alerts.notify("hello", client) is True
    assert "botTOK/sendMessage" in sent[0][0] and b"hello" in sent[0][1]

    def fail(req):
        raise httpx.ConnectError("down")
    bad = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    assert await alerts.notify("hello", bad) is False
    await client.aclose()
    await bad.aclose()


async def test_audit_of_critical_action_triggers_alert(live, monkeypatch):
    seen = []

    async def fake_notify(text, client=None):
        seen.append(text)
        return True
    monkeypatch.setattr(alerts, "notify", fake_notify)
    await executor.audit("kill_switch", "x")
    await executor.audit("proposal_rejected", "y")  # không thuộc danh sách cảnh báo
    assert len(seen) == 1 and "kill_switch" in seen[0]
