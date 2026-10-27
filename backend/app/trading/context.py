"""Chọn nguồn danh mục theo chế độ: PAPER_TRADING dùng sổ giả lập, các chế độ khác dùng Binance (chỉ đọc)."""
from ..core.config import settings


def provider():
    if settings.trading_mode == "PAPER_TRADING":
        from ..paper import service
        return service
    from ..portfolio import service
    return service
