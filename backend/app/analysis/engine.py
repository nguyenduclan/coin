"""Tính chỉ báo đa khung từ nến đã đóng, dựng ngữ cảnh, và (tuỳ chọn) nhờ AI diễn giải."""
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select, update

from ..ai import providers
from ..core.config import settings
from ..core.db import SessionLocal
from ..data.state import market
from ..indicators import ta
from ..trading.context import provider
from ..trading.strategy import build_setup, evaluate_position, trailing_suggestion
from ..models import Analysis, Candle, NewsItem, Proposal, Symbol

log = logging.getLogger("coin.analysis")

SYSTEM_PROMPT = (
    "Bạn là trợ lý phân tích thị trường crypto. CHỈ dùng số liệu và tin trong dữ liệu JSON được cung cấp. "
    "Không bịa tin, giá hay sự kiện. Không hứa hẹn lợi nhuận, không đưa xác suất thắng. "
    "Tin có verified_level UNVERIFIED_MEDIA là chưa xác minh, phải nêu rõ như vậy. "
    "Không đề xuất giá vào lệnh, stop-loss hay khối lượng. "
    "Trả về DUY NHẤT một JSON với các khóa: summary (string), scenarios "
    "({bullish, bearish, sideways}: string), risks (list string), news_view (string), caveats (list string). "
    "Viết tiếng Việt, ngắn gọn."
)

_last: dict[str, dict] = {}  # symbol -> {"candles": {...}, "price": float, "at": float}
_ai_calls = {"day": "", "n": 0}


async def load_df(symbol: str, tf: str, limit: int = 500) -> pd.DataFrame:
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(Candle.open_time, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume)
            .where(Candle.symbol == symbol, Candle.tf == tf)
            .order_by(Candle.open_time.desc()).limit(limit))).all()
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
    return df.iloc[::-1].reset_index(drop=True)


def preliminary_state(tf_summaries: dict[str, dict]) -> tuple[str, str]:
    """Trạng thái sơ bộ theo luật. Điểm vào/ra thuộc Giai đoạn 4 nên chưa có BUY SETUP."""
    trends = {tf: s.get("trend") for tf, s in tf_summaries.items()}
    if all(t in (None, "INSUFFICIENT") for t in trends.values()):
        return "DATA_UNAVAILABLE", "Chưa đủ nến đã đóng ở mọi khung"
    t1h, t4h = trends.get("1h"), trends.get("4h")
    if t1h in ("UP", "DOWN") and t4h in ("UP", "DOWN") and t1h != t4h:
        return "NO_TRADE", "Khung 1h và 4h mâu thuẫn"
    if t1h in (None, "INSUFFICIENT", "SIDEWAYS"):
        return "WAIT", "Khung 1h chưa có xu hướng rõ"
    return "WAIT", f"Xu hướng 1h {t1h}; chưa đủ điều kiện vào lệnh"


async def recent_news(base_asset: str, limit: int = 8) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    async with SessionLocal() as s:
        rows = (await s.scalars(
            select(NewsItem).where(NewsItem.assets.contains(base_asset), NewsItem.fetched_at >= since)
            .order_by(NewsItem.fetched_at.desc()).limit(limit))).all()
    return [{"title": r.title, "source": r.source, "url": r.url, "verified_level": r.verified_level,
             "published_at": r.published_at.isoformat() if r.published_at else None} for r in rows]


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("AI không trả về JSON")
    return json.loads(m.group(0))


def _ai_budget_ok() -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _ai_calls["day"] != today:
        _ai_calls.update(day=today, n=0)
    return _ai_calls["n"] < settings.ai_daily_call_limit


