import asyncio
import json
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update

from ..core import auth
from ..core.config import settings
from ..core.db import SessionLocal
from ..data.state import market
from ..analysis.engine import analyze
from ..models import Analysis, AuditLog, BacktestRun, Candle, NewsItem, Order, Proposal, Symbol
from .. import validation
from ..backtest import service as backtest_service
from ..paper import service as paper_service
from ..portfolio import service as portfolio_service
from ..trading.context import provider
from ..trading import executor, planner, preflight, reconcile
from ..trading.strategy import check_validity

router = APIRouter(prefix="/api")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,20}$")


class LoginIn(BaseModel):
    password: str


class SymbolIn(BaseModel):
    symbol: str


class SymbolPatch(BaseModel):
    active: bool


@router.get("/health")
async def health():
    return {"status": "ok", "ws_connected": market.ws_connected,
            "ws_reconnects": market.ws_reconnects, "mode": settings.trading_mode}


@router.post("/login")
async def login(body: LoginIn):
    if not auth.check_password(body.password):
        raise HTTPException(401, "Sai mật khẩu")
    return {"token": auth.make_token()}


async def _audit(action: str, detail: str = "") -> None:
    async with SessionLocal() as s:
        s.add(AuditLog(action=action, detail=detail[:500]))
        await s.commit()


async def _active_symbols() -> list[str]:
    async with SessionLocal() as s:
        rows = await s.scalars(select(Symbol.symbol).where(Symbol.active.is_(True)))
        return list(rows)


async def _sync_ingest(request: Request) -> None:
    await request.app.state.ingest.set_symbols(await _active_symbols())


@router.get("/symbols", dependencies=[Depends(auth.require_auth)])
async def list_symbols():
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Symbol).order_by(Symbol.created_at))).all()
    return [{"symbol": r.symbol, "active": r.active, "tick_size": r.tick_size,
             "step_size": r.step_size, "min_notional": r.min_notional} for r in rows]


@router.post("/symbols", dependencies=[Depends(auth.require_auth)])
async def add_symbol(body: SymbolIn, request: Request):
    sym = body.symbol.strip().upper()
    if not SYMBOL_RE.match(sym):
        raise HTTPException(400, "Mã không hợp lệ")
    async with SessionLocal() as s:
        if await s.get(Symbol, sym):
            raise HTTPException(409, "Mã đã tồn tại")
        total = await s.scalar(select(func.count()).select_from(Symbol).where(Symbol.active.is_(True)))
        if total >= settings.max_active_symbols:
            raise HTTPException(400, f"Tối đa {settings.max_active_symbols} mã đang kích hoạt")
    info = await request.app.state.rest.symbol_info(sym)
    if not info or info["status"] != "TRADING" or not info["is_spot_trading_allowed"]:
        raise HTTPException(400, f"{sym} không giao dịch được trên Binance Spot")
    async with SessionLocal() as s:
        s.add(Symbol(symbol=sym, base_asset=info["base_asset"], quote_asset=info["quote_asset"],
                     tick_size=info["tick_size"], step_size=info["step_size"],
                     min_notional=info["min_notional"]))
        await s.commit()
    await _audit("symbol_add", sym)
    await _sync_ingest(request)
    return {"symbol": sym}


@router.patch("/symbols/{symbol}", dependencies=[Depends(auth.require_auth)])
async def patch_symbol(symbol: str, body: SymbolPatch, request: Request):
    async with SessionLocal() as s:
        row = await s.get(Symbol, symbol.upper())
        if not row:
            raise HTTPException(404, "Không có mã này")
        if body.active and not row.active:
            total = await s.scalar(select(func.count()).select_from(Symbol).where(Symbol.active.is_(True)))
            if total >= settings.max_active_symbols:
                raise HTTPException(400, f"Tối đa {settings.max_active_symbols} mã đang kích hoạt")
        row.active = body.active
        await s.commit()
    await _audit("symbol_pause" if not body.active else "symbol_resume", symbol)
    await _sync_ingest(request)
    return {"symbol": symbol.upper(), "active": body.active}


