"""Cảnh báo ra ngoài (Telegram). Không bao giờ làm hỏng luồng gọi: lỗi gửi chỉ được ghi log.

Token nằm trong URL của Telegram nên KHÔNG đưa URL/ngoại lệ gốc vào log.
"""
import logging

import httpx

from .core.config import settings

log = logging.getLogger("coin.alerts")

# Hành động audit nào thì được đẩy ra Telegram (xem executor.audit)
ALERT_ACTIONS = {
    "trading_disabled", "kill_switch", "proposal_unknown_needs_review", "proposal_failed",
    "proposal_filled", "proposal_partial_filled", "protection_failed", "protection_missing",
    "reconcile_issue", "daily_loss_limit", "position_closed",
    "auto_enabled", "auto_disabled", "auto_buy", "auto_exit", "auto_failed",
}


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


async def notify(text: str, client: httpx.AsyncClient | None = None) -> bool:
    """True nếu đã gửi được. Chưa cấu hình hoặc lỗi -> False, không ném."""
    log.warning("ALERT %s", text)
    if not configured():
        return False
    own = client is None
    client = client or httpx.AsyncClient(timeout=5.0)
    try:
        r = await client.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                              json={"chat_id": settings.telegram_chat_id,
                                    "text": f"[coin {settings.trading_mode}] {text}"[:3500]})
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.warning("telegram send failed: %s", type(e).__name__)
        return False
    finally:
        if own:
            await client.aclose()
