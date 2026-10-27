"""Ngưỡng chấp nhận ĐƯỢC ĐỊNH TRƯỚC (cố định trong code, không chỉnh theo kết quả).

Đạt hết chỉ là điều kiện CẦN để cân nhắc bước tiếp theo, không bảo đảm lợi nhuận.
Muốn đổi ngưỡng phải sửa code và commit, tránh chỉnh ngưỡng cho vừa kết quả.
"""
import json
import time

from sqlalchemy import select

from .core.db import SessionLocal
from .models import BacktestRun, PaperAccount
from .paper import service as paper

THRESHOLDS = {
    "paper_days": 30, "paper_trades": 30, "paper_profit_factor": 1.3, "paper_max_dd_pct": 10.0,
    "bt_oos_trades": 30, "bt_oos_profit_factor": 1.2,
}


def criterion(cid: str, label: str, value, passed: bool, note: str = "") -> dict:
    return {"id": cid, "label": label, "value": value, "passed": bool(passed), "note": note}


def evaluate(paper_days: float, perf: dict, paper_dd: float | None, oos: dict | None) -> list[dict]:
    T = THRESHOLDS
    pf, n = perf.get("profit_factor"), perf.get("closed_trades", 0)
    ex = perf.get("expectancy")
    out = [
        criterion("paper_days", f"Paper trading đủ {T['paper_days']} ngày", round(paper_days, 1),
                  paper_days >= T["paper_days"]),
        criterion("paper_trades", f"Ít nhất {T['paper_trades']} lệnh paper đã đóng", n, n >= T["paper_trades"]),
        criterion("paper_pf", f"Paper profit factor ≥ {T['paper_profit_factor']}", pf,
                  pf is not None and pf >= T["paper_profit_factor"]),
        criterion("paper_expectancy", "Paper expectancy > 0 (sau phí và trượt giá)", ex,
                  ex is not None and ex > 0),
        criterion("paper_dd", f"Paper max drawdown ≤ {T['paper_max_dd_pct']}%", paper_dd,
                  paper_dd is not None and paper_dd <= T["paper_max_dd_pct"]),
    ]
    if oos is None:
        out += [criterion("bt_trades", "Backtest out-of-sample có kết quả", None, False, "Chưa chạy backtest"),
                criterion("bt_pf", "Backtest OOS profit factor", None, False, "Chưa chạy backtest")]
    else:
        opf = oos.get("profit_factor")
        out += [criterion("bt_trades", f"Backtest OOS ≥ {T['bt_oos_trades']} lệnh", oos.get("trades", 0),
                          oos.get("trades", 0) >= T["bt_oos_trades"]),
                criterion("bt_pf", f"Backtest OOS profit factor ≥ {T['bt_oos_profit_factor']}", opf,
                          opf is not None and opf >= T["bt_oos_profit_factor"])]
    return out


async def readiness() -> dict:
    async with SessionLocal() as s:
        acc = await s.get(PaperAccount, 1)
        run = await s.scalar(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(1))
    days = (time.time() - acc.started_at.timestamp()) / 86400 if acc else 0.0
    summ = await paper.summary()
    perf = await paper.performance_report()
    oos = None
    if run:
        oos = json.loads(run.result).get("combined", {}).get("segments", {}).get("out_of_sample", {})
    items = evaluate(days, perf, summ["drawdown"]["max_drawdown_pct"] if acc else None, oos)
    return {"criteria": items, "all_passed": all(i["passed"] for i in items), "thresholds": THRESHOLDS,
            "disclaimer": "Đạt toàn bộ chỉ là điều kiện cần để cân nhắc dùng vốn thật rất nhỏ. "
                          "Không bảo đảm lợi nhuận; mẫu nhỏ thì chỉ số không có ý nghĩa thống kê.",
            "backtest_run_id": run.id if run else None}