@router.delete("/symbols/{symbol}", dependencies=[Depends(auth.require_auth)])
async def delete_symbol(symbol: str, request: Request):
    async with SessionLocal() as s:
        row = await s.get(Symbol, symbol.upper())
        if not row:
            raise HTTPException(404, "Không có mã này")
        await s.delete(row)
        await s.commit()
    await _audit("symbol_delete", symbol)
    await _sync_ingest(request)
    return {"deleted": symbol.upper()}


@router.get("/watchlist", dependencies=[Depends(auth.require_auth)])
async def watchlist():
    return {"mode": settings.trading_mode, "ws_connected": market.ws_connected,
            "items": market.snapshot()}


@router.get("/candles/{symbol}/{tf}", dependencies=[Depends(auth.require_auth)])
async def candles(symbol: str, tf: str, limit: int = 200):
    if tf not in settings.tf_list:
        raise HTTPException(400, "Khung thời gian không hỗ trợ")
    limit = max(1, min(limit, 1000))
    async with SessionLocal() as s:
        rows = (await s.scalars(
            select(Candle).where(Candle.symbol == symbol.upper(), Candle.tf == tf)
            .order_by(Candle.open_time.desc()).limit(limit))).all()
    return [{"t": r.open_time, "o": r.open, "h": r.high, "l": r.low, "c": r.close,
             "v": r.volume} for r in reversed(rows)]


@router.get("/stream", dependencies=[Depends(auth.require_auth)])
async def stream(request: Request):
    async def gen():
        while not await request.is_disconnected():
            payload = {"ws_connected": market.ws_connected, "items": market.snapshot()}
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/analysis/{symbol}", dependencies=[Depends(auth.require_auth)])
async def latest_analysis(symbol: str, limit: int = 1):
    limit = max(1, min(limit, 50))
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Analysis).where(Analysis.symbol == symbol.upper())
                                .order_by(Analysis.created_at.desc()).limit(limit))).all()
    if not rows:
        raise HTTPException(404, "Chưa có phân tích cho mã này")
    reports = [json.loads(r.report) for r in rows]
    return reports[0] if limit == 1 else reports


@router.post("/analysis/{symbol}/run", dependencies=[Depends(auth.require_auth)])
async def run_analysis(symbol: str):
    async with SessionLocal() as s:
        row = await s.get(Symbol, symbol.upper())
    if not row:
        raise HTTPException(404, "Không có mã này")
    return await analyze(row.symbol, row.base_asset)


@router.get("/news", dependencies=[Depends(auth.require_auth)])
async def news(asset: str | None = None, limit: int = 30):
    q = select(NewsItem).order_by(NewsItem.fetched_at.desc()).limit(max(1, min(limit, 100)))
    if asset:
        q = q.where(NewsItem.assets.contains(asset.upper()))
    async with SessionLocal() as s:
        rows = (await s.scalars(q)).all()
    return [{"title": r.title, "url": r.url, "source": r.source, "assets": r.assets,
             "published_at": r.published_at, "fetched_at": r.fetched_at,
             "verified_level": r.verified_level} for r in rows]


@router.get("/ai/status", dependencies=[Depends(auth.require_auth)])
async def ai_status():
    from ..ai.providers import configured
    return {"selected": settings.ai_provider,
            "configured": {p: configured(p) for p in ("gemini", "groq", "anthropic", "openai")}}


@router.get("/proposals", dependencies=[Depends(auth.require_auth)])
async def list_proposals(status: str | None = None, symbol: str | None = None, limit: int = 50):
    q = select(Proposal).order_by(Proposal.created_at.desc()).limit(max(1, min(limit, 200)))
    if status:
        q = q.where(Proposal.status == status.upper())
    if symbol:
        q = q.where(Proposal.symbol == symbol.upper())
    async with SessionLocal() as s:
        rows = (await s.scalars(q)).all()
    now = time.time()
    out = []
    for r in rows:
        p = {"id": r.id, "symbol": r.symbol, "side": r.side, "status": r.status,
             "created_at": r.created_at, "expires_at": r.expires_at, "ref_price": r.ref_price,
             **json.loads(r.data)}
        p["confirm_token"] = executor.confirm_token(r.id, r.side, p)
        if r.status == "PROPOSED":
            st = market.symbols.get(r.symbol)
            p["invalid_reasons"] = check_validity(
                p, now=now, price=st.last if st else None, side=r.side,
                data_state=market.status(r.symbol, now)["state"], trend_1h=None)
        out.append(p)
    return out


