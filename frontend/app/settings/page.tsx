"use client";
import { useCallback, useEffect, useState } from "react";
import { useApp } from "../Shell";

type Sym = { symbol: string; active: boolean };

export default function Settings() {
  const { api } = useApp();
  const [syms, setSyms] = useState<Sym[]>([]);
  const [input, setInput] = useState("");
  const [err, setErr] = useState("");
  const [ai, setAi] = useState<any>(null);
  const [tr, setTr] = useState<any>(null);
  const loadTr = useCallback(() => api("/trading/status").then(setTr).catch(() => {}), [api]);
  const [pf, setPf] = useState<any>(null);
  const [rc, setRc] = useState<any>(null);
  const runPreflight = () => api("/trading/preflight").then(setPf).catch((e) => setErr(e.message));
  const kill = (cancel: boolean) => {
    const msg = cancel
      ? "DỪNG KHẨN CẤP và HUỶ MỌI LỆNH MỞ, gồm cả lệnh bảo vệ (stop/TP) trên sàn. Vị thế sẽ không còn stop. Tiếp tục?"
      : "Dừng khẩn cấp: TẮT giao dịch ngay (không đụng tới lệnh bảo vệ đang chạy). Tiếp tục?";
    if (!confirm(msg)) return;
    api("/trading/kill", { method: "POST", body: JSON.stringify({ cancel_orders: cancel }) })
      .then(() => loadTr()).catch((e) => setErr(e.message));
  };
  const toggleAuto = (enabled: boolean) => {
    let confirmText = "";
    if (enabled) {
      confirmText = prompt(`Bật TỰ ĐỘNG mua/thoát (${tr.mode}, ${tr.env}). Hệ thống sẽ tự đặt lệnh khi có tín hiệu, không hỏi từng lệnh. Gõ ENABLE AUTO để xác nhận:`) || "";
      if (!confirmText) return;
    }
    api("/trading/auto", { method: "POST", body: JSON.stringify({ enabled, confirm: confirmText }) })
      .then((r) => { setErr(""); setTr(r); }).catch((e) => setErr(e.message));
  };
  const toggle = (enabled: boolean) => {
    let confirmText = "";
    if (enabled) {
      confirmText = prompt(`Bật gửi lệnh lên sàn (${tr.mode}, ${tr.env}). Gõ ENABLE TRADING để xác nhận:`) || "";
      if (!confirmText) return;
    }
    api("/trading/enabled", { method: "POST", body: JSON.stringify({ enabled, confirm: confirmText }) })
      .then(setTr).catch((e) => setErr(e.message));
  };

  const load = useCallback(() => api("/symbols").then(setSyms).catch((e) => setErr(e.message)), [api]);
  useEffect(() => { load(); loadTr(); api("/trading/reconcile").then(setRc).catch(() => {}); api("/ai/status").then(setAi).catch(() => {}); }, [api, load, loadTr]);

  const act = (p: Promise<unknown>) => p.then(() => { setErr(""); load(); }).catch((e) => setErr(e.message));

  return (
    <>
      <h1>Danh sách coin</h1>
      <div className="card">
        <div className="row">
          <input placeholder="VD: BTCUSDT" value={input} onChange={(e) => setInput(e.target.value.toUpperCase())} />
          <button onClick={() => act(api("/symbols", { method: "POST", body: JSON.stringify({ symbol: input }) }).then(() => setInput("")))}>Thêm</button>
          <span className="bad">{err}</span>
        </div>
        <table style={{ marginTop: 8 }}>
          <tbody>
            {syms.map((s) => (
              <tr key={s.symbol}>
                <td>{s.symbol}</td><td className="mute">{s.active ? "đang phân tích" : "tạm dừng"}</td>
                <td>
                  <button onClick={() => act(api(`/symbols/${s.symbol}`, { method: "PATCH", body: JSON.stringify({ active: !s.active }) }))}>{s.active ? "Tạm dừng" : "Tiếp tục"}</button>
                  <button onClick={() => confirm(`Xóa ${s.symbol}?`) && act(api(`/symbols/${s.symbol}`, { method: "DELETE" }))}>Xóa</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h1>Giao dịch thật</h1>
      <div className="card">
        {tr && <>
          Chế độ: <b>{tr.mode}</b> ({tr.env}) · công tắc: <b className={tr.enabled ? "bad" : "ok"}>{tr.enabled ? "ĐANG BẬT" : "TẮT"}</b>
          {tr.blocked_reason && <div className="mute">{tr.blocked_reason}</div>}
          <div className="mute">Lỗi sàn liên tiếp: {tr.fail_streak}/{tr.max_order_failures} (đạt ngưỡng sẽ tự tắt)</div>
          <div style={{ marginTop: 8 }}>
            {tr.enabled ? <button onClick={() => toggle(false)}>TẮT giao dịch</button>
              : <button disabled={!["BINANCE_TESTNET", "LIVE"].includes(tr.mode)} onClick={() => toggle(true)}>Bật giao dịch…</button>}
          </div>
          {tr.live_limits && <div className="mute">
            Giới hạn LIVE: {tr.live_limits.max_order_usdt} USDT/lệnh · lỗ ngày tối đa {tr.live_limits.max_daily_loss_usdt} USDT · {tr.live_limits.max_open_positions} vị thế
            {tr.daily_loss?.known ? ` · lỗ hôm nay: ${tr.daily_loss.loss.toFixed(2)} USDT` : " · chưa có mốc vốn đầu ngày"}
          </div>}
          <div style={{ marginTop: 8 }}>
            <button onClick={() => kill(false)}>DỪNG KHẨN CẤP</button>{" "}
            <button onClick={() => kill(true)}>Dừng + huỷ mọi lệnh (kể cả stop)</button>{" "}
            <button onClick={runPreflight}>Chạy preflight</button>
          </div>
          {pf && <table style={{ marginTop: 8 }}><tbody>
            {pf.items.map((i: any) => <tr key={i.id}>
              <td className={i.passed ? "ok" : i.severity === "block" ? "bad" : "mute"}>{i.passed ? "ĐẠT" : i.severity === "block" ? "CHẶN" : "CẢNH BÁO"}</td>
              <td>{i.label}</td><td className="mute">{i.note}</td></tr>)}
          </tbody></table>}
          {rc && rc.issues?.length > 0 && <div style={{ marginTop: 8 }}><b>Đối soát với sàn:</b>
            {rc.issues.map((i: any) => <div key={i.key} className={i.severity === "block" ? "bad" : "mute"}>{i.text}</div>)}</div>}
          {["BINANCE_TESTNET", "LIVE"].includes(tr.mode) && <div style={{ marginTop: 8 }}>
            Tự động mua/thoát: <b className={tr.auto_enabled ? "bad" : "ok"}>{tr.auto_enabled ? "ĐANG BẬT" : "TẮT"}</b>{" "}
            {tr.auto_enabled ? <button onClick={() => toggleAuto(false)}>TẮT tự động</button>
              : <button disabled={!tr.enabled} onClick={() => toggleAuto(true)}>Bật tự động…</button>}
            <div className="mute">Tối đa {tr.auto_limits?.max_order_usdt} USDT/lệnh · {tr.auto_limits?.max_buys_per_day} lệnh mua/ngày · nghỉ {Math.round((tr.auto_limits?.cooldown_s ?? 0) / 3600)}h mỗi coin sau khi mua.
              Chỉ tự mua khi có BUY_SETUP và tự thoát khi 1h+4h cùng DOWN; giảm nửa vẫn cần bạn xác nhận. Tắt giao dịch là tự động cũng tắt.</div>
          </div>}
          {tr.mode === "PAPER_TRADING" && <div className="mute" style={{ marginTop: 8 }}>PAPER_TRADING đã tự động mua/bán giả lập khi có tín hiệu.</div>}
          <div className="mute">Mỗi lệnh vẫn cần bạn bấm xác nhận riêng. Chế độ đổi bằng TRADING_MODE trong .env của backend.</div>
        </>}
      </div>
      <h1>AI</h1>
      <div className="card">
        {ai && <>Đang chọn: <b>{ai.selected}</b> · đã cấu hình: {Object.entries(ai.configured).map(([k, v]) => `${k}=${v ? "có" : "chưa"}`).join(", ")}
          <div className="mute">Đổi nhà cung cấp bằng AI_PROVIDER và key trong file .env của backend (xem docs/AI_KEYS.md).</div></>}
      </div>
    </>
  );
}
