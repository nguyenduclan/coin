"use client";
import { useCallback, useEffect, useState } from "react";
import { fmt, pnlCls, ts, useApp } from "../Shell";

export default function Portfolio() {
  const { api } = useApp();
  const [pf, setPf] = useState<any>(null);
  const [perf, setPerf] = useState<any>(null);
  const [edit, setEdit] = useState<Record<string, { stop: string; tp: string }>>({});
  const [err, setErr] = useState("");
  const [tr, setTr] = useState<any>(null);
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api("/portfolio").then(setPf).catch((e) => setErr(e.message));
    api("/performance").then(setPerf).catch(() => {});
    api("/trading/status").then(setTr).catch(() => {});
  }, [api]);
  useEffect(() => { load(); const id = setInterval(load, 15000); return () => clearInterval(id); }, [load]);

  const save = (sym: string) => {
    const e = edit[sym]; if (!e) return;
    const num = (s: string) => (s.trim() === "" ? null : Number(s));
    api(`/portfolio/positions/${sym}/protection`, { method: "PATCH", body: JSON.stringify({ stop: num(e.stop), take_profit: num(e.tp) }) })
      .then(() => { setErr(""); load(); }).catch((x) => setErr(x.message));
  };

  const protect = (sym: string) => {
    if (!confirm(`Đặt lệnh OCO (stop + take-profit) cho ${sym} trên sàn?`)) return;
    setMsg(""); api(`/portfolio/positions/${sym}/protect`, { method: "POST" })
      .then((r: any) => { setMsg(`${sym}: ${r.status}${r.error ? " — " + r.error : ""}`); load(); }).catch((e) => setErr(e.message));
  };
  const PROT: Record<string, [string, string]> = {
    NONE: ["Không có", "bad"], PLANNED_NOT_PLACED: ["Chỉ là kế hoạch, CHƯA đặt trên sàn", "bad"],
    SIMULATED: ["Mô phỏng (paper)", "stale"], VERIFIED: ["Đã xác minh trên Binance", "ok"],
    PLACE_FAILED: ["THẤT BẠI: vị thế KHÔNG được bảo vệ", "bad"], PLACE_UNVERIFIED: ["Sàn nhận nhưng chưa xác minh", "stale"],
    STALE_ON_EXCHANGE: ["Lệnh trên sàn còn giá cũ", "stale"],
  };
  if (!pf) return <div className="mute">Đang tải… <span className="bad">{err}</span></div>;
  return (
    <>
      <h1>Portfolio</h1>
      {pf.warnings.map((w: string) => <div key={w} className="banner">{w}</div>)}
      {pf.sync_error && <div className="banner">Lỗi đồng bộ Binance: {pf.sync_error}</div>}
      <div className="mute">Số dư cập nhật lúc {ts(pf.balances_at)} · rủi ro đang mở {fmt(pf.open_risk_usdt)} USDT · max drawdown {fmt(pf.drawdown.max_drawdown_pct)}%</div>
      <div className="card">
        <table>
          <thead><tr><th>Mã</th><th>SL</th><th>Giá vốn</th><th>Giá</th><th>Giá trị</th><th>PnL chưa chốt</th><th>Đã chốt</th><th>Phí</th><th>Stop</th><th>TP</th><th>Bảo vệ</th><th>Rủi ro còn lại</th></tr></thead>
          <tbody>
            {pf.positions.map((p: any) => (
              <tr key={p.symbol}>
                <td>{p.symbol}{p.balance_mismatch && <span className="stale" title="Số dư Binance khác sổ lệnh"> ⚠</span>}</td>
                <td>{fmt(p.qty, 8)}</td><td>{fmt(p.avg_cost, 6)}</td><td>{fmt(p.price, 6)}</td><td>{fmt(p.value)}</td>
                <td className={pnlCls(p.unrealized_pnl)}>{fmt(p.unrealized_pnl)} ({fmt(p.unrealized_pct)}%)</td>
                <td className={pnlCls(p.realized_pnl)}>{fmt(p.realized_pnl)}</td><td>{fmt(p.fees, 4)}</td>
                <td><input size={8} defaultValue={p.stop ?? ""} onChange={(e) => setEdit({ ...edit, [p.symbol]: { stop: e.target.value, tp: edit[p.symbol]?.tp ?? String(p.take_profit ?? "") } })} /></td>
                <td><input size={8} defaultValue={p.take_profit ?? ""} onChange={(e) => setEdit({ ...edit, [p.symbol]: { tp: e.target.value, stop: edit[p.symbol]?.stop ?? String(p.stop ?? "") } })} /><button onClick={() => save(p.symbol)}>Lưu</button></td>
                <td className={(PROT[p.protection_status] ?? ["", ""])[1]}>{(PROT[p.protection_status] ?? [p.protection_status])[0]}
                  {tr?.can_trade && p.stop && p.take_profit && p.protection_status !== "VERIFIED" && p.protection_status !== "SIMULATED" && <button onClick={() => protect(p.symbol)}>Đặt bảo vệ</button>}</td>
                <td>{fmt(p.remaining_risk_usdt)}<div className="mute">{p.risk_note}</div></td>
              </tr>
            ))}
            {!pf.positions.length && <tr><td colSpan={12} className="mute">Không có vị thế đang mở.</td></tr>}
          </tbody>
        </table>
        <span className="bad">{err}</span> <span className="mute">{msg}</span>
        {pf.unvalued_assets.length > 0 && <div className="mute">Tài sản chưa định giá (không có trong watchlist): {pf.unvalued_assets.join(", ")}</div>}
      </div>
      {perf && (
        <div className="card"><h2>Hiệu suất (lệnh đã chốt)</h2>
          <div className="grid">
            <div>Số lệnh: {perf.closed_trades}</div><div>PnL ròng: <span className={pnlCls(perf.net_pnl)}>{fmt(perf.net_pnl)}</span></div>
            <div>Win rate: {perf.win_rate == null ? "—" : `${fmt(perf.win_rate * 100, 1)}%`}</div>
            <div>Profit factor: {fmt(perf.profit_factor)}</div><div>Expectancy: {fmt(perf.expectancy)}</div>
          </div>
          <div className="mute">{perf.note}</div>
        </div>
      )}
    </>
  );
}