class ProtectionIn(BaseModel):
    stop: float | None = None
    take_profit: float | None = None


@router.get("/portfolio", dependencies=[Depends(auth.require_auth)])
async def portfolio():
    return await provider().summary()


@router.post("/portfolio/sync", dependencies=[Depends(auth.require_auth)])
async def portfolio_sync(request: Request):
    acct = request.app.state.account
    if not acct.enabled():
        raise HTTPException(400, "Chưa cấu hình BINANCE_API_KEY/BINANCE_API_SECRET")
    return await portfolio_service.sync_account(acct)


@router.patch("/portfolio/positions/{symbol}/protection", dependencies=[Depends(auth.require_auth)])
async def position_protection(symbol: str, body: ProtectionIn):
    try:
        res = await provider().set_protection(symbol.upper(), body.stop, body.take_profit)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _audit("protection_plan", f"{symbol.upper()} stop={body.stop} tp={body.take_profit}")
    return res


@router.get("/history", dependencies=[Depends(auth.require_auth)])
async def history(limit: int = 200):
    """Đề xuất (mọi trạng thái) và lệnh đã khớp. Lệnh bị từ chối/gửi thất bại sẽ có ở Giai đoạn 7."""
    limit = max(1, min(limit, 500))
    async with SessionLocal() as s:
        props = (await s.scalars(select(Proposal).order_by(Proposal.created_at.desc())
                                 .limit(limit))).all()
    return {
        "proposals": [{"id": r.id, "symbol": r.symbol, "side": r.side, "status": r.status,
                       "created_at": r.created_at, "ref_price": r.ref_price,
                       **{k: v for k, v in json.loads(r.data).items()
                          if k in ("entry", "stop", "take_profit", "qty", "notional", "risk_usdt",
                                   "rr_after_costs", "exit_reason", "strategy", "result")}} for r in props],
        "trades": await provider().trade_history(limit),
    }


@router.get("/performance", dependencies=[Depends(auth.require_auth)])
async def performance_report():
    return await provider().performance_report()


@router.get("/indicators/{symbol}/{tf}", dependencies=[Depends(auth.require_auth)])
async def indicator_series(symbol: str, tf: str, limit: int = 300):
    """Chuỗi EMA20/EMA50 để vẽ lên biểu đồ (nến đã đóng)."""
    if tf not in settings.tf_list:
        raise HTTPException(400, "Khung thời gian không hỗ trợ")
    from ..analysis.engine import load_df
    from ..indicators import ta
    df = await load_df(symbol.upper(), tf, limit=max(60, min(limit, 1000)) + 60)
    if df.empty:
        return {"ema20": [], "ema50": []}
    e20, e50 = ta.ema(df["close"], 20), ta.ema(df["close"], 50)
    keep = df.index[60:]
    return {"ema20": [{"t": int(df.open_time[i]), "v": float(e20[i])} for i in keep],
            "ema50": [{"t": int(df.open_time[i]), "v": float(e50[i])} for i in keep]}


@router.get("/coin/{symbol}", dependencies=[Depends(auth.require_auth)])
async def coin_detail(symbol: str):
    sym = symbol.upper()
    async with SessionLocal() as s:
        row = await s.get(Symbol, sym)
        if not row:
            raise HTTPException(404, "Không có mã này")
        last = await s.scalar(select(Analysis).where(Analysis.symbol == sym)
                              .order_by(Analysis.created_at.desc()).limit(1))
        hist = (await s.execute(select(Analysis.created_at, Analysis.state, Analysis.ref_price)
                                .where(Analysis.symbol == sym).order_by(Analysis.created_at.desc())
                                .limit(100))).all()
    return {
        "symbol": sym, "active": row.active,
        "tick_size": row.tick_size, "step_size": row.step_size, "min_notional": row.min_notional,
        "market": next((i for i in market.snapshot() if i["symbol"] == sym), None),
        "report": json.loads(last.report) if last else None,
        "signal_history": [{"at": h[0], "state": h[1], "ref_price": h[2]} for h in hist],
        "position": await provider().get_position(sym),
    }