async def analyze(symbol: str, base_asset: str, use_ai: bool = True) -> dict:
    now = time.time()
    status = market.status(symbol, now)
    tfs: dict[str, dict] = {}
    for tf in settings.tf_list:
        tfs[tf] = ta.summarize(await load_df(symbol, tf))
    state, reason = preliminary_state(tfs)
    if status["state"] == "DATA_UNAVAILABLE":
        state, reason = "DATA_UNAVAILABLE", status["reason"]
    st = market.symbols.get(symbol)
    news = await recent_news(base_asset)
    report = {
        "symbol": symbol, "state": state, "state_reason": reason,
        "ref_price": st.last if st else None,
        "price_received_at": st.received_at if st else None,
        "data_status": status, "timeframes": tfs, "news": news,
        "news_note": "" if news else "Không có tin liên quan trong 24h qua từ các nguồn đã cấu hình",
        "ai": None, "ai_provider": "none", "ai_error": "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    proposal = None
    if state in ("WAIT", "NO_TRADE") and status["state"] == "OK" and report["ref_price"]:
        async with SessionLocal() as s:
            sym = await s.get(Symbol, symbol)
        filters = {"tick_size": sym.tick_size, "step_size": sym.step_size,
                   "min_notional": sym.min_notional} if sym else {}
        pos = await provider().get_position(symbol)
        if pos:
            # Spot: không mua thêm/bình quân giá xuống; chỉ đánh giá giữ hay thoát
            ev = evaluate_position(pos, report, report["ref_price"], filters.get("step_size") or 0,
                                   filters.get("min_notional") or 0)
            state = report["state"] = ev["state"]
            report["state_reason"] = ev["reason"]
            proposal = ev["proposal"]
            report["position"] = {**pos, "trailing": trailing_suggestion(
                pos, report["ref_price"], report["timeframes"].get("15m", {}).get("atr14"))}
        elif state == "WAIT":
            acct = await provider().equity_and_risk()
            report["account"] = {k: acct[k] for k in ("equity", "source", "open_risk")}
            setup = build_setup(report, filters, report["ref_price"], acct)
            if setup["ok"]:
                proposal = setup["proposal"]
                state = report["state"] = "BUY_SETUP"
                report["state_reason"] = "Thỏa điều kiện chiến lược trend_pullback"
            else:
                report["no_setup_reasons"] = setup["reasons"]
    report["proposal"] = proposal

    if use_ai and settings.ai_provider != "none" and state != "DATA_UNAVAILABLE":
        if not _ai_budget_ok():
            report["ai_error"] = "Vượt giới hạn số lần gọi AI trong ngày"
        else:
            try:
                _ai_calls["n"] += 1
                facts = {k: report[k] for k in ("symbol", "state", "state_reason", "ref_price",
                                                "data_status", "timeframes", "news")}
                text = await providers.complete(SYSTEM_PROMPT, json.dumps(facts, ensure_ascii=False))
                report["ai"] = _extract_json(text)
                report["ai_provider"] = settings.ai_provider
            except Exception as e:  # noqa: BLE001  — AI lỗi không được làm hỏng dữ liệu kỹ thuật
                report["ai_error"] = str(e)[:300]
                log.warning("AI failed for %s: %r", symbol, e)
    async with SessionLocal() as s:
        row = Analysis(symbol=symbol, state=state, ref_price=report["ref_price"],
                       data_status=status["state"], ai_provider=report["ai_provider"],
                       ai_error=report["ai_error"], report=json.dumps(report, ensure_ascii=False))
        s.add(row)
        await s.flush()
        if proposal:
            # Mỗi mã chỉ giữ một đề xuất đang chờ; cái cũ bị thay thế, không bao giờ thực thi lại
            await s.execute(update(Proposal).where(Proposal.symbol == symbol,
                                                   Proposal.status == "PROPOSED")
                            .values(status="SUPERSEDED"))
            pid = str(uuid.uuid4())
            report["proposal_id"] = pid
            s.add(Proposal(id=pid, symbol=symbol, expires_at=now + settings.proposal_ttl_s,
                           side=proposal["side"], ref_price=report["ref_price"], analysis_id=row.id,
                           data=json.dumps(proposal)))
            row.report = json.dumps(report, ensure_ascii=False)
        await s.commit()
    if proposal and settings.trading_mode == "PAPER_TRADING":
        # Paper: khớp giả lập ngay bằng bid/ask thật. Tiền thật (Giai đoạn 7) luôn cần xác nhận thủ công.
        report["paper_fill"] = await provider().execute_proposal(symbol, proposal, report["proposal_id"])
    _last[symbol] = {"candles": dict(st.last_closed_candle) if st else {},
                     "price": report["ref_price"], "at": now}
    if proposal and settings.trading_mode in ("BINANCE_TESTNET", "LIVE"):
        from ..trading import auto  # chỉ chạy khi công tắc giao dịch và công tắc tự động đều bật
        await auto.maybe_execute(symbol, report)
    return report


def needs_refresh(symbol: str, move_pct: float = 0.5) -> bool:
    """Chỉ phân tích lại khi có nến mới đóng hoặc giá đổi đáng kể kể từ lần trước."""
    prev, st = _last.get(symbol), market.symbols.get(symbol)
    if not st or st.last is None:
        return False
    if not prev:
        return True
    if dict(st.last_closed_candle) != prev["candles"]:
        return True
    return bool(prev["price"]) and abs(st.last / prev["price"] - 1) * 100 >= move_pct


def analysis_is_current(symbol: str) -> bool:
    """Phân tích gần nhất đã tính trên nến đóng mới nhất của mọi khung (chưa có nến mới sau đó)."""
    prev, st = _last.get(symbol), market.symbols.get(symbol)
    return bool(prev and st and dict(st.last_closed_candle) == prev["candles"])


async def expire_proposals() -> None:
    """Đề xuất quá hạn chuyển sang EXPIRED để lịch sử phản ánh đúng; không bao giờ thực thi lại."""
    async with SessionLocal() as s:
        await s.execute(update(Proposal).where(Proposal.status == "PROPOSED", Proposal.expires_at < time.time())
                        .values(status="EXPIRED"))
        await s.commit()
