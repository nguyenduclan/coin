"use client";
import { useEffect, useState } from "react";
import { fmt, ts, useApp } from "../Shell";

export default function History() {
  const { api } = useApp();
  const [h, setH] = useState<any>(null);
  const [err, setErr] = useState("");
  const [orders, setOrders] = useState<any[]>([]);
  useEffect(() => { api("/history").then(setH).catch((e) => setErr(e.message)); api("/orders").then(setOrders).catch(() => {}); }, [api]);
  if (!h) return <div className="mute">Đang tải… <span className="bad">{err}</span></div>;
  return (
    <>
      <h1>Đề xuất</h1>
      <div className="card">
        <table>
          <thead><tr><th>Thời điểm</th><th>Mã</th><th>Hướng</th><th>Trạng thái</th><th>Giá vào</th><th>Stop</th><th>TP</th><th>Giá trị</th><th>Rủi ro</th><th>R:R</th><th>Lý do</th></tr></thead>
          <tbody>
            {h.proposals.map((p: any) => (
              <tr key={p.id}><td>{ts(p.created_at)}</td><td>{p.symbol}</td><td>{p.side}</td><td>{p.status}</td>
                <td>{fmt(p.entry, 6)}</td><td>{fmt(p.stop, 6)}</td><td>{fmt(p.take_profit, 6)}</td><td>{fmt(p.notional)}</td>
                <td>{fmt(p.risk_usdt)}</td><td>{fmt(p.rr_after_costs)}</td><td className="mute">{p.exit_reason ?? p.strategy}{p.result?.error && <div className="bad">{p.result.error}</div>}{p.result?.protection && <div>Bảo vệ: {p.result.protection.status}</div>}</td></tr>
            ))}
            {!h.proposals.length && <tr><td colSpan={11} className="mute">Chưa có đề xuất nào.</td></tr>}
          </tbody>
        </table>
        <div className="mute">Trạng thái: PROPOSED chờ · REJECTED bạn từ chối · EXPIRED hết hạn · FAILED_PRECHECK bị chặn trước khi gửi · REJECTED_BY_EXCHANGE sàn từ chối · FILLED / PARTIAL_FILLED / NOT_FILLED kết quả khớp.</div>
      </div>
      <h1>Lệnh gửi lên sàn</h1>
      <div className="card">
        <table>
          <thead><tr><th>Thời gian</th><th>Mã</th><th>Loại</th><th>Hướng</th><th>SL</th><th>Giá dự kiến</th><th>Giá khớp TB</th><th>Đã khớp</th><th>Phí</th><th>Trạng thái</th><th>Môi trường</th></tr></thead>
          <tbody>
            {orders.map((o: any) => (
              <tr key={o.id}><td>{ts(o.created_at)}</td><td>{o.symbol}</td><td>{o.kind}</td><td>{o.side}</td><td>{fmt(o.qty, 8)}</td>
                <td>{fmt(o.expected_price, 6)}</td><td>{fmt(o.avg_price, 6)}</td><td>{fmt(o.executed_qty, 8)}</td><td>{fmt(o.fee_usdt, 6)}</td>
                <td>{o.status}{o.error && <div className="bad">{o.error}</div>}</td><td>{o.env}</td></tr>
            ))}
            {!orders.length && <tr><td colSpan={11} className="mute">Chưa có lệnh nào gửi lên sàn.</td></tr>}
          </tbody>
        </table>
      </div>
      <h1>Lệnh đã khớp</h1>
      <div className="card">
        <table>
          <thead><tr><th>Thời gian</th><th>Mã</th><th>Hướng</th><th>SL</th><th>Giá khớp</th><th>Phí (USDT)</th><th>Tài sản phí</th><th>PnL</th><th>Lý do</th></tr></thead>
          <tbody>
            {h.trades.map((t: any) => (
              <tr key={t.id}><td>{ts(t.time)}</td><td>{t.symbol}</td><td>{t.side}</td><td>{fmt(t.qty, 8)}</td><td>{fmt(t.price, 6)}</td>
                <td>{fmt(t.fee_usdt, 6)}</td><td>{t.fee_asset}{!t.fee_converted && <span className="stale"> (chưa quy đổi)</span>}</td><td>{fmt(t.pnl)}</td><td className="mute">{t.reason ?? ""}</td></tr>
            ))}
            {!h.trades.length && <tr><td colSpan={9} className="mute">Chưa có lệnh khớp (hoặc chưa kết nối Binance).</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