@router.get("/signals", dependencies=[Depends(auth.require_auth)])
async def signals():
    """Tín hiệu mới nhất của từng mã đang kích hoạt (cho bảng Watchlist)."""
    out = {}
    for sym in await _active_symbols():
        async with SessionLocal() as s:
            a = await s.scalar(select(Analysis).where(Analysis.symbol == sym)
                               .order_by(Analysis.created_at.desc()).limit(1))
        if not a:
            continue
        r = json.loads(a.report)
        pr = r.get("proposal") or {}
        out[sym] = {"state": r["state"], "reason": r.get("state_reason"), "at": a.created_at,
                    "trends": {tf: v.get("trend") for tf, v in r["timeframes"].items()},
                    "side": pr.get("side"), "entry": pr.get("entry"), "stop": pr.get("stop"),
                    "take_profit": pr.get("take_profit"), "notional": pr.get("notional"),
                    "qty": pr.get("qty")}
    return out


class BacktestDataIn(BaseModel):
    symbols: list[str]
    days: int = 180


class BacktestRunIn(BaseModel):
    symbols: list[str]
    days: int = 180
    equity: float = 1000.0


async def _check_symbols(symbols: list[str]) -> list[str]:
    syms = [s.strip().upper() for s in symbols if s.strip()]
    if not syms or len(syms) > settings.max_active_symbols:
        raise HTTPException(400, f"Chọn 1–{settings.max_active_symbols} mã")
    async with SessionLocal() as s:
        known = set(await s.scalars(select(Symbol.symbol).where(Symbol.symbol.in_(syms))))
    missing = [x for x in syms if x not in known]
    if missing:
        raise HTTPException(400, f"Mã chưa có trong danh sách: {', '.join(missing)}")
    return syms


@router.post("/backtest/data", dependencies=[Depends(auth.require_auth)])
async def backtest_download(body: BacktestDataIn, request: Request):
    if not 30 <= body.days <= 730:
        raise HTTPException(400, "days phải trong khoảng 30–730")
    syms = await _check_symbols(body.symbols)
    await _audit("backtest_download", f"{syms} {body.days}d")
    return {"job_id": await backtest_service.download_job(request.app.state.rest, syms, body.days)}


@router.post("/backtest/run", dependencies=[Depends(auth.require_auth)])
async def backtest_start(body: BacktestRunIn):
    if not 30 <= body.days <= 730 or body.equity <= 0:
        raise HTTPException(400, "days phải trong khoảng 30–730 và equity > 0")
    syms = await _check_symbols(body.symbols)
    return {"job_id": await backtest_service.backtest_job(syms, body.days, body.equity)}


@router.get("/backtest/jobs/{job_id}", dependencies=[Depends(auth.require_auth)])
async def backtest_job_status(job_id: str):
    job = backtest_service.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Không có job này (có thể backend đã khởi động lại)")
    return job


@router.get("/backtest/runs", dependencies=[Depends(auth.require_auth)])
async def backtest_runs():
    async with SessionLocal() as s:
        rows = (await s.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(30))).all()
    return [{"id": r.id, "created_at": r.created_at, "symbols": r.symbols, "days": r.days,
             "equity": r.equity} for r in rows]


@router.get("/backtest/runs/{run_id}", dependencies=[Depends(auth.require_auth)])
async def backtest_run_detail(run_id: str):
    async with SessionLocal() as s:
        r = await s.get(BacktestRun, run_id)
    if not r:
        raise HTTPException(404, "Không có lần chạy này")
    return {"id": r.id, "created_at": r.created_at, "symbols": r.symbols, "days": r.days,
            "equity": r.equity, "result": json.loads(r.result)}


@router.get("/readiness", dependencies=[Depends(auth.require_auth)])
async def readiness_report():
    return await validation.readiness()


class ResetIn(BaseModel):
    confirm: str


@router.post("/paper/reset", dependencies=[Depends(auth.require_auth)])
async def paper_reset(body: ResetIn):
    if body.confirm != "RESET PAPER":
        raise HTTPException(400, 'Gõ đúng "RESET PAPER" để xác nhận')
    await paper_service.reset()
    await _audit("paper_reset", "")
    return {"ok": True}


@router.get("/mode")
async def mode():
    return {"trading_mode": settings.trading_mode, "binance_env": settings.binance_env}


