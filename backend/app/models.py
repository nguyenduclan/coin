from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Symbol(Base):
    __tablename__ = "symbols"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    base_asset: Mapped[str] = mapped_column(String(20))
    quote_asset: Mapped[str] = mapped_column(String(20))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    tick_size: Mapped[float] = mapped_column(Float, default=0.0)
    step_size: Mapped[float] = mapped_column(Float, default=0.0)
    min_notional: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Candle(Base):
    """Chỉ lưu nến ĐÃ ĐÓNG."""

    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("symbol", "tf", "open_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    tf: Mapped[str] = mapped_column(String(4))
    open_time: Mapped[int] = mapped_column(BigInteger)  # ms
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    close_time: Mapped[int] = mapped_column(BigInteger)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(20), default="binance")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    action: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str] = mapped_column(String(500), default="")


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    assets: Mapped[str] = mapped_column(String(200), default="")  # "BTC,ETH"
    verified_level: Mapped[str] = mapped_column(String(30), default="UNVERIFIED_MEDIA")


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    state: Mapped[str] = mapped_column(String(20))
    ref_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_status: Mapped[str] = mapped_column(String(20))
    ai_provider: Mapped[str] = mapped_column(String(20), default="none")
    ai_error: Mapped[str] = mapped_column(String(300), default="")
    report: Mapped[str] = mapped_column(Text)  # JSON


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[float] = mapped_column(Float)  # epoch giây
    ref_price: Mapped[float] = mapped_column(Float)
    side: Mapped[str] = mapped_column(String(4), default="BUY")
    status: Mapped[str] = mapped_column(String(15), default="PROPOSED", index=True)
    analysis_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[str] = mapped_column(Text)  # JSON: entry/stop/tp/qty/risk/...


class Trade(Base):
    """Lệnh đã khớp (fill) đọc từ Binance."""

    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(60), primary_key=True)  # source:symbol:tradeId
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    fee_asset: Mapped[str] = mapped_column(String(20), default="")
    fee_converted: Mapped[bool] = mapped_column(Boolean, default=True)  # False: phí bằng coin khác, chưa quy đổi
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(20), default="binance")


class Position(Base):
    __tablename__ = "positions"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    # Stop/TP là KẾ HOẠCH do người dùng nhập; chưa có lệnh bảo vệ trên Binance (Giai đoạn 7)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    protection_status: Mapped[str] = mapped_column(String(20), default="NONE")
    protect_list_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # client list id của OCO
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    equity: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(10), default="binance")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    symbols: Mapped[str] = mapped_column(String(200))
    days: Mapped[int] = mapped_column(Integer)
    equity: Mapped[float] = mapped_column(Float)
    result: Mapped[str] = mapped_column(Text)  # JSON


class PaperAccount(Base):
    __tablename__ = "paper_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # luôn = 1
    cash: Mapped[float] = mapped_column(Float)
    start_equity: Mapped[float] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    qty: Mapped[float] = mapped_column(Float)
    avg_cost: Mapped[float] = mapped_column(Float)  # đã gồm phí mua
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_risk: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)  # chỉ có ở lệnh bán
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(String(60))
    proposal_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class Order(Base):
    """Mọi lệnh gửi lên sàn (kể cả thất bại). Ghi TRƯỚC khi gửi để khôi phục được khi sập giữa chừng."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # client order id
    proposal_id: Mapped[str | None] = mapped_column(String(36), index=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(10))  # ENTRY | EXIT | OCO
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # giá tham chiếu lúc xác nhận
    status: Mapped[str] = mapped_column(String(30))
    executed_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fee_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    exchange_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error: Mapped[str] = mapped_column(String(400), default="")
    env: Mapped[str] = mapped_column(String(10), default="mainnet")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
