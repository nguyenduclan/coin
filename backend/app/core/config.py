from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ALLOWED_MODES = {"ANALYSIS_ONLY", "PAPER_TRADING", "BINANCE_TESTNET", "LIVE"}
LIVE_ACK = "I_UNDERSTAND_REAL_MONEY_RISK"
TESTNET_REST = "https://testnet.binance.vision"
TESTNET_WS = "wss://stream.testnet.binance.vision"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./coin.db"
    binance_rest_url: str = "https://api.binance.com"
    binance_ws_url: str = "wss://stream.binance.com:9443"
    timeframes: str = "5m,15m,1h,4h,1d"
    max_active_symbols: int = 5
    # Dữ liệu cũ hơn ngưỡng này (giây) bị coi là STALE
    stale_after_seconds: float = 10.0
    backfill_limit: int = 500
    admin_password: str = ""
    session_secret: str = ""
    session_ttl_seconds: int = 12 * 3600
    trading_mode: str = "ANALYSIS_ONLY"
    # Chỉ cấp quyền Read; tắt rút tiền; giới hạn IP của VPS
    binance_api_key: str = ""
    binance_api_secret: str = ""

    # AI: none | gemini | groq | anthropic | openai
    ai_provider: str = "none"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    ai_daily_call_limit: int = 500

    # Quản trị rủi ro. Vốn nhập tay đến khi Portfolio đọc được số dư Binance (Giai đoạn 5).
    account_equity_usdt: float = 1000.0
    risk_pct: float = 0.5  # % vốn rủi ro mỗi lệnh, bị chặn trần cứng HARD_MAX_RISK_PCT
    max_alloc_pct_per_coin: float = 25.0
    max_total_risk_pct: float = 2.0
    fee_rate: float = 0.001  # mỗi chiều (0,1%); chỉnh nếu dùng BNB/VIP
    slippage_pct: float = 0.05
    min_rr: float = 1.5
    proposal_ttl_s: int = 300
    proposal_max_move_pct: float = 0.3  # giá lệch quá ngưỡng này -> đề xuất mất hiệu lực
    analysis_interval_s: int = 60
    news_interval_s: int = 180
    news_feeds: str = (
        "https://www.coindesk.com/arc/outboundfeeds/rss/,"
        "https://cointelegraph.com/rss"
    )

    # mainnet | testnet. Testnet dùng dữ liệu giá giả lập của Binance, chỉ để thử luồng lệnh (Giai đoạn 7).
    binance_env: str = "mainnet"
    market_env: str = "auto"  # auto | mainnet | testnet — nguồn dữ liệu giá
    trade_rest_url: str = ""  # tự tính từ BINANCE_ENV
    paper_start_equity: float = 1000.0

    # Thực thi lệnh (Giai đoạn 7)
    live_trading_ack: str = ""
    order_slip_buffer_pct: float = 0.10  # biên giá LIMIT-IOC khi mua so với ask
    exit_slip_buffer_pct: float = 0.50  # biên giá khi bán thoát so với bid (ưu tiên khớp)
    max_order_failures: int = 3  # số lỗi sàn liên tiếp thì tự TẮT giao dịch

    # Giới hạn vốn thật (Giai đoạn 8). Chỉ áp dụng cho LIVE; <= 0 nghĩa là CHẶN hoàn toàn (không phải "không giới hạn").
    live_max_order_usdt: float = 50.0  # giá trị tối đa mỗi lệnh mua
    live_max_daily_loss_usdt: float = 20.0  # lỗ vốn trong ngày (UTC) chạm mức này thì tự TẮT giao dịch
    live_max_open_positions: int = 2  # số vị thế mở tối đa
    preflight_max_clock_skew_ms: int = 1000

    # Tự động mua/thoát cho BINANCE_TESTNET và LIVE (PAPER vốn đã tự động). Công tắc riêng, mặc định TẮT.
    auto_max_order_usdt: float = 50.0  # giá trị tối đa mỗi lệnh tự mua (khối lượng bị cắt xuống nếu vượt)
    auto_max_buys_per_day: int = 3
    auto_cooldown_s: int = 4 * 3600  # nghỉ sau mỗi lần tự mua thành công, theo từng mã
    auto_retry_s: int = 300  # nghỉ sau một lần thử bị từ chối/lỗi

    # Cảnh báo Telegram (tuỳ chọn). Thiếu một trong hai thì chỉ ghi log + audit.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @model_validator(mode="after")
    def _check(self):
        if self.trading_mode not in ALLOWED_MODES:
            raise ValueError(f"TRADING_MODE phải thuộc {sorted(ALLOWED_MODES)}")
        if self.trading_mode == "LIVE":
            # LIVE cần cả cờ xác nhận tường minh; lúc đặt lệnh còn phải đạt ngưỡng paper/backtest và bật công tắc
            if self.live_trading_ack != LIVE_ACK:
                raise ValueError(f"LIVE cần LIVE_TRADING_ACK={LIVE_ACK}")
            if self.binance_env != "mainnet":
                raise ValueError("LIVE yêu cầu BINANCE_ENV=mainnet")
        if self.trading_mode == "BINANCE_TESTNET" and self.binance_env != "testnet":
            raise ValueError("TRADING_MODE=BINANCE_TESTNET yêu cầu BINANCE_ENV=testnet")
        # Nơi ĐẶT LỆNH (key) theo BINANCE_ENV; nguồn GIÁ theo MARKET_ENV.
        # auto: chỉ chế độ BINANCE_TESTNET dùng giá testnet (để lệnh thử khớp được); còn lại luôn dùng giá mainnet thật.
        self.trade_rest_url = TESTNET_REST if self.binance_env == "testnet" else self.binance_rest_url
        market_testnet = self.market_env == "testnet" or (
            self.market_env == "auto" and self.trading_mode == "BINANCE_TESTNET")
        if market_testnet:
            self.binance_rest_url, self.binance_ws_url = TESTNET_REST, TESTNET_WS
        return self

    @property
    def tf_list(self) -> list[str]:
        return [t.strip() for t in self.timeframes.split(",") if t.strip()]


settings = Settings()
