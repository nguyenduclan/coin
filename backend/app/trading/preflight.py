"""Kiểm tra trước khi bật giao dịch thật. Mục severity="block" không đạt thì KHÔNG được bật công tắc LIVE.

Chỉ ĐỌC từ sàn. "Đạt" chỉ nghĩa là cấu hình an toàn, không bảo đảm chiến lược có lãi.
"""
from .. import alerts, validation
from ..binance.account import AccountError, BinanceAccount
from ..core.config import LIVE_ACK, settings
from ..portfolio import service as portfolio
from . import reconcile


def _c(cid: str, label: str, passed: bool, note: str = "", severity: str = "block") -> dict:
    return {"id": cid, "label": label, "passed": bool(passed), "note": note, "severity": severity}


async def run(acct: BinanceAccount) -> dict:
    live = settings.trading_mode == "LIVE"
    items = [
        _c("mode", "Chế độ LIVE + BINANCE_ENV=mainnet + LIVE_TRADING_ACK",
           live and settings.binance_env == "mainnet" and settings.live_trading_ack == LIVE_ACK,
           f"hiện tại {settings.trading_mode}/{settings.binance_env}"),
        _c("keys", "Đã cấu hình BINANCE_API_KEY/SECRET", acct.enabled()),
        _c("limits", "Giới hạn vốn LIVE đều > 0",
           settings.live_max_order_usdt > 0 and settings.live_max_daily_loss_usdt > 0
           and settings.live_max_open_positions > 0,
           f"mỗi lệnh {settings.live_max_order_usdt:g} · lỗ ngày {settings.live_max_daily_loss_usdt:g} · "
           f"vị thế {settings.live_max_open_positions}"),
    ]
    info = None
    if acct.enabled():
        try:
            await acct.sync_time()
            skew = abs(acct.clock_skew_ms)
            items.append(_c("clock", f"Lệch đồng hồ ≤ {settings.preflight_max_clock_skew_ms}ms",
                            skew <= settings.preflight_max_clock_skew_ms, f"{skew}ms"))
            info = await acct.account()
            portfolio.apply_account_info(info)
            items.append(_c("can_trade", "API key có quyền giao dịch Spot", info.get("canTrade", False)))
        except AccountError as e:
            items.append(_c("account", "Đọc được tài khoản Binance", False, str(e)))
        if settings.binance_env == "mainnet":
            try:
                r = await acct.api_restrictions()
                items += [
                    _c("no_withdraw", "Key TẮT quyền rút tiền", not r.get("enableWithdrawals", True)),
                    _c("ip_restrict", "Key giới hạn theo IP (IP VPS)", r.get("ipRestrictions", r.get("ipRestrict", False))),
                    _c("least_priv", "Key không bật margin/futures/chuyển nội bộ",
                       not any(r.get(k) for k in ("enableMargin", "enableFutures", "enableInternalTransfer",
                                                  "permitsUniversalTransfer")),
                       severity="warn"),
                ]
            except AccountError as e:
                items.append(_c("api_restrictions", "Đọc được quyền của API key", False, str(e)))
        else:
            items.append(_c("api_restrictions", "Kiểm tra quyền key (chỉ mainnet)", True, "bỏ qua trên testnet",
                            severity="warn"))
    if info is not None:
        usdt = portfolio.account_state["balances"].get("USDT", 0.0)
        items.append(_c("balance", "Số dư USDT ≥ giới hạn mỗi lệnh", usdt >= settings.live_max_order_usdt,
                        f"{usdt:.2f} USDT"))
    ready = await validation.readiness()
    items.append(_c("readiness", "Đạt ngưỡng paper trading/backtest", ready["all_passed"],
                    f"{sum(i['passed'] for i in ready['criteria'])}/{len(ready['criteria'])} tiêu chí"))
    blocking = [i for i in reconcile.state["issues"] if i["severity"] == "block"]
    items.append(_c("reconcile", "Đối soát với sàn không có vấn đề nghiêm trọng", not blocking and
                    reconcile.state["at"] is not None,
                    "; ".join(i["text"] for i in blocking) or ("chưa chạy đối soát" if reconcile.state["at"] is None else "")))
    items.append(_c("alerts", "Đã cấu hình cảnh báo Telegram", alerts.configured(), severity="warn"))
    return {"items": items, "all_passed": all(i["passed"] for i in items if i["severity"] == "block")}