class ConfirmIn(BaseModel):
    confirm_token: str


class TradingToggleIn(BaseModel):
    enabled: bool
    confirm: str = ""


def _reject_http(e: executor.Reject) -> HTTPException:
    return HTTPException(e.http, e.msg)


@router.post("/proposals/{pid}/confirm", dependencies=[Depends(auth.require_auth)])
async def confirm_proposal(pid: str, body: ConfirmIn, request: Request):
    try:
        return await executor.confirm(request.app.state.account, pid, body.confirm_token)
    except executor.Reject as e:
        raise _reject_http(e)


@router.post("/proposals/{pid}/reject", dependencies=[Depends(auth.require_auth)])
async def reject_proposal(pid: str):
    try:
        return await executor.reject(pid)
    except executor.Reject as e:
        raise _reject_http(e)


@router.get("/trading/status", dependencies=[Depends(auth.require_auth)])
async def trading_status():
    return await executor.status()


class KillIn(BaseModel):
    cancel_orders: bool = False  # true: huỷ cả lệnh bảo vệ OCO trên sàn


@router.get("/trading/preflight", dependencies=[Depends(auth.require_auth)])
async def trading_preflight(request: Request):
    return await preflight.run(request.app.state.account)


@router.get("/trading/reconcile", dependencies=[Depends(auth.require_auth)])
async def trading_reconcile():
    return reconcile.state


@router.post("/trading/kill", dependencies=[Depends(auth.require_auth)])
async def trading_kill(body: KillIn, request: Request):
    return await executor.kill_switch(request.app.state.account, body.cancel_orders)


class AutoToggleIn(BaseModel):
    enabled: bool
    confirm: str = ""


@router.post("/trading/auto", dependencies=[Depends(auth.require_auth)])
async def trading_auto(body: AutoToggleIn, request: Request):
    if body.enabled:
        if settings.trading_mode not in ("BINANCE_TESTNET", "LIVE"):
            raise HTTPException(400, f"Chế độ {settings.trading_mode} không dùng công tắc này "
                                     "(PAPER_TRADING vốn đã tự động)")
        if body.confirm != "ENABLE AUTO":
            raise HTTPException(400, 'Gõ đúng "ENABLE AUTO" để bật tự động mua/thoát')
        ok, why = await executor.can_trade()
        if not ok or not await executor.trading_enabled():
            raise HTTPException(409, why or "Cần bật công tắc giao dịch trước")
        if settings.trading_mode == "LIVE":
            pf = await preflight.run(request.app.state.account)
            if not pf["all_passed"]:
                failed = "; ".join(i["label"] for i in pf["items"] if not i["passed"] and i["severity"] == "block")
                raise HTTPException(409, "Preflight chưa đạt, không bật được tự động: " + failed)
    await executor.set_auto_enabled(body.enabled, "người dùng thao tác trên giao diện")
    return await executor.status()


@router.post("/trading/enabled", dependencies=[Depends(auth.require_auth)])
async def trading_toggle(body: TradingToggleIn, request: Request):
    if body.enabled:
        if settings.trading_mode not in ("BINANCE_TESTNET", "LIVE"):
            raise HTTPException(400, f"Chế độ {settings.trading_mode} không gửi lệnh lên sàn")
        if body.confirm != "ENABLE TRADING":
            raise HTTPException(400, 'Gõ đúng "ENABLE TRADING" để bật giao dịch')
        if settings.trading_mode == "LIVE":
            pf = await preflight.run(request.app.state.account)
            if not pf["all_passed"]:
                failed = "; ".join(i["label"] for i in pf["items"] if not i["passed"] and i["severity"] == "block")
                raise HTTPException(409, "Preflight chưa đạt, không bật được LIVE: " + failed)
    await executor.set_trading_enabled(body.enabled, "người dùng thao tác trên giao diện")
    return await executor.status()


@router.post("/portfolio/positions/{symbol}/protect", dependencies=[Depends(auth.require_auth)])
async def position_protect(symbol: str, request: Request):
    try:
        return await executor.reprotect(request.app.state.account, symbol.upper())
    except executor.Reject as e:
        raise _reject_http(e)


