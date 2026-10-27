"""Giai đoạn 9: tự động mua/thoát trên testnet/LIVE."""
import json

import pytest
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import AuditLog, Order, Proposal
from app.trading import auto, executor

from .test_phase7 import (_open_position, env, make_proposal, set_market,  # noqa: F401
                          setup_symbol)


@pytest.fixture
async def au(env):  # noqa: F811
    fake, acct = env
    saved = {k: getattr(settings, k) for k in ("auto_max_order_usdt", "auto_max_buys_per_day",
                                               "auto_cooldown_s", "auto_retry_s")}
    auto.set_account(acct)
    auto._next_ok.clear()
    async with SessionLocal() as s:
        await s.execute(delete(AuditLog).where(AuditLog.action.like("auto_%")))
        await s.commit()
    await executor.set_auto_enabled(True, "test")
    yield fake, acct
    auto.set_account(None)
    for k, v in saved.items():
        setattr(settings, k, v)


async def report_for(pid: str, state: str) -> dict:
    async with SessionLocal() as s:
        p = await s.get(Proposal, pid)
    return {"proposal_id": pid, "proposal": json.loads(p.data), "state": state}


async def count_audit(action: str) -> int:
    async with SessionLocal() as s:
        return len((await s.scalars(select(AuditLog).where(AuditLog.action == action))).all())


async def test_auto_off_does_nothing(au):
    fake, acct = au
    await setup_symbol()
    await executor.set_auto_enabled(False)
    pid, _ = await make_proposal(qty=0.4)
    assert await auto.maybe_execute("T1USDT", await report_for(pid, "BUY_SETUP")) is None
    assert fake.n("POST", "/api/v3/order") == 0


async def test_auto_buy_goes_through_confirm_and_places_protection(au):
    fake, acct = au
    await setup_symbol()
    pid, _ = await make_proposal(qty=0.4)
    out = await auto.maybe_execute("T1USDT", await report_for(pid, "BUY_SETUP"))
    assert out["status"] == "FILLED" and out["protection"]["status"] == "VERIFIED"
    assert fake.n("POST", "/api/v3/order") == 1 and await count_audit("auto_buy") == 1


async def test_auto_buy_cuts_quantity_to_cap(au):
    fake, acct = au
    await setup_symbol()
    settings.auto_max_order_usdt = 50
    pid, _ = await make_proposal(qty=2.17)  # ~217 USDT
    out = await auto.maybe_execute("T1USDT", await report_for(pid, "BUY_SETUP"))
    assert out["status"] == "FILLED"
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Order).where(Order.proposal_id == pid, Order.kind == "ENTRY"))).all()
    assert rows and rows[0].qty <= 0.5 + 1e-9


async def test_auto_buy_respects_daily_limit_and_cooldown(au):
    fake, acct = au
    await setup_symbol()
    settings.auto_max_buys_per_day = 0
    pid, _ = await make_proposal(qty=0.4)
    assert await auto.maybe_execute("T1USDT", await report_for(pid, "BUY_SETUP")) is None
    assert fake.n("POST", "/api/v3/order") == 0
    settings.auto_max_buys_per_day = 3
    assert (await auto.maybe_execute("T1USDT", await report_for(pid, "BUY_SETUP")))["status"] == "FILLED"
    pid2, _ = await make_proposal(qty=0.4)  # cùng mã ngay sau đó: đang nghỉ
    assert await auto.maybe_execute("T1USDT", await report_for(pid2, "BUY_SETUP")) is None
    assert fake.n("POST", "/api/v3/order") == 1


async def test_auto_rejection_is_audited_and_retry_is_throttled(au):
    fake, acct = au
    await setup_symbol()
    fake.test_fail = True  # sàn từ chối ở /order/test -> Reject trước khi gửi lệnh
    pid, _ = await make_proposal(qty=0.4)
    assert await auto.maybe_execute("T1USDT", await report_for(pid, "BUY_SETUP")) is None
    assert await count_audit("auto_failed") == 1 and fake.n("POST", "/api/v3/order") == 0
    pid2, _ = await make_proposal(qty=0.4)
    await auto.maybe_execute("T1USDT", await report_for(pid2, "BUY_SETUP"))  # chưa tới giờ thử lại
    assert await count_audit("auto_failed") == 1


async def test_auto_ignores_manual_plans_and_non_setup_states(au):
    fake, acct = au
    await setup_symbol()
    pid, _ = await make_proposal(qty=0.4)
    rep = await report_for(pid, "BUY_SETUP")
    rep["proposal"]["strategy"] = "manual_plan"
    assert await auto.maybe_execute("T1USDT", rep) is None
    assert await auto.maybe_execute("T1USDT", await report_for(pid, "WAIT")) is None
    assert fake.n("POST", "/api/v3/order") == 0


async def test_auto_exit_only_on_full_exit_not_reduce(au):
    fake, acct = au
    await _open_position(fake, acct)
    set_market("T1USDT", 101.0, 101.1, 101.0)
    pid, _ = await make_proposal(side="SELL", qty=2.167, ref=101.0)
    rep = await report_for(pid, "REDUCE")
    rep["proposal"]["strategy"] = "position_exit"
    before = fake.n("POST", "/api/v3/order")
    assert await auto.maybe_execute("T1USDT", rep) is None  # REDUCE: người xác nhận
    assert fake.n("POST", "/api/v3/order") == before
    rep["state"] = "SELL_EXIT"
    out = await auto.maybe_execute("T1USDT", rep)
    assert out["status"] == "FILLED" and await count_audit("auto_exit") == 1


async def test_turning_trading_off_turns_auto_off(au):
    assert await executor.auto_enabled()
    await executor.set_trading_enabled(False, "test")
    assert not await executor.auto_enabled()


async def test_auto_never_runs_outside_testnet_live(au):
    fake, acct = au
    await setup_symbol()
    settings.trading_mode = "PAPER_TRADING"
    pid, _ = await make_proposal(qty=0.4)
    assert await auto.maybe_execute("T1USDT", await report_for(pid, "BUY_SETUP")) is None
    assert fake.n("POST", "/api/v3/order") == 0
