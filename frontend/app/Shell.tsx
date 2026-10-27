"use client";
import Link from "next/link";
import { createContext, useCallback, useContext, useEffect, useState } from "react";

type Api = <T = any>(path: string, init?: RequestInit) => Promise<T>;
const Ctx = createContext<{ api: Api; token: string } | null>(null);

export function useApp() {
  const c = useContext(Ctx);
  if (!c) throw new Error("useApp ngoài Shell");
  return c;
}

export const fmt = (n: number | null | undefined, d = 2) =>
  n == null ? "—" : n.toLocaleString("en-US", { maximumFractionDigits: n !== 0 && Math.abs(n) < 0.001 ? Math.max(d, 10) : d });
export const ts = (v: string | number | null | undefined) =>
  v == null ? "—" : new Date(typeof v === "number" ? v * 1000 : v).toLocaleString("vi-VN");
export const pnlCls = (n: number | null | undefined) => (n == null ? "" : n >= 0 ? "ok" : "bad");

const NAV = [["/", "Dashboard"], ["/portfolio", "Portfolio"], ["/history", "Lịch sử"], ["/backtest", "Backtest"], ["/settings", "Cài đặt"]];

export default function Shell({ children }: { children: React.ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    try { setToken(sessionStorage.getItem("token")); } catch {}
    setReady(true);
  }, []);

  const logout = () => { setToken(null); try { sessionStorage.removeItem("token"); } catch {} };

  const api: Api = useCallback(async (path, init = {}) => {
    const r = await fetch(`/api${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init.headers || {}) },
    });
    if (r.status === 401) logout();
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  }, [token]);

  async function login(e: React.FormEvent) {
    e.preventDefault(); setErr("");
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw }) });
    if (!r.ok) return setErr("Sai mật khẩu");
    const { token } = await r.json();
    try { sessionStorage.setItem("token", token); } catch {}
    setToken(token); setPw("");
  }

  if (!ready) return null;
  if (!token)
    return (
      <main><h1>Coin Assistant</h1>
        <form className="card row" onSubmit={login}>
          <input type="password" placeholder="Mật khẩu" value={pw} onChange={(e) => setPw(e.target.value)} />
          <button>Đăng nhập</button><span className="bad">{err}</span>
        </form>
      </main>
    );

  return (
    <Ctx.Provider value={{ api, token }}>
      <main>
        <nav className="row nav">
          {NAV.map(([href, label]) => <Link key={href} href={href}>{label}</Link>)}
          <span style={{ flex: 1 }} /><button onClick={logout}>Đăng xuất</button>
        </nav>
        {children}
      </main>
    </Ctx.Provider>
  );
}