@router.get("/orders", dependencies=[Depends(auth.require_auth)])
async def orders(limit: int = 100):
    async with SessionLocal() as s:
        rows = (await s.scalars(select(Order).order_by(Order.created_at.desc())
                                .limit(max(1, min(limit, 300))))).all()
    return [{"id": r.id, "proposal_id": r.proposal_id, "kind": r.kind, "symbol": r.symbol, "side": r.side,
             "qty": r.qty, "limit_price": r.limit_price, "expected_price": r.expected_price,
             "status": r.status, "executed_qty": r.executed_qty, "avg_price": r.avg_price,
             "fee_usdt": r.fee_usdt, "error": r.error, "env": r.env, "created_at": r.created_at,
             "updated_at": r.updated_at} for r in rows]


class PlanIn(BaseModel):
    symbol: str
    amount: float
    entry_price: float | None = None
    use_ai: bool = True


class PlanProposeIn(BaseModel):
    symbol: str
    amount: float
    entry_price: float | None = None
    acknowledge: bool = False  # bắt buộc true khi kết luận không phải BUY_NOW


@router.post("/plan", dependencies=[Depends(auth.require_auth)])
async def plan_trade(body: PlanIn):
    return await planner.plan(body.symbol, body.amount, body.entry_price, body.use_ai)


@router.post("/plan/propose", dependencies=[Depends(auth.require_auth)])
async def plan_propose(body: PlanProposeIn):
    """Biến kế hoạch thành đề xuất. Số liệu được TÍNH LẠI ở backend, không tin số từ trình duyệt."""
    if settings.trading_mode == "ANALYSIS_ONLY":
        raise HTTPException(400, "Chế độ ANALYSIS_ONLY chỉ phân tích, không tạo đề xuất giao dịch")
    p = await planner.plan(body.symbol, body.amount, body.entry_price, use_ai=False)
    if p.get("verdict") in ("ERROR", "DATA_UNAVAILABLE"):
        raise HTTPException(409, "; ".join(p.get("reasons") or [p.get("error", "Không có dữ liệu")]))
    if not p.get("can_propose"):
        raise HTTPException(409, "Không thể tạo đề xuất: " + "; ".join(p.get("notes", [])))
    if p["verdict"] != "BUY_NOW" and not body.acknowledge:
        raise HTTPException(409, f"Kết luận hiện tại là {p['verdict']}. Cần xác nhận đã đọc cảnh báo (acknowledge) để tạo đề xuất")
    pl = p["plan"]
    sym = body.symbol.upper()
    data = {"strategy": "manual_plan", "side": "BUY", "entry": pl["entry"], "qty": pl["qty"],
            "notional": pl["notional"], "stop": pl["stop"], "take_profit": pl["take_profit"],
            "risk_usdt": pl["risk_usdt"], "rr_after_costs": pl["rr_tp1"], "verdict_at_creation": p["verdict"],
            "acknowledged_warnings": body.acknowledge and p["verdict"] != "BUY_NOW",
            "confirm_conditions": ["Bạn đã xem kế hoạch và kết luận"], "limits": p["limits"],
            "invalidation": [f"Giá <= {pl['stop']}", f"Giá lệch quá {settings.proposal_max_move_pct}% so với giá tham chiếu",
                             f"Quá {settings.proposal_ttl_s}s kể từ khi tạo"]}
    pid = str(uuid.uuid4())
    async with SessionLocal() as s:
        await s.execute(update(Proposal).where(Proposal.symbol == sym, Proposal.status == "PROPOSED")
                        .values(status="SUPERSEDED"))
        s.add(Proposal(id=pid, symbol=sym, side="BUY", expires_at=time.time() + settings.proposal_ttl_s,
                       ref_price=p["price"]["ask"], data=json.dumps(data)))
        await s.commit()
    await _audit("plan_proposed", f"{sym} {body.amount} verdict={p['verdict']}")
    out = {"proposal_id": pid, "verdict": p["verdict"], "mode": settings.trading_mode,
           "confirm_token": executor.confirm_token(pid, "BUY", data)}
    if settings.trading_mode == "PAPER_TRADING":  # Paper: người dùng đã bấm tạo => khớp giả lập ngay
        out["paper_fill"] = await paper_service.execute_proposal(sym, data, pid)
    return out
