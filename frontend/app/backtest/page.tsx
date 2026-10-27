"use client";
import { useCallback, useEffect, useState } from "react";
import { fmt, pnlCls, ts, useApp } from "../Shell";

const pct = (n: number | null | undefined) => (n == null ? "—" : `${fmt(n, 1)}%`);

const HEAD = ["", "Lệnh", "PnL ròng", "Phí", "Win rate", "Profit factor", "Expectancy", "R TB", "Max DD"];

function cells(m: any) {
  return [m.trades, <span className={pnlCls(m.net_pnl)}>{fmt(m.net_pnl)}</span>, fmt(m.fees),
    m.win_rate == null ? "—" : pct(m.win_rate * 100), fmt(m.profit_factor), fmt(m.expectancy), fmt(m.avg_r),
    m.max_drawdown_pct == null ? "—" : pct(m.max_drawdown_pct)];
}

function Table({ rows }: { rows: [string, any][] }) {
  return (
    <table>
      <thead><tr>{HEAD.map((h) => <th key={h}>{h}</th>)}</tr></thead>
      <tbody>{rows.map(([k, m]) => <tr key={k}><td>{k}</td>{cells(m).map((c, i) => <td key={i}>{c}</td>)}</tr>)}</tbody>
    </table>
  );
}

export default function Backtest() {
  const { api } = useApp();
  const [syms, setSyms] = useState<string[]>([]);
  const [sel, setSel] = useState<string[]>([]);
  const [days, setDays] = useState(180);
  const [equity, setEquity] = useState(1000);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [runs, setRuns] = useState<any[]>([]);
  const [res, setRes] = useState<any>(null);
  const [ready, setReady] = useState<any>(null);

  const loadRuns = useCallback(() => api("/backtest/runs").then(setRuns).catch(() => {}), [api]);
  useEffect(() => {
    api("/symbols").then((s: any[]) => { setSyms(s.map((x) => x.symbol)); setSel(s.map((x) => x.symbol)); }).catch(() => {});
    api("/readiness").then(setReady).catch(() => {});
    loadRuns();
  }, [api, loadRuns]);

  const poll = async (jobId: string, label: string) => {
    for (;;) {
      const j = await api(`/backtest/jobs/${jobId}`);
      setMsg(`${label}: ${j.message}`);
      if (j.status !== "running") {
        if (j.status === "error") throw new Error(j.message);
        return j;
      }
      await new Promise((r) => setTimeout(r, 2000));
    }
  };
  const start = (path: string, label: string, body: any, after?: (j: any) => void) => {
    setErr(""); setMsg(`${label}…`);
    api(path, { method: "POST", body: JSON.stringify(body) })
      .then((r: any) => poll(r.job_id, label)).then((j) => { after?.(j); setMsg(`${label}: xong`); })
      .catch((e) => { setErr(e.message); setMsg(""); });
  };
  const open = (id: string) => api(`/backtest/runs/${id}`).then((r: any) => setRes(r)).catch((e) => setErr(e.message));

  const c = res?.result;
  return (
    <>
      <h1>Backtest & điều kiện chuyển bước</h1>
      {ready && (
        <div className="card">
          <h2>Ngưỡng chấp nhận (cố định trước, không chỉnh theo kết quả)</h2>
          <table><tbody>
            {ready.criteria.map((i: any) => (
              <tr key={i.id}><td>{i.label}</td><td>{i.value == null ? "—" : typeof i.value === "number" ? fmt(i.value) : String(i.value)}</td>
                <td className={i.passed ? "ok" : "bad"}>{i.passed ? "ĐẠT" : "CHƯA ĐẠT"}</td><td className="mute">{i.note}</td></tr>
            ))}
          </tbody></table>
          <div className="mute">{ready.disclaimer}</div>
        </div>
      )}
      <div className="card">
        <div className="row">
          {syms.map((s) => (
            <label key={s}><input type="checkbox" checked={sel.includes(s)}
              onChange={(e) => setSel(e.target.checked ? [...sel, s] : sel.filter((x) => x !== s))} /> {s}</label>
          ))}
          <label>Số ngày <input type="number" min={30} max={730} value={days} onChange={(e) => setDays(+e.target.value)} style={{ width: 70 }} /></label>
          <label>Vốn/mã <input type="number" value={equity} onChange={(e) => setEquity(+e.target.value)} style={{ width: 80 }} /></label>
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          <button onClick={() => start("/backtest/data", "Tải dữ liệu", { symbols: sel, days })}>1. Tải dữ liệu lịch sử</button>
          <button onClick={() => start("/backtest/run", "Backtest", { symbols: sel, days, equity }, (j) => { loadRuns(); open(j.result_id); })}>2. Chạy backtest</button>
          <span className="mute">{msg}</span><span className="bad">{err}</span>
        </div>
        <div className="mute">Dùng đúng các tham số rủi ro/phí/trượt giá đang cấu hình; không tối ưu tham số. Tải dữ liệu có thể mất vài chục giây mỗi mã.</div>
      </div>

      <div className="card"><h2>Các lần chạy</h2>
        {runs.map((r) => <div key={r.id}><a href="#" onClick={(e) => { e.preventDefault(); open(r.id); }}>{ts(r.created_at)}</a> · {r.symbols} · {r.days} ngày · vốn {r.equity}</div>)}
        {!runs.length && <span className="mute">Chưa có.</span>}
      </div>

      {c && (
        <>
          <div className="card">
            <h2>Tổng hợp (mỗi mã chạy độc lập)</h2>
            <Table rows={[["Tất cả", c.combined.metrics]]} />
            <div className="mute">{c.combined.note}</div>
            {c.combined.segments && <><h2 style={{ marginTop: 12 }}>Theo giai đoạn thời gian (50% / 25% / 25%)</h2>
              <Table rows={Object.entries(c.combined.segments) as [string, any][]} /></>}
            {c.combined.windows && <><h2 style={{ marginTop: 12 }}>4 cửa sổ liên tiếp (độ ổn định)</h2>
              <Table rows={c.combined.windows.map((w: any, i: number) => [`Cửa sổ ${i + 1}`, w])} /></>}
            <h2 style={{ marginTop: 12 }}>Theo trạng thái thị trường (xu hướng 1d lúc vào lệnh)</h2>
            <Table rows={Object.entries(c.combined.by_regime) as [string, any][]} />
          </div>
          {Object.entries(c.symbols).map(([s, v]: [string, any]) => (
            <div className="card" key={s}>
              <h2>{s}</h2>
              <div className="mute">Lợi nhuận {pct(v.return_pct)} · giữ nguyên (buy & hold) cùng kỳ {pct(v.benchmark_buy_hold_pct)} · {ts(v.period[0] / 1000)} → {ts(v.period[1] / 1000)}</div>
              <Table rows={[["Tổng", v.metrics], ...(Object.entries(v.segments || {}) as [string, any][])]} />
              <div className="mute">Bỏ qua: {Object.entries(v.skipped).map(([k, n]) => `${k}=${n}`).join(", ")}</div>
            </div>
          ))}
          {Object.keys(c.errors || {}).length > 0 && <div className="banner">Lỗi: {JSON.stringify(c.errors)}</div>}
          <div className="card"><h2>Giới hạn của backtest</h2><ul>{c.limits.map((x: string) => <li key={x}>{x}</li>)}</ul></div>
          <div className="card"><h2>Lệnh (500 gần nhất)</h2>
            <table><thead><tr><th>Mã</th><th>Vào</th><th>Giá vào</th><th>Giá ra</th><th>PnL</th><th>R</th><th>Lý do ra</th><th>Regime</th></tr></thead>
              <tbody>{c.trades.slice().reverse().map((t: any, i: number) => (
                <tr key={i}><td>{t.symbol}</td><td>{ts(t.entry_time / 1000)}</td><td>{fmt(t.entry, 4)}</td><td>{fmt(t.exit, 4)}</td>
                  <td className={pnlCls(t.pnl)}>{fmt(t.pnl)}</td><td>{fmt(t.r)}</td><td>{t.reason}</td><td>{t.regime}</td></tr>))}</tbody></table>
          </div>
        </>
      )}
    </>
  );
}
