"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { fmt, pnlCls, ts, useApp } from "./Shell";

type Item = { symbol: string; bid: number | null; ask: number | null; last: number | null; change_pct_24h: number | null;
  data: { state: string; reason: string; age_s: number | null } };
type Sig = { state: string; at: string; trends: Record<string, string>; entry?: number; stop?: number; take_profit?: number; notional?: number; side?: string };

const TFS = ["5m", "15m", "1h", "4h", "1d"];
const arrow = (t?: string) => (t === "UP" ? "▲" : t === "DOWN" ? "▼" : t === "SIDEWAYS" ? "■" : "·");

export default function Dashboard() {
  const { api, token } = useApp();
  const [items, setItems] = useState<Item[]>([]);
  const [wsOk, setWsOk] = useState(false);
  const [sigs, setSigs] = useState<Record<string, Sig>>({});
  const [pf, setPf] = useState<any>(null);
  const [err, setErr] = useState("");
  const [mode, setMode] = useState<any>(null);
  const [tr, setTr] = useState<any>(null);

  useEffect(() => { fetch("/api/mode").then((r) => r.json()).then(setMode).catch(() => {}); }, []);

  useEffect(() => {
    const es = new EventSource(`/api/stream?token=${encodeURIComponent(token)}`);
    es.onmessage = (e) => { const d = JSON.parse(e.data); setItems(d.items); setWsOk(d.ws_connected); };
    es.onerror = () => setWsOk(false);
    return () => es.close();
  }, [token]);

  useEffect(() => {
    const load = () => {
      api("/signals").then(setSigs).catch((e) => setErr(e.message));
      api("/portfolio").then(setPf).catch((e) => setErr(e.message));
      api("/trading/status").then(setTr).catch(() => {});
    };
    load();
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [api]);

  return (
    <>
      <div className="banner">Chế độ {mode?.trading_mode ?? "…"}{mode?.binance_env === "testnet" ? " (Binance testnet)" : ""} — WebSocket: {wsOk ? "đã kết nối" : "mất kết nối"} <span className="bad">{err}</span></div>
      {tr?.enabled && <div className="banner" style={{ background: "#4a1520", color: "#f6465d" }}>GIAO DỊCH ĐANG BẬT ({tr.mode}, {tr.env}): xác nhận đề xuất sẽ gửi lệnh lên sàn.</div>}
      {pf && (
        <div className="grid">
          <Stat label={`Tổng tài sản (${pf.source === "binance" ? "Binance" : "nhập tay"})`} v={fmt(pf.equity)} />
          <Stat label="USDT" v={fmt(pf.usdt_balance)} />
          <Stat label="Vốn đang dùng" v={fmt(pf.capital_in_use)} />
          <Stat label="PnL chưa chốt" v={fmt(pf.unrealized_pnl)} cls={pnlCls(pf.unrealized_pnl)} />
          <Stat label="PnL đã chốt" v={fmt(pf.realized_pnl)} cls={pnlCls(pf.realized_pnl)} />
          <Stat label="Phí" v={fmt(pf.fees)} />
          <Stat label="Drawdown hiện tại" v={`${fmt(pf.drawdown.current_drawdown_pct)}%`} />
          <Stat label="Vị thế mở" v={String(pf.open_positions)} />
        </div>
      )}
      {pf?.warnings?.map((w: string) => <div key={w} className="banner">{w}</div>)}
      <h1>Watchlist</h1>
      <div className="card">
        <table>
          <thead><tr><th>Mã</th><th>Giá</th><th>24h</th><th>{TFS.join(" ")}</th><th>Tín hiệu</th><th>Vào</th><th>Stop</th><th>TP</th><th>Vốn (USDT)</th><th>Phân tích</th><th>Dữ liệu</th></tr></thead>
          <tbody>
            {items.map((i) => {
              const s = sigs[i.symbol];
              return (
                <tr key={i.symbol}>
                  <td><Link href={`/coin/${i.symbol}`}>{i.symbol}</Link></td>
                  <td>{fmt(i.last, 6)}</td>
                  <td className={pnlCls(i.change_pct_24h)}>{i.change_pct_24h == null ? "—" : `${fmt(i.change_pct_24h)}%`}</td>
                  <td className="mute">{TFS.map((t) => arrow(s?.trends[t])).join(" ")}</td>
                  <td>{s ? s.state : "—"}</td>
                  <td>{fmt(s?.entry, 6)}</td><td>{fmt(s?.stop, 6)}</td><td>{fmt(s?.take_profit, 6)}</td>
                  <td>{fmt(s?.notional)}</td>
                  <td className="mute">{s ? ts(s.at) : "—"}</td>
                  <td className={i.data.state.toLowerCase()}>{i.data.state}{i.data.reason ? ` · ${i.data.reason}` : ""}</td>
                </tr>
              );
            })}
            {!items.length && <tr><td colSpan={11} className="mute">Chưa có mã nào đang kích hoạt. Thêm ở trang Cài đặt.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Stat({ label, v, cls = "" }: { label: string; v: string; cls?: string }) {
  return <div className="card stat"><div className="mute">{label}</div><div className={`big ${cls}`}>{v}</div></div>;
}
