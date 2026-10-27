"use client";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { fmt, ts, useApp } from "../../Shell";

const TFS = ["5m", "15m", "1h", "4h", "1d"];

export default function Coin() {
  const { symbol } = useParams<{ symbol: string }>();
  const { api } = useApp();
  const [tf, setTf] = useState("1h");
  const [d, setD] = useState<any>(null);
  const [news, setNews] = useState<any[]>([]);
  const [err, setErr] = useState("");
  const [live, setLive] = useState<any>(null);
  const [live2, setLive2] = useState<any>(null);
  const [outcome, setOutcome] = useState<any>(null);
  const box = useRef<HTMLDivElement>(null);
  const [amount, setAmount] = useState("10");
  const [entry, setEntry] = useState("");
  const [plan, setPlan] = useState<any>(null);
  const [planBusy, setPlanBusy] = useState(false);
  const [planErr, setPlanErr] = useState("");
  const [ack, setAck] = useState(false);
  const [proposed, setProposed] = useState("");
  const VERDICT: Record<string, [string, string, string]> = {
    BUY_NOW: ["CÓ THỂ MUA NGAY", "#0f3a2a", "#2ebd85"], WAIT_PULLBACK: ["CHỜ GIÁ HỒI VỀ", "#3a2f0b", "#f0b90b"],
    NO_TRADE: ["KHÔNG NÊN MUA LÚC NÀY", "#4a1520", "#f6465d"], DATA_UNAVAILABLE: ["CHƯA CÓ ĐỦ DỮ LIỆU", "#222", "#8b93a1"],
  };
  const body = (extra: object = {}) => JSON.stringify({ symbol, amount: Number(amount), entry_price: entry.trim() ? Number(entry) : null, ...extra });
  const runPlan = async () => {
    setPlanBusy(true); setPlanErr(""); setProposed(""); setAck(false);
    try { const p = await api("/plan", { method: "POST", body: body() }); if (p.verdict === "ERROR") setPlanErr(p.error); setPlan(p.verdict === "ERROR" ? null : p); }
    catch (e: any) { setPlanErr(e.message); }
    setPlanBusy(false);
  };
  const propose = async () => {
    setPlanErr(""); setProposed("");
    try {
      const r = await api("/plan/propose", { method: "POST", body: body({ acknowledge: ack }) });
      setProposed(r.paper_fill ? (r.paper_fill.ok ? `Đã khớp giả lập (paper) tại ${fmt(r.paper_fill.price, 10)}. Xem ở trang Portfolio.` : `Paper từ chối: ${r.paper_fill.reason}`)
        : "Đã tạo đề xuất. Kéo xuống khung “Đề xuất” bên dưới để xem và bấm Xác nhận.");
      api(`/proposals?status=PROPOSED&symbol=${symbol}&limit=1`).then((l: any[]) => setLive(l[0] ?? null)).catch(() => {});
    } catch (e: any) { setPlanErr(e.message); }
  };

  useEffect(() => {
    const load = () => {
      api(`/coin/${symbol}`).then(setD).catch((e) => setErr(e.message));
      api(`/proposals?status=PROPOSED&symbol=${symbol}&limit=1`).then((l: any[]) => setLive(l[0] ?? null)).catch(() => {});
      api("/trading/status").then(setLive2).catch(() => {});
    };
    load();
    api(`/news?asset=${String(symbol).replace("USDT", "")}&limit=15`).then(setNews).catch(() => {});
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, [api, symbol]);

  useEffect(() => {
    let chart: any; let alive = true;
    (async () => {
      const [{ createChart }, c, ind] = await Promise.all([
        import("lightweight-charts"),
        api(`/candles/${symbol}/${tf}?limit=300`),
        api(`/indicators/${symbol}/${tf}?limit=300`),
      ]);
      if (!alive || !box.current) return;
      chart = createChart(box.current, { height: 380, layout: { background: { color: "#181b21" }, textColor: "#8b93a1" },
        grid: { vertLines: { color: "#22262e" }, horzLines: { color: "#22262e" } }, timeScale: { timeVisible: true } });
      const cs = chart.addCandlestickSeries({ upColor: "#2ebd85", downColor: "#f6465d", borderVisible: false, wickUpColor: "#2ebd85", wickDownColor: "#f6465d" });
      cs.setData(c.map((x: any) => ({ time: x.t / 1000, open: x.o, high: x.h, low: x.l, close: x.c })));
      const line = (data: any[], color: string) => {
        const s = chart.addLineSeries({ color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
        s.setData(data.map((x) => ({ time: x.t / 1000, value: x.v })));
      };
      line(ind.ema20, "#f0b90b"); line(ind.ema50, "#5b8def");
      chart.timeScale().fitContent();
    })().catch((e) => setErr(String(e.message)));
    return () => { alive = false; chart?.remove(); };
  }, [api, symbol, tf]);

  const decide = async (kind: "confirm" | "reject") => {
    if (!live) return;
    if (kind === "confirm") {
      const msg = `XÁC NHẬN ${live.side} ${symbol}\n\nSố lượng: ${live.qty}\nGiá tham chiếu: ${live.ref_price}\n` +
        (live.stop ? `Stop: ${live.stop}  TP: ${live.take_profit}\n` : "") +
        `Giá trị: ~${fmt(live.notional)} USDT\nChế độ: ${live2?.mode} (${live2?.env})\n\nLệnh sẽ được gửi lên sàn. Tiếp tục?`;
      if (!confirm(msg)) return;
    }
    setErr(""); setOutcome(null);
    try {
      const r = kind === "confirm"
        ? await api(`/proposals/${live.id}/confirm`, { method: "POST", body: JSON.stringify({ confirm_token: live.confirm_token }) })
        : await api(`/proposals/${live.id}/reject`, { method: "POST" });
      setOutcome(r);
    } catch (e: any) { setErr(e.message); }
    api(`/proposals?status=PROPOSED&symbol=${symbol}&limit=1`).then((l: any[]) => setLive(l[0] ?? null)).catch(() => {});
  };

  const r = d?.report;
  const pr = r?.proposal;
  return (
    <>
      <h1>{symbol} {d?.market && <span className="mute">· {fmt(d.market.last, 6)} · {d.market.data.state}</span>}</h1>
      <span className="bad">{err}</span>
      <div className="card">
        <div className="row">{TFS.map((t) => <button key={t} disabled={t === tf} onClick={() => setTf(t)}>{t}</button>)}
          <span className="mute">EMA20 vàng · EMA50 xanh (chỉ nến đã đóng)</span></div>
        <div ref={box} style={{ marginTop: 8 }} />
      </div>


      <div className="card">
        <h2>Kế hoạch giao dịch</h2>
        <div className="row">
          <label>Số tiền (USDT) <input type="number" min={1} value={amount} onChange={(e) => setAmount(e.target.value)} style={{ width: 90 }} /></label>
          <label>Giá mua dự kiến <input type="text" placeholder={d?.market?.ask ? String(d.market.ask) : "giá hiện tại"} value={entry} onChange={(e) => setEntry(e.target.value)} style={{ width: 150 }} /></label>
          <button disabled={planBusy} onClick={runPlan}>{planBusy ? "Đang phân tích…" : "Phân tích"}</button>
        </div>
        <div className="mute">Để trống giá mua dự kiến để dùng giá thị trường hiện tại. Hệ thống tính số liệu từ nến đã đóng; AI chỉ giải thích bằng lời.</div>
        {planErr && <div className="banner">{planErr}</div>}
        {plan && (
          <div style={{ marginTop: 10 }}>
            <div className="banner" style={{ background: VERDICT[plan.verdict]?.[1] ?? "#222", color: VERDICT[plan.verdict]?.[2] ?? "#fff" }}>
              <b>{VERDICT[plan.verdict]?.[0] ?? plan.verdict}</b>
              {plan.wait_zone && <> — vùng chờ mua: {fmt(plan.wait_zone.low, 10)} – {fmt(plan.wait_zone.high, 10)}</>}
              <ul style={{ margin: "4px 0 0" }}>{(plan.reasons || []).map((x: string) => <li key={x}>{x}</li>)}</ul>
            </div>
            {plan.plan && (
              <table style={{ marginTop: 8 }}><tbody>
                <tr><td>Giá vào</td><td>{fmt(plan.plan.entry, 10)}</td></tr>
                <tr><td>Số coin nhận được</td><td>{fmt(plan.plan.qty, 8)} (≈ {fmt(plan.plan.notional)} USDT)</td></tr>
                <tr><td>Bán cắt lỗ nếu giá về</td><td className="bad">{fmt(plan.plan.stop, 10)} ({fmt(-plan.plan.stop_distance_pct, 2)}%)</td></tr>
                <tr><td>Lỗ tối đa theo kế hoạch</td><td className="bad">{fmt(plan.plan.risk_usdt, 4)} USDT ({fmt(plan.plan.risk_pct_of_amount, 2)}% số tiền)</td></tr>
                <tr><td>Chốt lời 1 tại</td><td className="ok">{fmt(plan.plan.take_profit, 10)} (+{fmt(plan.plan.tp1_distance_pct, 2)}%) → lãi ròng {fmt(plan.plan.reward_usdt_tp1, 4)} USDT, R:R {fmt(plan.plan.rr_tp1)}</td></tr>
                <tr><td>Chốt lời 2 tại</td><td className="ok">{fmt(plan.plan.take_profit_2, 10)} → lãi ròng {fmt(plan.plan.reward_usdt_tp2, 4)} USDT, R:R {fmt(plan.plan.rr_tp2)}</td></tr>
                <tr><td>Phí khứ hồi ước tính</td><td>{fmt(plan.plan.fees_roundtrip_est, 4)} USDT</td></tr>
              </tbody></table>
            )}
            {plan.notes?.length > 0 && <div className="banner" style={{ marginTop: 8 }}>{plan.notes.map((x: string) => <div key={x}>• {x}</div>)}</div>}
            {plan.exit_rules && <><b>Khi nào bán</b><ul>{plan.exit_rules.map((x: any) => <li key={x.if}><b>{x.if}</b> → {x.do}</li>)}</ul></>}
            {plan.ai && <div className="card" style={{ background: "#14171c" }}><b>Diễn giải AI</b><p>{plan.ai.summary}</p>
              {plan.ai.why && <ul>{plan.ai.why.map((x: string) => <li key={x}>{x}</li>)}</ul>}
              {plan.ai.risks && <><b>Rủi ro</b><ul>{plan.ai.risks.map((x: string) => <li key={x}>{x}</li>)}</ul></>}
              {plan.ai.exit_advice && <p><b>Về việc thoát:</b> {plan.ai.exit_advice}</p>}</div>}
            {plan.ai_error && <div className="mute">AI: {plan.ai_error}</div>}
            {plan.limits && <div className="mute">{plan.limits.join(" · ")}</div>}
            {plan.can_propose && (
              <div style={{ marginTop: 8 }}>
                {plan.verdict !== "BUY_NOW" && <label className="bad"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> Tôi hiểu kết luận không phải “MUA NGAY” và vẫn muốn tạo đề xuất</label>}
                <div><button disabled={plan.verdict !== "BUY_NOW" && !ack} onClick={propose}>
                  {live2?.mode === "PAPER_TRADING" ? "Mua giả lập theo kế hoạch" : "Tạo đề xuất mua (chờ xác nhận)"}</button></div>
              </div>
            )}
            {plan.can_propose === false && <div className="mute">Chưa thể tạo đề xuất từ kế hoạch này (xem cảnh báo ở trên).</div>}
          </div>
        )}
        {proposed && <div className="banner" style={{ marginTop: 8 }}>{proposed}</div>}
      </div>

      {r?.position && (
        <div className="card">
          <h2>Vị thế đang giữ: {fmt(r.position.qty, 8)} @ giá vốn {fmt(r.position.avg_cost, 10)}</h2>
          <div>Stop: {fmt(r.position.stop, 10)} · TP: {fmt(r.position.take_profit, 10)} · Trạng thái: <b>{r.state}</b> <span className="mute">({r.state_reason})</span></div>
          {r.position.trailing
            ? <div className="banner" style={{ marginTop: 8 }}>Gợi ý dời stop lên <b>{fmt(r.position.trailing.suggested_stop, 10)}</b> (đang {fmt(r.position.trailing.current_stop, 10)}): {r.position.trailing.reason}. Sửa stop ở trang Portfolio.</div>
            : <div className="mute">Chưa có gợi ý dời stop (cần stop đã nhập và lãi từ 1R).</div>}
        </div>
      )}
      {r && (
        <div className="card">
          <h2>Trạng thái: {r.state}</h2>
          <div className="mute">{r.state_reason} · phân tích lúc {ts(r.generated_at)} · giá tham chiếu {fmt(r.ref_price, 6)} (nhận lúc {ts(r.price_received_at)})</div>
          <table style={{ marginTop: 8 }}>
            <thead><tr><th>Khung</th><th>Xu hướng</th><th>RSI14</th><th>MACD hist</th><th>ATR</th><th>Hỗ trợ</th><th>Kháng cự</th><th>KL/TB20</th></tr></thead>
            <tbody>
              {TFS.map((t) => { const s = r.timeframes[t] || {}; return (
                <tr key={t}><td>{t}</td><td>{s.trend}</td><td>{fmt(s.rsi14, 1)}</td><td>{fmt(s.macd_hist, 4)}</td><td>{fmt(s.atr14, 4)}</td>
                  <td>{(s.support || []).slice(0, 2).map((x: number) => fmt(x, 4)).join(", ") || "—"}</td>
                  <td>{(s.resistance || []).slice(0, 2).map((x: number) => fmt(x, 4)).join(", ") || "—"}</td><td>{fmt(s.volume_ratio)}</td></tr>); })}
            </tbody>
          </table>
          {r.no_setup_reasons && <div style={{ marginTop: 8 }}><b>Chưa có điểm vào vì:</b><ul>{r.no_setup_reasons.map((x: string) => <li key={x}>{x}</li>)}</ul></div>}
        </div>
      )}

      {pr && (
        <div className="card">
          <h2>Đề xuất {pr.side} <span className="mute">(ID {r.proposal_id?.slice(0, 8)}, hết hạn sau ~5 phút)</span></h2>
          <table><tbody>
            <tr><td>Giá vào</td><td>{fmt(pr.entry, 6)}</td></tr>
            {pr.stop != null && <tr><td>Stop-loss</td><td>{fmt(pr.stop, 6)}</td></tr>}
            {pr.take_profit != null && <tr><td>Take-profit</td><td>{fmt(pr.take_profit, 6)}</td></tr>}
            <tr><td>Số lượng</td><td>{fmt(pr.qty, 8)}</td></tr>
            <tr><td>Giá trị lệnh (USDT)</td><td>{fmt(pr.notional)}</td></tr>
            {pr.risk_usdt != null && <tr><td>Rủi ro kế hoạch</td><td>{fmt(pr.risk_usdt)} USDT ({fmt(pr.risk_pct_equity, 3)}% vốn)</td></tr>}
            {pr.rr_after_costs != null && <tr><td>R:R sau phí</td><td>{fmt(pr.rr_after_costs)}</td></tr>}
            {pr.est_fees_roundtrip != null && <tr><td>Phí khứ hồi ước tính</td><td>{fmt(pr.est_fees_roundtrip)} USDT</td></tr>}
            {pr.exit_reason && <tr><td>Lý do thoát</td><td>{pr.exit_reason}</td></tr>}
          </tbody></table>
          <b>Điều kiện xác nhận</b><ul>{pr.confirm_conditions?.map((x: string) => <li key={x}>{x}</li>)}</ul>
          <b>Điều kiện vô hiệu</b><ul>{pr.invalidation?.map((x: string) => <li key={x}>{x}</li>)}</ul>
          <b>Giới hạn</b><ul>{pr.limits?.map((x: string) => <li key={x}>{x}</li>)}</ul>
          {live && live.id === r.proposal_id && (
            <div style={{ marginTop: 8 }}>
              {live.invalid_reasons?.length > 0 && <div className="banner">Đề xuất không còn hợp lệ: {live.invalid_reasons.join("; ")}</div>}
              {live2 && !live2.can_trade && <div className="banner">Không thể gửi lệnh: {live2.blocked_reason}</div>}
              <button disabled={!live2?.can_trade || live.invalid_reasons?.length > 0} onClick={() => decide("confirm")}>
                {live.side === "BUY" ? "Xác nhận MUA" : "Xác nhận BÁN"}</button>
              <button onClick={() => decide("reject")}>Từ chối</button>
            </div>
          )}
          {live2?.mode === "PAPER_TRADING" && <div className="mute">Paper trading: đề xuất được khớp giả lập tự động, không cần xác nhận.</div>}
        </div>
      )}
      {outcome && <div className="card"><h2>Kết quả: {outcome.status}</h2>
        <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(outcome, null, 2)}</pre>
        {outcome.protection && outcome.protection.status !== "VERIFIED" && <div className="banner">Vị thế CHƯA được bảo vệ trên sàn: {outcome.protection.error}. Xem trang Portfolio.</div>}
        {outcome.warning && <div className="banner">{outcome.warning}</div>}</div>}
      {err && <div className="banner">{err}</div>}

      {r?.ai && (
        <div className="card"><h2>Diễn giải AI ({r.ai_provider})</h2>
          <p>{r.ai.summary}</p>
          {r.ai.scenarios && <ul><li><b>Tăng:</b> {r.ai.scenarios.bullish}</li><li><b>Giảm:</b> {r.ai.scenarios.bearish}</li><li><b>Đi ngang:</b> {r.ai.scenarios.sideways}</li></ul>}
          {r.ai.risks && <><b>Rủi ro</b><ul>{r.ai.risks.map((x: string) => <li key={x}>{x}</li>)}</ul></>}
          <div className="mute">AI chỉ diễn giải số liệu đã tính; không tạo giá vào lệnh.</div></div>
      )}
      {r && !r.ai && <div className="card mute">AI: {r.ai_error || "chưa bật (AI_PROVIDER=none)"}</div>}

      <div className="card"><h2>Tin liên quan</h2>
        {news.length ? <ul>{news.map((n) => <li key={n.url}><a href={n.url} target="_blank" rel="noreferrer">{n.title}</a> <span className="mute">· {n.source} · {ts(n.published_at)} · chưa xác minh</span></li>)}</ul>
          : <span className="mute">Không có tin liên quan từ các nguồn đã cấu hình.</span>}
      </div>

      <div className="card"><h2>Lịch sử tín hiệu</h2>
        <table><thead><tr><th>Thời điểm</th><th>Trạng thái</th><th>Giá tham chiếu</th></tr></thead>
          <tbody>{d?.signal_history.slice(0, 30).map((h: any, i: number) => <tr key={i}><td>{ts(h.at)}</td><td>{h.state}</td><td>{fmt(h.ref_price, 6)}</td></tr>)}</tbody></table>
      </div>
    </>
  );
}
