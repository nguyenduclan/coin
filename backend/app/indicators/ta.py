"""Chỉ báo kỹ thuật tính bằng code (không dùng AI). Đầu vào: DataFrame cột open/high/low/close/volume."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI Wilder: trung bình đầu tiên là SMA của n thay đổi đầu, sau đó làm mượt đệ quy."""
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return pd.Series(out, index=close.index)
    d = np.diff(close.to_numpy(dtype=float))
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), dn[:n].mean()

    def val(a, b):
        return 100.0 if b == 0 else 100 - 100 / (1 + a / b)

    out[n] = val(au, ad)
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
        out[i + 1] = val(au, ad)
    return pd.Series(out, index=close.index)


def macd(close: pd.Series, fast=12, slow=26, signal=9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd})


def vwap(df: pd.DataFrame, n: int = 50) -> pd.Series:
    """VWAP trượt n nến (không reset theo phiên)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).rolling(n).sum() / df["volume"].rolling(n).sum()


def swing_levels(df: pd.DataFrame, w: int = 5, max_levels: int = 3) -> dict:
    """Hỗ trợ/kháng cự từ đỉnh/đáy cục bộ (cần w nến mỗi bên nên chỉ dùng nến đã đóng)."""
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    last = df["close"].iloc[-1]
    highs, lows = [], []
    for i in range(w, len(df) - w):
        if hi[i] == hi[i - w:i + w + 1].max():
            highs.append(hi[i])
        if lo[i] == lo[i - w:i + w + 1].min():
            lows.append(lo[i])
    res = sorted({h for h in highs if h > last})[:max_levels]
    sup = sorted({l for l in lows if l < last}, reverse=True)[:max_levels]
    return {"resistance": [float(x) for x in res], "support": [float(x) for x in sup]}


MIN_CANDLES = 60


def classify_trend(df: pd.DataFrame) -> str:
    """UP / DOWN / SIDEWAYS / INSUFFICIENT dựa trên EMA và histogram MACD."""
    if len(df) < MIN_CANDLES:
        return "INSUFFICIENT"
    c = df["close"]
    e20, e50 = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1]
    h = macd(c)["hist"].iloc[-1]
    px = c.iloc[-1]
    if px > e20 > e50 and h > 0:
        return "UP"
    if px < e20 < e50 and h < 0:
        return "DOWN"
    return "SIDEWAYS"


def _f(x) -> float | None:
    return None if x is None or pd.isna(x) else float(x)


def summarize(df: pd.DataFrame) -> dict:
    """Bộ chỉ báo của nến đóng cuối cùng."""
    if len(df) < MIN_CANDLES:
        return {"trend": "INSUFFICIENT", "candles": len(df)}
    c = df["close"]
    m, bb = macd(c), bollinger(c)
    vol_avg = df["volume"].rolling(20).mean().iloc[-1]
    out = {
        "trend": classify_trend(df), "candles": len(df), "close": _f(c.iloc[-1]),
        "ema9": _f(ema(c, 9).iloc[-1]), "ema20": _f(ema(c, 20).iloc[-1]),
        "ema50": _f(ema(c, 50).iloc[-1]),
        "ema200": _f(ema(c, 200).iloc[-1]) if len(df) >= 200 else None,
        "rsi14": _f(rsi(c).iloc[-1]),
        "macd": _f(m["macd"].iloc[-1]), "macd_signal": _f(m["signal"].iloc[-1]),
        "macd_hist": _f(m["hist"].iloc[-1]),
        "atr14": _f(atr(df).iloc[-1]),
        "bb_upper": _f(bb["upper"].iloc[-1]), "bb_lower": _f(bb["lower"].iloc[-1]),
        "vwap50": _f(vwap(df).iloc[-1]),
        "volume_ratio": _f(df["volume"].iloc[-1] / vol_avg) if vol_avg else None,
    }
    out.update(swing_levels(df))
    return out


def series_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Chỉ báo cho MỌI hàng, chỉ dùng dữ liệu quá khứ của hàng đó (không look-ahead) — dùng cho backtest.
    Giá trị tại hàng i khớp summarize(df.iloc[:i+1]) (có test kiểm tra)."""
    c = df["close"]
    m = macd(c)
    e20, e50 = ema(c, 20), ema(c, 50)
    out = pd.DataFrame({"close": c, "ema20": e20, "ema50": e50, "rsi14": rsi(c),
                        "atr14": atr(df), "hist": m["hist"]})
    up = (c > e20) & (e20 > e50) & (m["hist"] > 0)
    dn = (c < e20) & (e20 < e50) & (m["hist"] < 0)
    trend = pd.Series("SIDEWAYS", index=df.index)
    trend[up] = "UP"
    trend[dn] = "DOWN"
    trend.iloc[: MIN_CANDLES - 1] = "INSUFFICIENT"
    out["trend"] = trend
    return out
