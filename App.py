"""Nexion F&O Terminal - Streamlit + Zerodha Kite Connect (analysis only, no orders)."""
import base64
import hmac
import json
import threading
import html
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import contextmanager
import math
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kex

IST = ZoneInfo("Asia/Kolkata")
st.set_page_config(page_title="Nexion F&O Terminal", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")

# ----------------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------------
def secret(name):
    try:
        return st.secrets[name]
    except Exception:
        return None


API_KEY, API_SECRET, APP_PASSWORD = secret("KITE_API_KEY"), secret("KITE_API_SECRET"), secret("APP_PASSWORD")
if not API_KEY or not API_SECRET:
    st.error("KITE_API_KEY / KITE_API_SECRET Streamlit Secrets mein set nahi hain. "
             "App settings > Secrets mein add karein (README/deployment steps dekhein).")
    st.stop()


def friendly(e):
    """User-safe error text. Never includes keys/tokens."""
    if isinstance(e, kex.TokenException):
        return "Session expire/invalid hai. Dobara login karein."
    if isinstance(e, kex.PermissionException):
        return "Zerodha ne is data ki permission nahi di (Kite Connect plan/account check karein)."
    if isinstance(e, kex.InputException):
        return f"Request galat thi: {str(e)[:150]}"
    if isinstance(e, kex.NetworkException):
        return "Zerodha/network se response nahi mila ya rate limit lagi. Thoda ruk kar refresh karein."
    return f"Unexpected error ({type(e).__name__}). Dobara try karein."


# ----------------------------------------------------------------------------
# Auth: official Kite flow  login_url -> Zerodha login (+TOTP) -> redirect with
# request_token -> generate_session -> access_token (valid till ~6 AM next day)
# ----------------------------------------------------------------------------
@st.cache_resource
def token_store():
    """Server-side store so a refresh / second device reuses today's token.
    Lost whenever the Streamlit container restarts (then just log in again)."""
    return {}


STORE = token_store()


def token_expiry(created):
    exp = created.replace(hour=6, minute=0, second=0, microsecond=0)
    return exp + timedelta(days=1) if created >= exp else exp


def stored_token():
    tok = STORE.get("token")
    if tok and datetime.now(IST) < token_expiry(STORE["created"]):
        return tok
    STORE.clear()
    return None


def reset_session():
    STORE.clear()
    for k in ("access_token", "verified", "user"):
        st.session_state.pop(k, None)


def handle_callback(kite):
    qp = st.query_params
    rt = qp.get("request_token")
    if not rt:
        return
    status = qp.get("status", "success")
    keep_k = qp.get("k")
    st.query_params.clear()  # request_token URL mein na rahe
    if keep_k and APP_PASSWORD and gate_valid(keep_k):
        st.query_params["k"] = keep_k
    if status != "success":
        st.session_state.login_error = f"Zerodha login complete nahi hua (status: {status}). Dobara try karein."
        return
    try:
        data = kite.generate_session(rt, api_secret=API_SECRET)
        STORE.update(token=data["access_token"], created=datetime.now(IST),
                     user=data.get("user_name") or data.get("user_id"))
        st.session_state.access_token = data["access_token"]
        st.session_state.pop("verified", None)
    except kex.TokenException:
        st.session_state.login_error = "request_token expire/use ho chuka hai (ye sirf kuch minute valid rehta hai). Dobara login karein."
    except kex.InputException:
        st.session_state.login_error = "API key ya API secret galat lag raha hai. Streamlit Secrets check karein."
    except Exception as e:
        st.session_state.login_error = f"Login fail: {friendly(e)}"


GATE_DAYS = 30


def _gate_sig(issued):
    return hmac.new(str(APP_PASSWORD).encode(), f"nexion:{issued}".encode(), "sha256").hexdigest()[:24]


def gate_token():
    issued = int(time.time() // 86400)
    return f"{issued}.{_gate_sig(issued)}"


def gate_valid(tok):
    try:
        issued, sig = str(tok).split(".")
        age = int(time.time() // 86400) - int(issued)
        return 0 <= age <= GATE_DAYS and hmac.compare_digest(sig, _gate_sig(int(issued)))
    except Exception:
        return False


def password_gate():
    """Password ek baar; uske baad URL mein signed key (?k=...) 30 din tak password skip karti hai."""
    if not APP_PASSWORD or st.session_state.get("gate_ok"):
        return
    k = st.query_params.get("k")
    if k and gate_valid(k):
        st.session_state.gate_ok = True
        return
    st.title("🔒 Nexion F&O Terminal")
    pw = st.text_input("App password", type="password")
    if pw:
        if hmac.compare_digest(pw.encode(), str(APP_PASSWORD).encode()):
            st.session_state.gate_ok = True
            st.query_params["k"] = gate_token()
            st.rerun()
        st.error("Galat password.")
    st.stop()


def ensure_session():
    kite = KiteConnect(api_key=API_KEY)
    handle_callback(kite)
    password_gate()
    token = st.session_state.get("access_token") or stored_token()
    if token:
        kite.set_access_token(token)
        if not st.session_state.get("verified"):
            try:
                prof = kite.profile()
                st.session_state.update(access_token=token, verified=True,
                                        user=prof.get("user_name") or prof.get("user_id"))
            except kex.TokenException:
                reset_session()
                token = None
            except Exception as e:
                st.error(friendly(e))
                st.stop()
    if not token:
        st.title("📈 Nexion F&O Terminal")
        err = st.session_state.pop("login_error", None)
        if err:
            st.error(err)
        st.info("1) Neeche button tap karein  2) Zerodha par login + TOTP  3) App par apne aap wapas aa jayenge.")
        lurl = kite.login_url()
        if APP_PASSWORD and st.session_state.get("gate_ok"):
            lurl += "&redirect_params=" + urllib.parse.quote("k=" + gate_token())
        st.link_button("🔐 Zerodha se Login karein", lurl, type="primary")
        st.caption("Login Zerodha ke official page par hota hai; app aapka password/TOTP kabhi nahi dekhta.")
        st.stop()
    return kite


def guarded(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except kex.TokenException:
        reset_session()
        st.rerun(scope="app")
    except Exception as e:
        st.warning(friendly(e))
        return None


@contextmanager
def section(name):
    """Ek tab/section mein bug aaye to baaki app chalta rahe."""
    try:
        yield
    except Exception as e:  # st.rerun/st.stop alag exception type hain, ye unhe nahi pakadta
        st.error(f"⚠️ {name} section mein error ({type(e).__name__}): {str(e)[:160]}")


# ----------------------------------------------------------------------------
# Data layer (cached to respect rate limits: quote ~1/s, historical ~3/s)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=6 * 3600, show_spinner="Instrument master load ho raha hai (pehli baar 10-20 sec)...")
def load_instruments(_kite, day):
    frames = []
    for ex in ("NSE", "BSE", "NFO", "BFO"):
        try:
            frames.append(pd.DataFrame(_kite.instruments(ex)))
        except kex.TokenException:
            raise
        except Exception:
            pass
    df = pd.concat(frames, ignore_index=True)
    keep = ["instrument_token", "tradingsymbol", "name", "expiry", "strike", "lot_size",
            "instrument_type", "segment", "exchange"]
    df = df[keep].copy()
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce")
    return df


@st.cache_data(ttl=3, show_spinner=False)
def get_quotes(_kite, keys):
    out = {}
    for i in range(0, len(keys), 400):
        out.update(_kite.quote(list(keys[i:i + 400])))
    return out


@st.cache_data(ttl=60, show_spinner=False)
def get_history(_kite, token, interval, days, oi=False):
    to = datetime.now(IST).replace(tzinfo=None)
    return pd.DataFrame(_kite.historical_data(token, to - timedelta(days=days), to, interval, oi=oi))


@st.cache_data(ttl=300, show_spinner=False)
def get_oi_change(_kite, token):
    """(latest OI, change vs previous daily candle). Not directly in quote()."""
    time.sleep(0.35)
    df = get_history(_kite, token, "day", 7, True)
    if df is None or len(df) < 2 or "oi" not in df:
        return None
    return float(df.oi.iloc[-1]), float(df.oi.iloc[-1] - df.oi.iloc[-2])


# ----------------------------------------------------------------------------
# Calculations (all documented in the "Logic" tab)
# ----------------------------------------------------------------------------
def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _n(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def bs(S, K, T, r, sig, cp):
    if T <= 0 or sig <= 0:
        return max(S - K, 0) if cp == "CE" else max(K - S, 0)
    d1 = (math.log(S / K) + (r + sig * sig / 2) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if cp == "CE":
        return S * _N(d1) - K * math.exp(-r * T) * _N(d2)
    return K * math.exp(-r * T) * _N(-d2) - S * _N(-d1)


def greeks(S, K, T, r, sig, cp):
    d1 = (math.log(S / K) + (r + sig * sig / 2) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    gamma = _n(d1) / (S * sig * math.sqrt(T))
    vega = S * _n(d1) * math.sqrt(T) / 100  # per 1 vol point
    base = -S * _n(d1) * sig / (2 * math.sqrt(T))
    if cp == "CE":
        delta, theta = _N(d1), base - r * K * math.exp(-r * T) * _N(d2)
    else:
        delta, theta = _N(d1) - 1, base + r * K * math.exp(-r * T) * _N(-d2)
    return dict(delta=delta, gamma=gamma, theta=theta / 365, vega=vega)  # theta per day


def implied_vol(price, S, K, T, r, cp):
    if not price or price <= 0 or T <= 0:
        return None
    intrinsic = max(S - K * math.exp(-r * T), 0) if cp == "CE" else max(K * math.exp(-r * T) - S, 0)
    if price < intrinsic - 0.01:
        return None
    lo, hi = 0.01, 5.0
    if bs(S, K, T, r, hi, cp) < price or bs(S, K, T, r, lo, cp) > price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if bs(S, K, T, r, mid, cp) > price else (mid, hi)
    return (lo + hi) / 2


def years_to_expiry(expiry):
    exp = datetime(expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=IST)
    return max((exp - datetime.now(IST)).total_seconds(), 60) / (365 * 86400)


def true_range(d):
    pc = d.close.shift()
    return pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)


def supertrend(d, period=10, mult=3.0):
    atr = true_range(d).ewm(alpha=1 / period, adjust=False).mean().values
    hl2 = ((d.high + d.low) / 2).values
    c = d.close.values
    n = len(d)
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fub, flb, dr = ub.copy(), lb.copy(), np.ones(n)
    for i in range(1, n):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        dr[i] = 1 if c[i] > fub[i - 1] else -1 if c[i] < flb[i - 1] else dr[i - 1]
    return np.where(dr == 1, flb, fub), dr


def add_indicators(df, intraday=False):
    """EMA9/21/50/200, RSI, ATR, ADX/DMI, MACD, Stochastic, Supertrend; volume ho to rel-vol/OBV (+ intraday VWAP)."""
    d = df.copy()
    for sp in (9, 21, 50, 200):
        d[f"ema{sp}"] = d.close.ewm(span=sp, adjust=False).mean()
    chg = d.close.diff()
    up, dn = chg.clip(lower=0), (-chg.clip(upper=0)).replace(0, 1e-12)
    rs = up.ewm(alpha=1 / 14, adjust=False).mean() / dn.ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + rs)
    tr = true_range(d)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    a = d.atr.replace(0, np.nan)
    uph, dnl = d.high.diff(), -d.low.diff()
    pdm = pd.Series(np.where((uph > dnl) & (uph > 0), uph, 0.0), index=d.index)
    mdm = pd.Series(np.where((dnl > uph) & (dnl > 0), dnl, 0.0), index=d.index)
    d["pdi"] = 100 * pdm.ewm(alpha=1 / 14, adjust=False).mean() / a
    d["mdi"] = 100 * mdm.ewm(alpha=1 / 14, adjust=False).mean() / a
    dx = 100 * (d.pdi - d.mdi).abs() / (d.pdi + d.mdi).replace(0, np.nan)
    d["adx"] = dx.ewm(alpha=1 / 14, adjust=False).mean()
    macd = d.close.ewm(span=12, adjust=False).mean() - d.close.ewm(span=26, adjust=False).mean()
    d["macd"], d["macd_sig"] = macd, macd.ewm(span=9, adjust=False).mean()
    d["macd_hist"] = d.macd - d.macd_sig
    ll, hh = d.low.rolling(14).min(), d.high.rolling(14).max()
    d["stoch_k"] = (100 * (d.close - ll) / (hh - ll).replace(0, np.nan)).rolling(3).mean()
    d["st"], d["st_dir"] = supertrend(d)
    if "volume" in d and d.volume.fillna(0).sum() > 0:
        d["relvol"] = d.volume / d.volume.rolling(20).mean()
        d["obv"] = (np.sign(d.close.diff()).fillna(0) * d.volume).cumsum()
        if intraday:
            day = pd.to_datetime(d["date"]).dt.date
            tp = (d.high + d.low + d.close) / 3
            d["vwap"] = (tp * d.volume).groupby(day).cumsum() / d.volume.groupby(day).cumsum()
    return d


def market_open():
    n = datetime.now(IST)
    return n.weekday() < 5 and (9, 15) <= (n.hour, n.minute) <= (15, 30)


def num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


def build_chain(quotes, sel, S, T, r):
    rows = {}
    for _, c in sel.iterrows():
        q = quotes.get(f"{c.exchange}:{c.tradingsymbol}")
        if not q:
            continue
        tag, K = c.instrument_type, float(c.strike)
        ltp = q.get("last_price") or 0
        iv = implied_vol(ltp, S, K, T, r, tag)
        g = greeks(S, K, T, r, iv, tag) if iv else None
        depth = q.get("depth") or {}
        bid = (depth.get("buy") or [{}])[0].get("price") or None
        ask = (depth.get("sell") or [{}])[0].get("price") or None
        row = rows.setdefault(K, {"Strike": K})
        row.update({f"{tag} LTP": ltp, f"{tag} OI": q.get("oi"), f"{tag} Vol": q.get("volume"),
                    f"{tag} IV%": iv * 100 if iv else None, f"{tag} Delta": g["delta"] if g else None,
                    f"{tag} bid": bid, f"{tag} ask": ask, f"{tag} sym": c.tradingsymbol,
                    f"{tag} lot": c.lot_size, f"{tag} iv": iv,
                    f"{tag} token": c.instrument_token})
    return pd.DataFrame(rows.values()).sort_values("Strike").reset_index(drop=True) if rows else pd.DataFrame()


def parity_spot(cq, sel, K, T, r):
    """Underlying implied by put-call parity at strike K: C - P + K*exp(-rT)."""
    v = {}
    for _, c in sel[sel.strike == K].iterrows():
        q = cq.get(f"{c.exchange}:{c.tradingsymbol}")
        if q and q.get("last_price"):
            v[c.instrument_type] = q["last_price"]
    return v["CE"] - v["PE"] + K * math.exp(-r * T) if len(v) == 2 else None


def chain_stats(ch):
    ce, pe = ch.get("CE OI", pd.Series(dtype=float)).fillna(0), ch.get("PE OI", pd.Series(dtype=float)).fillna(0)
    if ce.sum() == 0 or pe.sum() == 0:
        return None
    k = ch.Strike.values
    pain = [float((np.maximum(x - k, 0) * ce.values).sum() + (np.maximum(k - x, 0) * pe.values).sum()) for x in k]
    return dict(pcr=pe.sum() / ce.sum(), resistance=float(ch.Strike[ce.idxmax()]),
                support=float(ch.Strike[pe.idxmax()]), max_pain=float(k[int(np.argmin(pain))]))


# ----------------------------------------------------------------------------
# News (Google News RSS) + crude keyword sentiment. Low confidence by design.
# ----------------------------------------------------------------------------
POS = set("surge surges rally rallies gain gains jump jumps soar soars rise rises record high upgrade upgrades beat beats "
          "strong bullish profit growth boost recover recovers rebound optimism positive outperform wins approval "
          "buyback dividend expansion".split())
NEG = set("fall falls drop drops plunge plunges slump slumps slide slides crash decline declines loss losses weak bearish "
          "downgrade downgrades miss misses probe fraud penalty ban tariff war inflation concern concerns fear fears worry "
          "slowdown selloff sell-off resigns default recall raid shut outflow outflows".split())


def headline_score(title):
    w = set(re.findall(r"[a-z\-]+", title.lower()))
    p, n = len(w & POS), len(w & NEG)
    return (p > n) - (p < n)


@st.cache_data(ttl=900, show_spinner=False)
def fetch_news(query):
    try:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query + " when:2d")
               + "&hl=en-IN&gl=IN&ceid=IN:en")
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        items = []
        for it in list(ET.fromstring(r.content).iter("item"))[:15]:
            title = (it.findtext("title") or "").strip()
            try:
                when = parsedate_to_datetime(it.findtext("pubDate")).astimezone(IST)
            except Exception:
                when = None
            items.append(dict(title=title, link=it.findtext("link"), when=when, score=headline_score(title)))
        return items, None
    except Exception as e:
        return [], f"News fetch nahi hua ({type(e).__name__})"


def news_factor(items):
    if len(items) < 3:
        return None
    avg = sum(i["score"] for i in items) / len(items)
    pts = 1 if avg > 0.25 else -1 if avg < -0.25 else 0
    return pts, f"{len(items)} headlines, avg sentiment {avg:+.2f} (keyword-based, low confidence)"



# ----------------------------------------------------------------------------
# Strategy engine
# ----------------------------------------------------------------------------
def buildup(price_chg_pct, oi_chg):
    if oi_chg is None or abs(price_chg_pct) < 0.1:
        return None
    if price_chg_pct > 0 and oi_chg > 0:
        return 1, "Long build-up: price up + OI up"
    if price_chg_pct < 0 and oi_chg > 0:
        return -1, "Short build-up: price down + OI up"
    if price_chg_pct > 0:
        return 0, "Short covering: price up + OI down (weak signal)"
    return 0, "Long unwinding: price down + OI down (weak signal)"




def make_leg(rows, K, tag, sgn):
    r = rows.get(K)
    if r is None:
        return None
    ltp, iv = num(r.get(f"{tag} LTP")), num(r.get(f"{tag} iv"))
    if not ltp or not iv:
        return None
    ask, bid = num(r.get(f"{tag} ask")), num(r.get(f"{tag} bid"))
    return dict(s=sgn, tag=tag, K=K, iv=iv, ltp=ltp, entry=(ask or ltp) if sgn > 0 else (bid or ltp),
                sym=r.get(f"{tag} sym"), vol=num(r.get(f"{tag} Vol")), delta=num(r.get(f"{tag} Delta")),
                spread=(ask - bid) / ask * 100 if ask and bid else None)


def pick_by_delta(rows, strikes, tag, target):
    best = None
    for K in strikes:
        dl, ltp = num(rows[K].get(f"{tag} Delta")), num(rows[K].get(f"{tag} LTP"))
        if dl is None or not ltp:
            continue
        gap = abs(abs(dl) - target)
        if best is None or gap < best[0]:
            best = (gap, K)
    return best[1] if best else None


def credit_spread(ctx, tag, target):
    """Hedged short option: short strike beyond OI wall (delta<=0.30) + long strike 1-2 steps further OTM."""
    rows, strikes, stats, S = ctx["rows"], ctx["strikes"], ctx["stats"], ctx["spot"]
    wall = (stats["support"] if tag == "PE" else stats["resistance"]) if stats else None
    ok = []
    for K in strikes:
        dl, ltp = num(rows[K].get(f"{tag} Delta")), num(rows[K].get(f"{tag} LTP"))
        if dl is None or not ltp:
            continue
        if (K <= S if tag == "PE" else K >= S) and abs(dl) <= 0.30:
            ok.append(K)
    note = None
    if wall is not None:
        w = [K for K in ok if (K <= wall if tag == "PE" else K >= wall)]
        if w:
            ok, note = w, f"Short strike OI wall {wall:.0f} ke bahar/barabar rakha"
        else:
            note = f"OI wall {wall:.0f} ke bahar suitable strike nahi mila - delta se chuna"
    K = (max(ok) if tag == "PE" else min(ok)) if ok else pick_by_delta(rows, strikes, tag, target)
    if K is None:
        return None, "Short strike nahi mila (market data missing)."
    sh = make_leg(rows, K, tag, -1)
    if not sh:
        return None, "Short leg ka price/IV nahi mila."
    lg, i = None, strikes.index(K)
    for steps in (2, 1):
        j = max(0, min(len(strikes) - 1, i - steps if tag == "PE" else i + steps))
        if j != i:
            lg = make_leg(rows, strikes[j], tag, +1)
        if lg:
            break
    if not lg:
        return None, "Hedge leg ka price nahi mila (sidebar mein strikes window badhayein)."
    credit = sh["entry"] - lg["entry"]
    width = abs(sh["K"] - lg["K"])
    if credit <= 0:
        return None, "Net credit nahi ban raha."
    notes = [note] if note else []
    if credit < 0.15 * width:
        notes.append("⚠️ Credit spread width ke 15% se kam - reward kamzor.")
    return [sh, lg], notes


def shift_legs(legs, S0, S1, T, cfg, basis):
    """Legs ki entry price ko underlying S0 se S1 par reprice karta hai (breakout trigger par entry ke liye)."""
    out = []
    for l in legs:
        l2 = dict(l)
        l2["entry"] = max(0.05, l["entry"] + bs(S1 + basis, l["K"], T, cfg["r"], l["iv"], l["tag"])
                          - bs(S0 + basis, l["K"], T, cfg["r"], l["iv"], l["tag"]))
        out.append(l2)
    return out


def struct_eval(legs, lot, T, scen, cfg, basis=0.0):
    V0 = sum(l["s"] * l["entry"] for l in legs)
    out = {"V0": V0}
    for name, (S1, hd) in scen.items():
        Tn = max(T - hd / 365, 1 / 525600)
        prices = [bs(S1 + basis, l["K"], Tn, cfg["r"], l["iv"], l["tag"]) for l in legs]
        V1 = sum(l["s"] * p for l, p in zip(legs, prices))
        costs = 2 * cfg["brok"] * len(legs)
        for l, p in zip(legs, prices):
            sold = l["entry"] if l["s"] < 0 else p
            costs += cfg["stt"] / 100 * sold * lot + cfg["slip"] / 100 * (l["entry"] + p) * lot
        out[name] = dict(S=S1, V=V1, net=(V1 - V0) * lot - costs)
    return out



# ----------------------------------------------------------------------------
# Global cues (Yahoo Finance public chart endpoint - unofficial, kabhi fail ho sakta hai)
# ----------------------------------------------------------------------------
GLOBALS = [("S&P 500", "^GSPC", "us"), ("Nasdaq", "^IXIC", "us"), ("Dow Jones", "^DJI", "us"),
           ("Nikkei 225", "^N225", "asia"), ("Hang Seng", "^HSI", "asia"), ("Shanghai", "000001.SS", "asia"),
           ("Dollar Index", "DX-Y.NYB", "macro"), ("Brent Crude", "BZ=F", "macro"), ("USD/INR", "INR=X", "macro")]


def _fetch_one(item):
    name, sym, grp = item
    try:
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym),
                         params={"range": "5d", "interval": "1d"}, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        res = r.json()["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        return dict(name=name, grp=grp, last=closes[-1], chg=(closes[-1] / closes[-2] - 1) * 100)
    except Exception:
        return dict(name=name, grp=grp, last=None, chg=None)


@st.cache_data(ttl=300, show_spinner=False)
def fetch_global():
    with ThreadPoolExecutor(max_workers=5) as ex:
        return list(ex.map(_fetch_one, GLOBALS))


def global_factor(g):
    vals = [x["chg"] for x in g if x["grp"] in ("us", "asia") and x["chg"] is not None]
    if len(vals) < 4:
        return None
    avg = sum(vals) / len(vals)
    return (1 if avg > 0.3 else -1 if avg < -0.3 else 0), f"US+Asia indices avg {avg:+.2f}%"


# ----------------------------------------------------------------------------
# Favourites (GitHub ki favourites.txt se)
# ----------------------------------------------------------------------------
def load_favs():
    try:
        lines = (Path(__file__).with_name("favourites.txt")).read_text(encoding="utf-8").splitlines()
        favs = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
        return favs or ["NIFTY 50", "NIFTY BANK", "SENSEX"]
    except Exception:
        return ["NIFTY 50", "NIFTY BANK", "SENSEX"]


# ============================================================================
# ANALYTICS: pivots, S/R levels, opening range, market structure (BOS/CHOCH), candle patterns
# Sab calculations sirf Zerodha ke asli candles/quotes par; kuch hardcode/simulate nahi.
# ============================================================================
def sg(x):
    return 1 if x > 0 else -1 if x < 0 else 0


def to_ist(ts):
    if ts is None:
        return None
    try:
        t = pd.Timestamp(ts)
        return t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)
    except Exception:
        return None


def dts_ist(s):
    t = pd.to_datetime(s)
    return t.dt.tz_convert(IST) if getattr(t.dt, "tz", None) is not None else t


def pivots(d, n=3):
    """Swing highs/lows; pivot tab confirm hota hai jab uske baad n candles aa jayein."""
    h, l = d.high.values, d.low.values
    ph, pl = [], []
    for i in range(n, len(d) - n):
        if h[i] > max(h[i - n:i]) and h[i] >= max(h[i + 1:i + n + 1]):
            ph.append((i, float(h[i])))
        if l[i] < min(l[i - n:i]) and l[i] <= min(l[i + 1:i + n + 1]):
            pl.append((i, float(l[i])))
    return ph, pl


def sr_clusters(d, spot, atr, n=3, lookback=150):
    dd = d.tail(lookback).reset_index(drop=True)
    ph, pl = pivots(dd, n)
    pts = sorted([(p, dd.date.iloc[i]) for i, p in ph + pl])
    tol = max(0.35 * atr, 0.0005 * spot)
    cl = []
    for p, ts in pts:
        if cl and abs(p - float(np.mean(cl[-1]["ps"]))) <= tol:
            cl[-1]["ps"].append(p)
            cl[-1]["ts"] = ts
        else:
            cl.append(dict(ps=[p], ts=ts))
    return [dict(price=float(np.mean(c["ps"])), touches=len(c["ps"]), ts=c["ts"]) for c in cl]


def prev_day(dday, today):
    try:
        days = dts_ist(dday.date).dt.date
        p = dday[days < today]
        return None if p.empty else p.iloc[-1]
    except Exception:
        return None


def opening_range(di, now):
    """Pehle 15 min (9:15-9:30) ka high/low, latest session ka."""
    try:
        t = dts_ist(di.date)
        last_day = t.dt.date.iloc[-1]
        sel = (t.dt.date == last_day) & ((t.dt.hour * 60 + t.dt.minute) < 9 * 60 + 30)
        orc = di[sel.values]
        if orc.empty:
            return None
        formed = (last_day != now.date()) or (now.hour * 60 + now.minute >= 9 * 60 + 30)
        return dict(high=float(orc.high.max()), low=float(orc.low.min()), formed=formed)
    except Exception:
        return None


def build_levels(spot, atr_n, dsc, dday, di, stats, now):
    lv = []
    for c in sr_clusters(dsc, spot, atr_n):
        lv.append(dict(price=c["price"], label=f"Swing ({c['touches']}x)", src="Swing", touches=c["touches"]))
    pdv = prev_day(dday, now.date())
    if pdv is not None:
        lv += [dict(price=float(pdv.high), label="Prev-day High", src="PDH", touches=1),
               dict(price=float(pdv.low), label="Prev-day Low", src="PDL", touches=1),
               dict(price=float(pdv.close), label="Prev-day Close", src="PDC", touches=1)]
    if di is not None:
        o = opening_range(di, now)
        if o:
            tag = "" if o["formed"] else " (forming)"
            lv += [dict(price=o["high"], label="OR High" + tag, src="OR", touches=1),
                   dict(price=o["low"], label="OR Low" + tag, src="OR", touches=1)]
    if len(dday) >= 20:
        lv += [dict(price=float(dday.high.tail(20).max()), label="20d High", src="20d", touches=1),
               dict(price=float(dday.low.tail(20).min()), label="20d Low", src="20d", touches=1)]
    if stats:
        lv += [dict(price=stats["resistance"], label="OI Resistance (max Call OI)", src="OI", touches=1),
               dict(price=stats["support"], label="OI Support (max Put OI)", src="OI", touches=1)]
    for l in lv:
        l["kind"] = "Support" if l["price"] < spot else "Resistance"
    return sorted(lv, key=lambda x: x["price"])


def structure_events(d, n=2, look=6):
    """BOS/CHOCH closed candles par. Trend: last 2 swing highs & lows (HH/HL = up, LH/LL = down)."""
    ph, pl = pivots(d, n)
    N, c = len(d), d.close.values
    ev = []

    def last_before(piv, i):
        cand = [p for p in piv if p[0] < i - n]
        return cand[-1] if cand else None

    def trend_at(i):
        hs, ls_ = [p for p in ph if p[0] < i - n][-2:], [p for p in pl if p[0] < i - n][-2:]
        if len(hs) == 2 and len(ls_) == 2:
            if hs[1][1] > hs[0][1] and ls_[1][1] > ls_[0][1]:
                return 1
            if hs[1][1] < hs[0][1] and ls_[1][1] < ls_[0][1]:
                return -1
        return 0

    for i in range(max(n + 3, N - look), N):
        sh, sl = last_before(ph, i), last_before(pl, i)
        tr = trend_at(i)
        if sh and c[i] > sh[1] and c[i - 1] <= sh[1]:
            ev.append(dict(name="CHOCH ↑" if tr == -1 else "BOS ↑", dir=1, level=sh[1], idx=i, ts=d.date.iloc[i]))
        if sl and c[i] < sl[1] and c[i - 1] >= sl[1]:
            ev.append(dict(name="CHOCH ↓" if tr == 1 else "BOS ↓", dir=-1, level=sl[1], idx=i, ts=d.date.iloc[i]))
    return trend_at(N), ev, ph, pl


def candle_patterns(d, levels, atr, tf):
    """Engulfing, rejection (pin bar), breakout+retest - sirf closed candles par. qual=True matlab signal mein ginega."""
    out, N = [], len(d)
    if N < 5:
        return out
    near = 0.5 * atr
    for i in range(max(1, N - 3), N):
        o, h, l, c = (d.open.iloc[i], d.high.iloc[i], d.low.iloc[i], d.close.iloc[i])
        po, pc = d.open.iloc[i - 1], d.close.iloc[i - 1]
        body, rng, ts = abs(c - o), max(h - l, 1e-9), d.date.iloc[i]
        if pc < po and c > o and c >= po and o <= pc and body >= 0.5 * atr:
            out.append(dict(name="Bullish engulfing", dir=1, tf=tf, level=float(c), ts=ts, qual=True, status="confirmed (candle close)"))
        if pc > po and c < o and c <= po and o >= pc and body >= 0.5 * atr:
            out.append(dict(name="Bearish engulfing", dir=-1, tf=tf, level=float(c), ts=ts, qual=True, status="confirmed (candle close)"))
        lw, uw, bb = min(o, c) - l, h - max(o, c), max(body, 0.05 * rng)
        if lw >= 2 * bb and lw >= 0.6 * rng:
            at = any(abs(l - x["price"]) <= near and x["kind"] == "Support" for x in levels)
            out.append(dict(name="Bullish rejection" + ("" if at else " (key level par nahi)"), dir=1, tf=tf, level=float(l), ts=ts,
                            qual=at, status="confirmed (candle close)"))
        if uw >= 2 * bb and uw >= 0.6 * rng:
            at = any(abs(h - x["price"]) <= near and x["kind"] == "Resistance" for x in levels)
            out.append(dict(name="Bearish rejection" + ("" if at else " (key level par nahi)"), dir=-1, tf=tf, level=float(h), ts=ts,
                            qual=at, status="confirmed (candle close)"))
    c, lo, hi = d.close.values, d.low.values, d.high.values
    last = c[-1]
    for L in levels:
        P = L["price"]
        if abs(P - last) > 3 * atr:
            continue
        up = [i for i in range(max(1, N - 20), N) if c[i] > P and c[i - 1] <= P]
        dn = [i for i in range(max(1, N - 20), N) if c[i] < P and c[i - 1] >= P]
        if up and last > P:
            b = up[-1]
            rt = [j for j in range(b + 1, N) if lo[j] <= P + 0.3 * atr and c[j] > P]
            if rt:
                out.append(dict(name=f"Breakout + retest hold ↑ ({L['label']})", dir=1, tf=tf, level=P, ts=d.date.iloc[rt[-1]], qual=True,
                                status="confirmed (candle close)"))
            elif N - 1 - b <= 3:
                out.append(dict(name=f"Breakout ↑ ({L['label']}), retest pending", dir=1, tf=tf, level=P, ts=d.date.iloc[b], qual=False,
                                status="developing"))
        if dn and last < P:
            b = dn[-1]
            rt = [j for j in range(b + 1, N) if hi[j] >= P - 0.3 * atr and c[j] < P]
            if rt:
                out.append(dict(name=f"Breakdown + retest hold ↓ ({L['label']})", dir=-1, tf=tf, level=P, ts=d.date.iloc[rt[-1]], qual=True,
                                status="confirmed (candle close)"))
            elif N - 1 - b <= 3:
                out.append(dict(name=f"Breakdown ↓ ({L['label']}), retest pending", dir=-1, tf=tf, level=P, ts=d.date.iloc[b], qual=False,
                                status="developing"))
    return out


def fut_oi_info(hf, tf):
    """Futures OI: day change (pichle session ke last candle se) aur ~1 ghante ka change - observation interval ke saath."""
    try:
        if hf is None or "oi" not in hf or len(hf) < 6:
            return None
        days = dts_ist(hf.date).dt.date
        last_day = days.iloc[-1]
        prev = hf[days < last_day]
        now_oi = float(hf.oi.iloc[-1])
        k = 4 if "15" in tf else 12
        return dict(now=now_oi, day=(now_oi - float(prev.oi.iloc[-1])) if not prev.empty else None,
                    hr=(now_oi - float(hf.oi.iloc[-1 - k])) if len(hf) > k else None,
                    t_now=hf.date.iloc[-1], t_prev=prev.date.iloc[-1] if not prev.empty else None,
                    t_hr=hf.date.iloc[-1 - k] if len(hf) > k else None)
    except Exception:
        return None


# ============================================================================
# ANALYSIS: 3 directional categories (Regime 25% / Structure 25% / Positioning 20%)
# Har category ke andar signals ka weighted average (max ±1) - correlated indicators
# ek hi category mein hone se score inflate nahi hota. Volatility aur construction
# direction nahi, instrument/quality decide karte hain.
# ============================================================================
NEED = 35.0
CAT_W = {"Regime": 0.25, "Structure": 0.25, "Positioning": 0.20}


def analyze_horizon(name, ds, dd, hf, ctx, tf_label, intraday):
    spot, stats, bu, oiw, glb, nws = ctx["spot"], ctx["stats"], ctx["bu"], ctx["oiw"], ctx["glb"], ctx["news"]
    now, is_open = ctx["now"], ctx["is_open"]
    dsc = ds.iloc[:-1] if (is_open and len(ds) > 40) else ds
    ls, ld = ds.iloc[-1], dd.iloc[-1]
    atr_n = float(ds.atr.iloc[-1])
    lv = build_levels(spot, atr_n, dsc, dd, ds if intraday else None, stats, now)
    state, events, ph, pl = structure_events(dsc)
    pats = candle_patterns(dsc, lv, atr_n, tf_label)
    rows = []

    def add(cat, nm, tf, val, reading, sig, w=0.0):
        rows.append(dict(cat=cat, name=nm, tf=tf, val=val, reading=reading, sig=sig, w=w))

    ok = lambda x: x is not None and not pd.isna(x)
    # ---- Regime (slow, daily)
    if len(dd) >= 200 and ok(ld.ema200):
        a, b, nm = ld.ema50, ld.ema200, "EMA50/200"
    else:
        a, b, nm = ld.ema21, ld.ema50, "EMA21/50"
    s = 1 if (a > b and ld.close > a) else -1 if (a < b and ld.close < a) else 0
    add("Regime", nm, "1D", f"{a:,.0f} / {b:,.0f}", {1: "Uptrend", -1: "Downtrend", 0: "Mixed"}[s], s, 0.30)
    if ok(ld.adx):
        s = sg(ld.pdi - ld.mdi) if ld.adx >= 20 else 0
        add("Regime", "ADX/DMI", "1D", f"ADX {ld.adx:.0f}, +DI {ld.pdi:.0f}, -DI {ld.mdi:.0f}",
            ("Strong " if ld.adx >= 25 else "") + ("bullish trend" if s > 0 else "bearish trend" if s < 0 else "weak trend / range"), s, 0.25)
    add("Regime", "Supertrend", "1D", f"{ld.st:,.0f}", "Bullish" if ld.st_dir > 0 else "Bearish", int(ld.st_dir), 0.20)
    if glb:
        add("Regime", "Global cues", "-", glb[1], "Positive" if glb[0] > 0 else "Negative" if glb[0] < 0 else "Mixed", glb[0], 0.15)
    if nws:
        add("Regime", "News (keyword, low conf.)", "-", nws[1], "Positive" if nws[0] > 0 else "Negative" if nws[0] < 0 else "Neutral", nws[0], 0.10)
    # ---- Structure (selected timeframe)
    s = 1 if (ls.ema9 > ls.ema21 and ls.close > ls.ema21) else -1 if (ls.ema9 < ls.ema21 and ls.close < ls.ema21) else 0
    add("Structure", "EMA9/21", tf_label, f"{ls.ema9:,.0f} / {ls.ema21:,.0f}", {1: "Bullish", -1: "Bearish", 0: "Mixed"}[s], s, 0.15)
    add("Structure", "Supertrend", tf_label, f"{ls.st:,.0f}", "Bullish" if ls.st_dir > 0 else "Bearish", int(ls.st_dir), 0.15)
    hprev = ds.macd_hist.iloc[-2] if len(ds) > 1 else ls.macd_hist
    s = 1 if (ls.macd > ls.macd_sig and ls.macd_hist > hprev) else -1 if (ls.macd < ls.macd_sig and ls.macd_hist < hprev) else 0
    add("Structure", "MACD", tf_label, f"hist {ls.macd_hist:+.1f}", {1: "Bullish momentum", -1: "Bearish momentum", 0: "Mixed/fading"}[s], s, 0.15)
    r = ls.rsi
    s = 1 if 55 < r <= 75 else -1 if 25 <= r < 45 else 0
    add("Structure", "RSI14", tf_label, f"{r:.1f}", "Bullish zone" if s > 0 else "Bearish zone" if s < 0 else "Neutral / extreme", s, 0.10)
    recent = [e for e in events if e["idx"] >= len(dsc) - 5]
    ms = state
    if any(e["name"].startswith("CHOCH") and e["dir"] == 1 for e in recent):
        ms = 1
    elif any(e["name"].startswith("CHOCH") and e["dir"] == -1 for e in recent):
        ms = -1
    rd = ("; ".join(f"{e['name']} @ {e['level']:,.0f}" for e in recent) or "no recent break") + f" | state {({1: 'HH/HL', -1: 'LH/LL', 0: 'range'})[state]}"
    add("Structure", "Market structure (BOS/CHOCH)", tf_label, f"{({1: 'Up', -1: 'Down', 0: 'Range'})[state]}", rd, ms, 0.25)
    vw_sig, vw_txt = None, None
    if hf is not None and "vwap" in hf and ok(hf.vwap.iloc[-1]):
        px, vw, src = float(hf.close.iloc[-1]), float(hf.vwap.iloc[-1]), "VWAP (futures)"
    elif intraday and "vwap" in ds and ok(ls.vwap):
        px, vw, src = float(ls.close), float(ls.vwap), "VWAP"
    else:
        px = vw = src = None
    if vw is not None:
        vw_sig = sg(px - vw) if abs(px - vw) > 0.05 * atr_n else 0
        add("Structure", src, tf_label, f"{px:,.0f} vs {vw:,.0f}", "Price VWAP ke upar" if vw_sig > 0 else "Price VWAP ke neeche" if vw_sig < 0 else "VWAP par", vw_sig, 0.10)
    qual = [p for p in pats if p.get("qual")]
    ps_ = sg(sum(p["dir"] for p in qual))
    add("Structure", "Candle patterns (closed)", tf_label, f"{len(qual)} qualified",
        ", ".join(p["name"] for p in qual[:3]) or "koi qualified pattern nahi", ps_, 0.10)
    # ---- Positioning (F&O)
    if bu:
        add("Positioning", "Futures price/OI", "day", bu[1], "Inferred (verified intent nahi)", int(bu[0]), 0.35)
    if oiw and oiw.get("zone"):
        pu, ca = oiw["put"], oiw["call"]
        pt = 1 if pu > 0 and pu > max(ca, 0) * 1.2 else -1 if ca > 0 and ca > max(pu, 0) * 1.2 else 0
        add("Positioning", "OI change (put/call zones)", oiw.get("tf", "intraday"), f"Put zone {pu:+,.0f} / Call zone {ca:+,.0f} ({oiw.get('label', '-')})",
            "Support build ho raha (put zone)" if pt > 0 else "Resistance build ho raha (call zone)" if pt < 0 else "No clear build", pt, 0.35)
    elif oiw:
        pt = 1 if oiw["put"] > 0 and oiw["put"] > oiw["call"] else -1 if oiw["call"] > 0 and oiw["call"] > oiw["put"] else 0
        add("Positioning", "OI change at walls", "day", f"Put wall {oiw['put']:+,.0f} / Call wall {oiw['call']:+,.0f}",
            "Support build ho raha" if pt > 0 else "Resistance build ho raha" if pt < 0 else "No clear build", pt, 0.35)
    if stats:
        p = stats["pcr"]
        add("Positioning", "PCR (OI) - theory", "now", f"{p:.2f}", "Extreme high" if p > 1.3 else "Extreme low" if p < 0.7 else "Normal band",
            1 if p > 1.3 else -1 if p < 0.7 else 0, 0.10)
        near = 0.004 * spot
        s = 1 if 0 <= spot - stats["support"] <= near else -1 if 0 <= stats["resistance"] - spot <= near else 0
        add("Positioning", "Price vs OI walls", "now", f"S {stats['support']:,.0f} / R {stats['resistance']:,.0f}",
            "Support ke paas" if s > 0 else "Resistance ke paas" if s < 0 else "Walls door", s, 0.20)
    # ---- Info rows (score mein nahi)
    if ok(ls.stoch_k):
        add("Info", "Stochastic %K", tf_label, f"{ls.stoch_k:.0f}", "Overbought" if ls.stoch_k > 80 else "Oversold" if ls.stoch_k < 20 else "Mid", 0)
    if "relvol" in ds and ok(ls.relvol):
        add("Info", "Relative volume", tf_label, f"{ls.relvol:.2f}x", "Volume expansion" if ls.relvol > 1.5 else "Normal/low", 0)
    if "obv" in ds and len(ds) > 12:
        add("Info", "OBV (10 candle)", tf_label, f"{ds.obv.iloc[-1] - ds.obv.iloc[-11]:+,.0f}", "Accumulation" if ds.obv.iloc[-1] > ds.obv.iloc[-11] else "Distribution", 0)
    add("Info", "ATR14", tf_label, f"{atr_n:,.1f} ({atr_n / spot * 100:.2f}%)", "Volatility unit (SL/targets)", 0)
    if ctx.get("rv"):
        add("Info", "Historical vol (20d)", "1D", f"{ctx['rv'] * 100:.1f}%", "Realised", 0)
    if ctx.get("atm_iv"):
        add("Info", "ATM IV", "now", f"{ctx['atm_iv'] * 100:.1f}%", f"IV/RV {ctx['ratio']:.2f} - options {ctx['regime']}" if ctx.get("ratio") else "Implied", 0)
    # ---- Scores
    cats = {}
    for cat in CAT_W:
        rs = [x for x in rows if x["cat"] == cat and x["w"] > 0]
        cats[cat] = sum(x["w"] * x["sig"] for x in rs) / sum(x["w"] for x in rs) if rs else None
    av = {k: v for k, v in cats.items() if v is not None}
    DS = 100 * sum(CAT_W[k] * v for k, v in av.items()) / sum(CAT_W[k] for k in av) if av else 0.0
    view, conflict = (sg(DS) if abs(DS) >= NEED else 0), None
    if view:
        opp = [k for k, v in av.items() if v * view <= -0.4]
        if opp:
            conflict, view = "Conflicting evidence: " + ", ".join(opp) + " category ulti hai", 0
    grade = "-"
    if view:
        al = all(v * view > 0.2 for v in av.values())
        grade = "A" if (abs(DS) >= 60 and al) else "B" if abs(DS) >= 45 else "C"
    return dict(name=name, rows=rows, cats=cats, DS=DS, view=view, grade=grade, conflict=conflict, need=NEED, levels=lv,
                events=events, pats=pats, state=state, atr_n=atr_n, ph=ph, pl=pl, nclosed=len(dsc),
                close_last=float(dsc.close.iloc[-1]), ema21=float(ls.ema21), vwap_sig=vw_sig, tf=tf_label)


# ============================================================================
# TRADE PLAN + STATUS (READY / WAIT / INVALIDATED / NO TRADE)
# ============================================================================
ST_READY, ST_WAIT, ST_INV, ST_NO = "🟢 READY", "🟡 WAIT", "🔴 INVALIDATED", "⚪ NO TRADE"


def make_plan(an, sgn, S, slu, reach):
    lv = an["levels"]
    ahead = [l for l in lv if (l["price"] > S if sgn > 0 else l["price"] < S)]
    trig = min(ahead, key=lambda l: abs(l["price"] - S)) if ahead else None
    if trig and abs(trig["price"] - S) > reach:
        trig = None
    behind = [l for l in lv if (l["price"] < S if sgn > 0 else l["price"] > S)]
    pb = max(behind, key=lambda l: l["price"] * sgn) if behind else None
    piv = an["pl"] if sgn > 0 else an["ph"]
    cand = [p for _, p in piv if (p < S if sgn > 0 else p > S)]
    inv = None
    if cand:
        p = max(cand) if sgn > 0 else min(cand)
        if 0.5 * slu <= abs(S - p) <= 2.5 * slu:
            inv = p
    if inv is None:
        inv = S - sgn * slu
    recent = lambda e: e["idx"] >= an["nclosed"] - 5
    conf = [f"{e['name']} @ {e['level']:,.0f}" for e in an["events"] if e["dir"] == sgn and recent(e)]
    conf += [p["name"] for p in an["pats"] if p["dir"] == sgn and p.get("qual")]
    opp = [e for e in an["events"] if e["dir"] == -sgn and e["idx"] >= an["nclosed"] - 3]
    vw_ok = an["vwap_sig"] is None or an["vwap_sig"] * sgn >= 0
    extended = (S - an["ema21"]) * sgn > 1.5 * an["atr_n"]
    return dict(trig=trig, pb=pb, inv=inv, conf=conf, opp=opp, vwap_ok=vw_ok, extended=extended, sgn=sgn)


def plan_status(plan, ctx, hname):
    """Direction wale setup ka status (R:R/risk filters alag se NO TRADE bana sakte hain)."""
    if plan["opp"]:
        e = plan["opp"][0]
        return ST_INV, f"Structure toot gaya: {e['name']} @ {e['level']:,.0f} (setup ke against)"
    if ctx["data_state"] == "closed":
        return ST_WAIT, "Market band hai - agle session ke liye plan (trigger wahi rahega)"
    if not plan["conf"]:
        return ST_WAIT, "Confirmation (BOS/CHOCH/pattern) abhi nahi"
    if not plan["vwap_ok"]:
        return ST_WAIT, "Price VWAP ke galat side par hai"
    if plan["extended"]:
        return ST_WAIT, "Price EMA21 se bahut door (extended) - pullback ka wait"
    return ST_READY, "Confirmation mil gaya: " + "; ".join(plan["conf"][:2])


def key_level_ahead(an, S, sgn):
    """Sabse nazdeek *significant* level (3+ touches ya PDH/PDL/OR/20d/OI wall). Chhote swing levels target cap nahi karte."""
    c = [l for l in an["levels"] if (l["price"] > S if sgn > 0 else l["price"] < S)
         and (l.get("touches", 1) >= 3 or l.get("src") in ("PDH", "PDL", "OI", "20d", "OR"))]
    return min(c, key=lambda l: abs(l["price"] - S)) if c else None


def levels_consistent(c):
    """Silly-error guard: buy mein target premium entry se upar, SL neeche; credit mein buy-back target < credit < SL;
    target scenario net profit > 0 aur SL scenario net loss < 0."""
    ev = c["ev"]
    v0, t1, sl = ev["V0"], ev["t1"], ev["sl"]
    if "up" in ev:  # iron condor: t1 = market flat
        ok = t1["net"] > 0 and sl["net"] < 0
        return ok, "ok" if ok else f"flat net {t1['net']:,.0f}, breach net {sl['net']:,.0f}"
    a0, a1, a2 = abs(v0), abs(t1["V"]), abs(sl["V"])
    if v0 > 0:   # long
        order = sl["V"] < v0 < t1["V"] and (not ev.get("t2") or ev["t2"]["V"] >= t1["V"])
    else:        # credit
        order = a1 < a0 < a2
    det = f"entry {a0:.1f}, target {a1:.1f}, SL {a2:.1f}"
    if not order:
        return False, det + " - target par premium entry se behtar nahi (theta/time decay > expected move)"
    if t1["net"] <= 0:
        return False, det + f" - target par bhi net P&L {t1['net']:,.0f} (costs ke baad)"
    if sl["net"] >= 0:
        return False, det + f" - SL par loss nahi ({sl['net']:,.0f}): stop level bekaar"
    return True, det


def risk_checks(c, ctx, hname):
    """(check, ok, detail, hard)"""
    out = []
    rr = c["rr"]
    out.append(("Net R:R ≥ 1.2 (costs ke baad)", bool(rr and rr >= 1.2), f"{rr:.2f}" if rr else "N/A", True))
    out.append(("Size ≥ 1 lot (risk budget ke andar)", c["lots"] >= 1, f"{c['lots']} lot", True))
    ok_l, why_l = levels_consistent(c)
    out.append(("Levels logical (target > entry > SL, target par profit)", ok_l, why_l, True))
    if hname.startswith("Swing") and ctx["dte"] == 0:
        out.append(("Swing trade expiry-day par nahi", False, "aaj expiry - agla expiry chunein", True))
    out.append(("Data fresh", ctx["data_state"] != "stale", ctx["data_label"], True))
    liq_bad = [l for l in c["legs"] if (not l["vol"]) or (l["spread"] and l["spread"] > 3)]
    out.append(("Liquidity (volume + spread ≤3%)", not liq_bad, "ok" if not liq_bad else "volume/spread problem", ctx["is_open"]))
    if hname == "Intraday":
        late = ctx["is_open"] and (ctx["now"].hour, ctx["now"].minute) >= (15, 0)
        out.append(("Naya intraday entry 15:00 se pehle", not late, f"{ctx['now']:%H:%M}", True))
    out.append(("Expiry day nahi", ctx["dte"] != 0, "aaj expiry hai" if ctx["dte"] == 0 else "ok", False))
    return out


# ----------------------------------------------------------------------------
# Trade engine - har instrument ke liye ek hi flow
# ----------------------------------------------------------------------------
def make_hz(tf):
    mins = 15 if "15" in tf else 5
    return {"Intraday": dict(move=0.5, hold=0.25, db=0.50, ds=0.25, scale=math.sqrt(90 / mins)),
            "Swing (2-3 din / expiry)": dict(move=3.0, hold=3.0, db=0.55, ds=0.25, scale=1.0)}


ACTIONS = ["BUY CE", "SELL CE (hedged)", "BUY PE", "SELL PE (hedged)", "RANGE: Iron Condor"]


def dir_scen(S, m, sgn, wall, hold, slu):
    t1, t2, note = S + sgn * min(1.5 * slu, 1.0 * m), S + sgn * min(2.5 * slu, 1.5 * m), None
    if wall is not None:
        if sgn > 0 and wall > S:
            t1, t2 = min(t1, wall), min(t2, wall)
        if sgn < 0 and wall < S:
            t1, t2 = max(t1, wall), max(t2, wall)
        if abs(wall - S) < 0.25 * m:
            note = f"⚠️ Level {wall:.0f} bahut paas hai - move limited reh sakta hai."
    # Target/SL beech raaste mein hit hote hain: T1 ~0.6, SL ~0.4 of hold; T2 aur flat poore hold par.
    sc = {"t1": (t1, hold * 0.6), "flat": (S, hold), "sl": (S - sgn * slu, hold * 0.4)}
    if abs(t2 - t1) >= 0.15 * slu:  # T2 == T1 (dono ek hi level se capped) to duplicate dikhane ka matlab nahi
        sc = {"t1": sc["t1"], "t2": (t2, hold), "flat": sc["flat"], "sl": sc["sl"]}
    return sc, note


def equity_levels(view, S, atr_u):
    if view == 0:
        return None
    s = view
    return dict(side="BUY" if s > 0 else "SELL (intraday short, MIS)", zone=(S - s * 0.25 * atr_u, S),
                t1=S + s * 1.5 * atr_u, t2=S + s * 2.5 * atr_u, sl=S - s * 1.0 * atr_u)


def equity_from_plan(view, S, plan, an, atr_u):
    """Equity levels plan se: SL = invalidation, T1/T2 = 1.5R/2.5R ya pehla key level. (Plan aur levels alag-alag nahi honge)"""
    R = max(abs(S - plan["inv"]), 0.25 * atr_u, 1e-9)
    t1, t2, note = S + view * 1.5 * R, S + view * 2.5 * R, None
    kl = key_level_ahead(an, S, view)
    if kl is not None and abs(kl["price"] - S) < 1.5 * R:
        t1 = kl["price"]
        note = f"T1 = {kl['label']} @ {kl['price']:,.0f} (key level 1.5R se pehle aa raha hai)"
    return dict(side="BUY" if view > 0 else "SELL (intraday short, MIS)", zone=(S - view * 0.25 * atr_u, S),
                t1=t1, t2=t2, sl=plan["inv"], rr1=abs(t1 - S) / R, note=note)


def horizon_view(hname, hz, an, ctx):
    S, T, cfg, rows, strikes, stats = ctx["spot"], ctx["T"], ctx["cfg"], ctx["rows"], ctx["strikes"], ctx["stats"]
    view, DS = an["view"], an["DS"]
    atr_u = an["atr_n"] * hz["scale"]
    out = dict(name=hname, an=an, view=view, D=DS, need=an["need"], grade=an["grade"], atr=atr_u, eq=None, eq_status=None,
               actions=[], err=None, plan=None, m=None, hold=None)
    plan = None
    if view:
        plan = make_plan(an, view, S, max(atr_u, 1e-9), 3 * atr_u)
        out["plan"] = plan
        out["eq"] = equity_from_plan(view, S, plan, an, atr_u)
        if ctx["data_state"] == "stale":
            out["eq_status"] = (ST_NO, "Data stale hai")
        else:
            out["eq_status"] = plan_status(plan, ctx, hname)
            if out["eq_status"][0] == ST_READY and out["eq"]["rr1"] < 1.2:
                out["eq_status"] = (ST_WAIT, f"T1 sirf {out['eq']['rr1']:.1f}R door - breakout close ka wait, tab tak risk:reward kamzor")
    sig = ctx["atm_iv"]
    if not rows or not sig or not T:
        out["err"] = "Option data nahi (is instrument par F&O nahi hai ya ATM IV calculate nahi hua)."
        return out
    lot, regime, dte = ctx["lot"], ctx["regime"], ctx["dte"]
    tdays = T * 365
    move, hold = min(hz["move"], tdays), min(hz["hold"], 0.5 * tdays)
    m = S * sig * math.sqrt(move / 365)
    slu = min(max(atr_u, 0.3 * m), 0.6 * m)  # SL = ATR, 0.3σ-0.6σ ke beech
    out["m"], out["hold"] = m, hold
    if view:
        plan = make_plan(an, view, S, slu, 1.0 * m)
        out["plan"] = plan
    A = {k: dict(action=k, status=ST_NO, reason="", card=None, pref="", checks=[]) for k in ACTIONS}

    def card(legs, scen, notes, S0=None):
        S0 = S if S0 is None else S0
        ev = struct_eval(legs, lot, T, scen, cfg, ctx["basis"])
        if ev["V0"] < 0 and "up" not in scen and "sl" in scen:
            # Directional credit spread: SL premium-based (2x credit, width ke andar) - index level us premium par solve hota hai.
            cr0 = -ev["V0"]
            w_ = max(abs(legs[0]["K"] - legs[1]["K"]), abs(legs[-1]["K"] - legs[-2]["K"]))
            sl_prem = min(2.0 * cr0, cr0 + 0.6 * (w_ - cr0))
            sgn_ = 1 if scen["sl"][0] < S0 else -1
            Tn_ = max(T - scen["sl"][1] / 365, 1 / 525600)
            cost_ = lambda S1: -sum(l["s"] * bs(S1 + ctx["basis"], l["K"], Tn_, cfg["r"], l["iv"], l["tag"]) for l in legs)
            far = S0 - sgn_ * 3 * max(m, 1.0)
            if cost_(far) <= sl_prem:
                S_sl = far
            else:
                a_, b_ = S0, far
                for _ in range(40):
                    mid = (a_ + b_) / 2
                    a_, b_ = (mid, b_) if cost_(mid) < sl_prem else (a_, mid)
                S_sl = (a_ + b_) / 2
            scen = dict(scen, sl=(S_sl, scen["sl"][1]))
            ev = struct_eval(legs, lot, T, scen, cfg, ctx["basis"])
            notes = list(notes) + [f"Credit spread SL premium-based (buy-back ≈ {sl_prem:.1f}); index ≈ {S_sl:,.0f} par."]
        if "dn" in ev:
            ev["sl"] = min(ev["up"], ev["dn"], key=lambda x: x["net"])
        risk = -ev["sl"]["net"]
        rr = ev["t1"]["net"] / risk if risk > 0 and ev["t1"]["net"] > 0 else None
        credit = ev["V0"] < 0
        width = max(abs(legs[0]["K"] - legs[1]["K"]), abs(legs[-1]["K"] - legs[-2]["K"])) if len(legs) >= 2 else None
        maxp = -ev["V0"] * lot if credit else None
        maxl = (width + ev["V0"]) * lot if credit else ev["V0"] * lot
        size_loss = maxl if credit else max(risk, 1.0)
        lots = int((cfg["capital"] * cfg["risk"] / 100) // size_loss) if size_loss > 0 else 0
        pt1 = psl = None
        if "dn" not in ev and m > 0:
            nd = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
            pt1 = 2 * (1 - nd(abs(ev["t1"]["S"] - S0) / m))
            psl = 2 * (1 - nd(abs(ev["sl"]["S"] - S0) / m))
        pop = None
        if credit:
            ds_ = [abs(l["delta"]) for l in legs if l["s"] < 0 and l["delta"] is not None]
            if ds_:
                pop = max(0.0, 1 - sum(ds_))
        warn = list(notes)
        for l in legs:
            if not l["vol"]:
                warn.append(f"{l['K']:.0f} {l['tag']}: volume nahi dikh raha (illiquid ho sakta hai).")
            if l["spread"] and l["spread"] > 3:
                warn.append(f"{l['K']:.0f} {l['tag']}: bid-ask spread {l['spread']:.1f}% - slippage risk.")
        if dte == 0:
            warn.append("Expiry day: short options par extra margin (ELM) lagta hai; hedged spread mein bhi margin badh sakta hai.")
        if dte <= 1 and not credit:
            warn.append("Expiry ke bahut kareeb: theta tezi se kaat'ta hai - naye buy par extra savdhani.")
        return dict(legs=legs, ev=ev, rr=rr, warn=warn, maxp=maxp, maxl=maxl, lots=lots, pop=pop, credit=credit, pt1=pt1, psl=psl)

    def finish(key, c, kind, directional, extra=None, bo=None):
        c["bo"] = bo
        checks = risk_checks(c, ctx, hname) + (extra or [])
        hard = [x for x in checks if (not x[1]) and x[3]]
        pref = "preferred" if (regime in (None, "fair") or (regime == "cheap" and kind == "buy")
                               or (regime == "rich" and kind == "sell")) else "alternative (IV/RV ke hisaab se)"
        if directional and plan_status(plan, ctx, hname)[0] == ST_INV:
            # Structure toot gaya: ye risk-filter fail se zyada important info hai
            A[key].update(card=c, status=ST_INV, reason=plan_status(plan, ctx, hname)[1], pref=pref, checks=checks)
            return
        if hard:
            st_, why = ST_NO, "Risk filter fail: " + "; ".join(f"{x[0]} ({x[2]})" for x in hard)
        elif directional:
            st_, why = plan_status(plan, ctx, hname)
            if bo and st_ != ST_INV:
                st_ = ST_WAIT
                why = (f"Breakout trigger: {bo['label']} {bo['level']:,.0f} ke {'upar' if plan['sgn'] > 0 else 'neeche'} candle close; "
                       f"entry price trigger par ({bo['trig']:,.0f}) maan kar nikala hai")
            rf = [x for x in (extra or []) if not x[1]]
            if rf and st_ == ST_READY:
                st_, why = ST_WAIT, f"Key level bahut paas ({rf[0][2]}) - breakout close ka wait, phir agle level tak room dekhein"
        else:
            rangey = an["state"] == 0 and not an["events"] and (ctx["adx_d"] is None or ctx["adx_d"] < 25)
            if ctx["data_state"] == "closed":
                st_, why = ST_WAIT, "Market band hai - agle session ke liye plan"
            elif rangey:
                st_, why = ST_READY, "Range structure confirm: koi BOS/CHOCH nahi, ADX low"
            else:
                st_, why = ST_WAIT, "Range confirm nahi (structure break ya ADX high)"
        A[key].update(card=c, status=st_, reason=why, pref=pref, checks=checks)

    lbl = {1: "bullish", -1: "bearish", 0: "neutral"}[view]
    if view != 0:
        tb, ts_, sgn = ("CE", "PE", 1) if view > 0 else ("PE", "CE", -1)
        kb, ks = ("BUY CE", "SELL PE (hedged)") if view > 0 else ("BUY PE", "SELL CE (hedged)")
        kl = key_level_ahead(an, S, sgn)
        bo = None
        if kl is not None and abs(kl["price"] - S) < 1.5 * slu:
            # Key level 1.5R se kam door: seedha wahan se target cap karne ki jagah breakout trade plan karte hain
            nxt = key_level_ahead(an, kl["price"], sgn)
            trig_s = kl["price"] + sgn * 0.1 * an["atr_n"]
            sl_d = max(0.5 * slu, 0.6 * an["atr_n"])
            scen, wnote = dir_scen(trig_s, m, sgn, nxt["price"] if nxt else None, hold, sl_d)
            bo = dict(level=kl["price"], label=kl["label"], trig=trig_s, nxt=nxt)
            room = []
        else:
            wall = kl["price"] if kl else None
            room = [("Room ≥ 1R agle key level tak", True if kl is None else abs(wall - S) >= 1.0 * slu,
                     "koi key level nahi" if kl is None else f"{kl['label']} @ {wall:,.0f} ({abs(wall - S) / slu:.1f}R)", False)]
            scen, wnote = dir_scen(S, m, sgn, wall, hold, slu)
        Kb = pick_by_delta(rows, strikes, tb, hz["db"])
        lg = make_leg(rows, Kb, tb, 1) if Kb else None
        if lg:
            legs_b = shift_legs([lg], S, bo["trig"], T, cfg, ctx["basis"]) if bo else [lg]
            finish(kb, card(legs_b, scen, [wnote] if wnote else [], bo["trig"] if bo else None), "buy", True, room, bo)
        else:
            A[kb]["reason"] = "Suitable strike/price nahi mila."
        legs, notes = credit_spread(ctx, ts_, hz["ds"])
        if legs:
            legs_s = shift_legs(legs, S, bo["trig"], T, cfg, ctx["basis"]) if bo else legs
            finish(ks, card(legs_s, scen, notes, bo["trig"] if bo else None), "sell", True, room, bo)
        else:
            A[ks]["reason"] = notes
    else:
        if regime == "cheap":
            A["RANGE: Iron Condor"]["reason"] = "Options sasta (IV < realised vol) - premium bechne ka edge nahi."
        else:
            ps, pn = credit_spread(ctx, "PE", hz["ds"])
            cs, cn = credit_spread(ctx, "CE", hz["ds"])
            if ps and cs:
                scen = {"t1": (S, hold), "flat": (S, hold), "up": (S + m, hold * 0.5), "dn": (S - m, hold * 0.5)}
                finish("RANGE: Iron Condor", card(ps + cs, scen, pn + cn + ["Range view: spot short strikes ke beech rehna chahiye."]), "sell", False)
            else:
                A["RANGE: Iron Condor"]["reason"] = "Dono spreads ka data nahi mila."
    for k in ACTIONS:
        if not A[k]["card"] and not A[k]["reason"]:
            A[k]["reason"] = (f"View {lbl} hai - ye side ke against." if view != 0 else
                              (an["conflict"] or f"Direction clear nahi (score {DS:+.0f}, chahiye ±{an['need']:g})."))
    out["actions"] = [A[k] for k in ACTIONS]
    return out


# ============================================================================
# Rendering helpers
# ============================================================================
VIEW_LBL = {1: "UPSIDE ⬆️", -1: "DOWNSIDE ⬇️", 0: "SIDEWAYS ↔️"}


def legs_text(c):
    return " / ".join(f"{'BUY' if l['s'] > 0 else 'SELL'} {l['K']:.0f} {l['tag']}" for l in c["legs"])


def action_row(a):
    c = a["card"]
    note = (a["reason"] or "")[:90]
    if not c or a["status"] in (ST_NO, ST_INV):   # numbers sirf READY/WAIT par - warna silly levels dikhte hain
        return {"Action": legs_text(c) if c else a["action"], "Status": a["status"], "Entry": "-", "Target": "-", "Stop loss": "-", "Note": note}
    ev, cr = c["ev"], c["credit"]
    return {"Action": legs_text(c), "Status": a["status"],
            "Entry": f"credit ₹{abs(ev['V0']):.1f}" if cr else f"buy ₹{ev['V0']:.1f}",
            "Target": f"buy-back ₹{abs(ev['t1']['V']):.1f}" if cr else f"sell ₹{ev['t1']['V']:.1f}",
            "Stop loss": f"buy-back ₹{abs(ev['sl']['V']):.1f}" if cr else f"exit ₹{ev['sl']['V']:.1f}", "Note": note}


def margin_check(kite, legs, lot, dex, nlots):
    params = [dict(exchange=dex, tradingsymbol=l["sym"], transaction_type="BUY" if l["s"] > 0 else "SELL",
                   variety="regular", product="NRML", order_type="MARKET", quantity=int(lot * nlots)) for l in legs]
    res = kite.basket_order_margins(params)  # sirf margin calculate karta hai, order place nahi hota
    fin = res.get("final") or res.get("initial") or {}
    return fin.get("total")


def plan_lines(plan, an):
    if not plan:
        return
    up = plan["sgn"] > 0
    t = plan["trig"]
    if t:
        st.markdown(f"**Entry trigger:** {'close above' if up else 'close below'} {t['price']:,.0f} ({t['label']}) breakout" +
                    (f", ya pullback {plan['pb']['price']:,.0f} ({plan['pb']['label']}) par {'bullish' if up else 'bearish'} confirmation" if plan["pb"] else ""))
    elif plan["pb"]:
        st.markdown(f"**Entry trigger:** pullback {plan['pb']['price']:,.0f} ({plan['pb']['label']}) par {'bullish' if up else 'bearish'} confirmation")
    st.markdown(f"**Invalidation:** {'close below' if up else 'close above'} {plan['inv']:,.0f} (swing/ATR) ya opposite BOS/CHOCH")


def render_card(a, ctx, hkey, an):
    c = a["card"]
    ev, cr, lot = c["ev"], c["credit"], ctx["lot"]
    no_trade = a["status"] in (ST_NO, ST_INV)
    with st.expander(f"{a['status']}  {legs_text(c)}" + ("  (sirf reference - trade nahi)" if no_trade else ""), expanded=a["status"] == ST_READY):
        if no_trade:
            st.error(f"{a['status']}: " + a["reason"] + " - neeche ke numbers sirf reference hain, entry/target/SL ke taur par use na karein.")
        else:
            st.caption(f"{a['reason']} | {a['pref']}")
        labs = ("Net credit (sell price)", "Buy-back target T1", "Buy-back stop loss") if cr else \
               ("Entry (buy price)", "Target sell price T1", "Stop-loss price")
        x, y, z = st.columns(3)
        x.metric(labs[0], f"₹{abs(ev['V0']):.2f}")
        y.metric(labs[1], f"₹{abs(ev['t1']['V']):.2f}", f"index {ev['t1']['S']:,.0f}")
        z.metric(labs[2], f"₹{abs(ev['sl']['V']):.2f}", f"index {ev['sl']['S']:,.0f}")
        st.caption(f"Quote: {ctx['data_label']} | Expiry {ctx['expiry_txt']}")
        if c.get("bo"):
            b_ = c["bo"]
            st.info(f"Breakout plan: {b_['label']} {b_['level']:,.0f} ke paas hai, isliye entry/targets trigger {b_['trig']:,.0f} par "
                    f"{'agle key level ' + format(b_['nxt']['price'], ',.0f') + ' tak' if b_['nxt'] else 'ATR-based'} nikale hain.")
        rows_ = [("Target 1", ev["t1"]), ("Target 2", ev.get("t2")), ("Market flat (time decay)", ev["flat"]), ("Stop loss", ev["sl"])]
        st.dataframe(pd.DataFrame([{"Scenario": n, "Index": round(v["S"], 1), "Structure price": round(abs(v["V"]), 2),
                                    "Net P&L/lot": round(v["net"])} for n, v in rows_ if v]), hide_index=True)
        txt = f"Lot {lot} | Max loss ≈ ₹{c['maxl']:,.0f}" + (f" | Max profit ≈ ₹{c['maxp']:,.0f}" if c["maxp"] else " (premium)")
        txt += f" | Net R:R {c['rr']:.2f}" if c["rr"] else " | Net R:R N/A"
        st.markdown(txt)
        st.markdown(f"📐 Suggested size: **{c['lots']} lot** (capital × risk% ÷ max loss)" +
                    (f" | short strike OTM expire hone ki approx probability ≈ {c['pop'] * 100:.0f}%" if c["pop"] else ""))
        st.dataframe(pd.DataFrame([{"Risk filter": n, "Result": "✅ pass" if ok_ else ("❌ FAIL" if hard else "⚠️ note"), "Detail": d_}
                                   for n, ok_, d_, hard in a["checks"]]), hide_index=True)
        if c.get("pt1") is not None:
            st.caption(f"Baseline touch probability (edge ke bina, random-walk): T1 ≈ {c['pt1'] * 100:.0f}%, SL ≈ {c['psl'] * 100:.0f}% - "
                       "signal ka edge isse upar hona chahiye. Signal strength ≠ profit probability.")
        view = an["view"]
        sup = [r for r in an["rows"] if r["w"] > 0 and view and r["sig"] * view > 0]
        con = [r for r in an["rows"] if r["w"] > 0 and view and r["sig"] * view < 0]
        with st.expander("Evidence: supporting / conflicting"):
            st.markdown("**Supporting:** " + (", ".join(f"{r['name']} ({r['tf']})" for r in sup) or "-"))
            st.markdown("**Conflicting:** " + (", ".join(f"{r['name']} ({r['tf']})" for r in con) or "koi nahi"))
        for w in c["warn"]:
            st.warning(w)
        if c["lots"] == 0:
            st.warning("Risk budget is trade ke max loss se chhota hai - size 0 lot. Risk % badhayein ya hedged spread chunein.")
        st.caption("Unvalidated, hypothetical scenario - backtest nahi hua. Orders app se place nahi hote.")
        mk = f"margin_{hkey}_{a['action']}"
        if st.button("💰 Margin check (Kite)", key="b_" + mk):
            try:
                tot = margin_check(ctx["kite"], c["legs"], lot, ctx["dex"], max(1, c["lots"]))
                st.session_state[mk] = f"≈ ₹{tot:,.0f} ({max(1, c['lots'])} lot)" if tot else "Margin data nahi mila."
            except Exception as e:
                st.session_state[mk] = f"Margin check fail ({type(e).__name__}) - broker terminal mein check karein."
        if st.session_state.get(mk):
            st.info("Estimated margin " + str(st.session_state[mk]) + " - order se pehle Zerodha mein confirm karein.")


def matrix_df(an):
    v = an["view"]
    out = []
    for r in an["rows"]:
        if r["w"] <= 0:
            sig = "info"
        elif v:
            sig = "✅ supports" if r["sig"] * v > 0 else "❌ opposes" if r["sig"] * v < 0 else "⚪ neutral"
        else:
            sig = "▲" if r["sig"] > 0 else "▼" if r["sig"] < 0 else "•"
        out.append({"Category": r["cat"], "Indicator": r["name"], "TF": r["tf"], "Value": r["val"], "Reading": r["reading"], "Signal": sig})
    return pd.DataFrame(out)


def render_hz(res, ctx, hkey, spot):
    if res is None:
        st.info("Is horizon ke liye historical data kam hai - signal generate nahi ho sakta.")
        return
    an = res["an"]
    cs = " | ".join(f"{k} {v:+.2f}" if v is not None else f"{k} n/a" for k, v in an["cats"].items())
    st.markdown(f"**View: {VIEW_LBL[res['view']]}**  |  Score {res['D']:+.0f} / ±100 (signal ≥ {res['need']:g})  |  Grade **{res['grade']}**")
    st.caption(f"Categories: {cs}. Signal strength ≠ profit probability.")
    if an["conflict"]:
        st.warning(an["conflict"])
    if res["eq"] and not ctx.get("is_index"):
        e = res["eq"]
        stt = res["eq_status"]
        st.markdown(f"**Equity (cash) - {e['side']}** · {stt[0]}")
        st.caption(stt[1])
        x, y, z = st.columns(3)
        x.metric("Zone", f"{min(e['zone']):,.2f} - {max(e['zone']):,.2f}")
        y.metric("Target 1 / 2", f"{e['t1']:,.2f} / {e['t2']:,.2f}")
        z.metric("Stop loss", f"{e['sl']:,.2f}")
        st.caption(f"Levels: SL = invalidation (structure), T1/T2 = 1.5R/2.5R ya pehla key level | T1 reward ≈ {e['rr1']:.1f}R - hypothetical."
                   + (f" {e['note']}" if e.get("note") else ""))
    elif not ctx.get("is_index") and not res["eq"]:
        st.caption("Equity: abhi koi clear direction nahi - wait karein.")
    if res["plan"]:
        plan_lines(res["plan"], an)
    if res["err"]:
        st.info(res["err"])
    else:
        st.caption(f"Expected 1σ move ≈ ±{res['m']:,.0f} pts | hold ≈ {res['hold']:.2f} din")
        st.dataframe(pd.DataFrame([action_row(a) for a in res["actions"]]), hide_index=True)
        for a in res["actions"]:
            if a["card"]:
                render_card(a, ctx, hkey, an)
            else:
                st.caption(f"⚪ {a['action']}: {a['reason']}")
    with st.expander("🧠 Indicator matrix & logic"):
        st.dataframe(matrix_df(an), hide_index=True)
        st.caption("Har category ke andar weighted average (max ±1): correlated indicators score inflate nahi karte. "
                   "Regime 25% / Structure 25% / Positioning 20% (jo available ho unmein normalise).")
    with st.expander("🔎 Patterns & structure events (closed candles)"):
        if an["pats"] or an["events"]:
            recs = [dict(Pattern=p["name"], TF=p["tf"], Level=round(p["level"], 1), Time=str(to_ist(p["ts"]))[:16], Status=p["status"]) for p in an["pats"]]
            recs += [dict(Pattern=e["name"], TF=an["tf"], Level=round(e["level"], 1), Time=str(to_ist(e["ts"]))[:16], Status="confirmed (candle close)") for e in an["events"]]
            st.dataframe(pd.DataFrame(recs), hide_index=True)
        else:
            st.caption("Abhi koi pattern/structure break detect nahi hua.")


# ============================================================================
# Chart - pure SVG (koi extra library nahi: Altair/Plotly Python 3.14 par toot sakte hain)
# ============================================================================
def make_chart_svg(d, levels, show_ema, show_vwap, show_levels, daily, n=90, W=900, H=430):
    c = d.tail(n).reset_index(drop=True)
    if len(c) < 5:
        return None
    UP, DOWN, AMB, GREY, TXT, GRID = "#79C99A", "#E58B8B", "#E9C778", "#9CA8B6", "#EDF1F5", "#303945"
    kc = {"Support": UP, "Resistance": DOWN, "Entry": AMB, "Target": UP, "Stop": DOWN}
    labels = dts_ist(c.date).dt.strftime("%d %b" if daily else "%d %H:%M").tolist()
    lo, hi = float(c.low.min()), float(c.high.max())
    pad_v = 0.012 * hi
    lv = [l for l in (levels or []) if show_levels and lo - pad_v <= l["price"] <= hi + pad_v]
    for l in lv:
        lo, hi = min(lo, l["price"]), max(hi, l["price"])
    pad = (hi - lo) * 0.05 or 1.0
    y0, y1 = lo - pad, hi + pad
    ml, mr, mt, mb = 64, 130, 10, 34
    pw, ph = W - ml - mr, H - mt - mb
    step = pw / len(c)
    X = lambda i: ml + (i + 0.5) * step
    Y = lambda p: mt + (y1 - p) / (y1 - y0) * ph
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="sans-serif" font-size="11">',
           f'<rect width="{W}" height="{H}" fill="#14181E"/>']
    for k in range(5):
        p = y0 + (y1 - y0) * k / 4
        out.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{Y(p):.1f}" y2="{Y(p):.1f}" stroke="{GRID}" stroke-width="0.6"/>')
        out.append(f'<text x="{ml - 6}" y="{Y(p) + 4:.1f}" fill="{GREY}" text-anchor="end">{p:,.0f}</text>')
    every = max(1, len(c) // 7)
    for i in range(0, len(c), every):
        out.append(f'<text x="{X(i):.1f}" y="{H - 12}" fill="{GREY}" text-anchor="middle">{html.escape(labels[i])}</text>')
    bw = max(1.5, step * 0.6)
    for i, r in c.iterrows():
        col = UP if r.close >= r.open else DOWN
        out.append(f'<line x1="{X(i):.1f}" x2="{X(i):.1f}" y1="{Y(r.high):.1f}" y2="{Y(r.low):.1f}" stroke="{col}" stroke-width="1"/>')
        top, bot = Y(max(r.open, r.close)), Y(min(r.open, r.close))
        out.append(f'<rect x="{X(i) - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(1.0, bot - top):.1f}" fill="{col}"/>')

    def line(col_name, color, dash=""):
        if col_name not in c:
            return
        path, pen = [], False
        for i, v in enumerate(c[col_name].tolist()):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                pen = False
                continue
            path.append(f'{"L" if pen else "M"}{X(i):.1f},{Y(v):.1f}')
            pen = True
        if path:
            out.append(f'<path d="{" ".join(path)}" fill="none" stroke="{color}" stroke-width="1.3"' + (f' stroke-dasharray="{dash}"' if dash else "") + '/>')

    if show_ema:
        line("ema21", AMB)
    if show_vwap:
        line("vwap", GREY, "5,3")
    last_y = -99.0
    for l in sorted(lv, key=lambda x: -x["price"]):
        col = kc.get(l.get("kind"), GREY)
        y = Y(l["price"])
        out.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="1" stroke-dasharray="5,4" opacity="0.8"/>')
        ty = max(y + 4, last_y + 12)
        last_y = ty
        out.append(f'<text x="{ml + pw + 6}" y="{ty:.1f}" fill="{col}">{html.escape(str(l.get("label", ""))[:16])} {l["price"]:,.0f}</text>')
    lp = float(c.close.iloc[-1])
    out.append(f'<text x="{ml + 4}" y="{mt + 12}" fill="{TXT}">Last {lp:,.2f}</text>')
    out.append("</svg>")
    return "".join(out)


# ============================================================================
# GOOGLE SHEET (Apps Script webhook): signal journal, outcomes, OI snapshots, backtest summaries.
# Koi extra Python library nahi - sirf requests. Setup: apps_script.gs (Sheet > Extensions > Apps Script).
# ============================================================================
SHEET_URL, SHEET_TOKEN = secret("SHEET_WEBHOOK_URL"), secret("SHEET_TOKEN")


def sheet_ok():
    return bool(SHEET_URL and SHEET_TOKEN)


def sheet_call(payload, timeout=8):
    r = requests.post(SHEET_URL, data=json.dumps(dict(payload, token=SHEET_TOKEN)),
                      headers={"Content-Type": "text/plain"}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(str(j.get("error", "sheet error"))[:80])
    return j


def sheet_append(tab, headers, rows, timeout=8):
    rows = [["" if v is None else str(v) for v in r] for r in rows]
    return sheet_call(dict(action="append", tab=tab, headers=headers, rows=rows), timeout)


@st.cache_resource
def sheet_err():
    return {}


def sheet_append_bg(tab, headers, rows):
    """Background thread mein save: dashboard ka render Sheet ki slowness par nahi rukta."""
    err = sheet_err()

    def _run():
        try:
            sheet_append(tab, headers, rows, timeout=20)
            err.pop("last", None)
        except Exception as e:
            err["last"] = f"{tab}: {type(e).__name__}"
    threading.Thread(target=_run, daemon=True).start()


def sheet_read(tab, limit=3000, timeout=8):
    vals = sheet_call(dict(action="read", tab=tab, limit=limit), timeout).get("values") or []
    if len(vals) < 2:
        return pd.DataFrame(columns=vals[0] if vals else [])
    return pd.DataFrame(vals[1:], columns=vals[0])


SIG_HEADERS = ["id", "ts", "instrument", "token", "tf", "horizon", "view", "status", "grade", "score", "spot",
               "trigger", "inv", "t1", "t2", "rr1", "cats", "option", "opt_entry", "opt_target", "opt_sl", "expiry"]
OUT_HEADERS = ["id", "evaluated_at", "outcome", "R", "bars", "mfe", "mae"]


@st.cache_resource
def logged_ids():
    return set()


@st.cache_data(ttl=600, show_spinner=False)
def sheet_signal_ids():
    try:
        df = sheet_read("signals", 800, 5)
        return set(df["id"].tolist()) if "id" in df else set()
    except Exception:
        return set()


def log_ready_signals(res, sym, tok, tf, now, expiry):
    """READY directional setups ko journal mein save karta hai (ek instrument/horizon/direction/din = ek entry)."""
    if not sheet_ok():
        return 0
    known, rows = logged_ids() | sheet_signal_ids(), []
    for hname, r in res.items():
        if not r or not r.get("view") or not r.get("eq") or not r.get("eq_status") or r["eq_status"][0] != ST_READY:
            continue
        sid = f"{sym}|{hname[:3]}|{now:%Y-%m-%d}|{r['view']}"
        if sid in known:
            continue
        e, pl, an = r["eq"], r["plan"], r["an"]
        opt = next((a for a in r["actions"] if a["card"] and a["status"] == ST_READY), None)
        oe = ot = os_ = ""
        if opt:
            ev = opt["card"]["ev"]
            oe, ot, os_ = round(abs(ev["V0"]), 2), round(abs(ev["t1"]["V"]), 2), round(abs(ev["sl"]["V"]), 2)
        rows.append([sid, f"{now:%Y-%m-%d %H:%M:%S}", sym, tok, tf if hname == "Intraday" else "day", hname, r["view"],
                     r["eq_status"][0], r["grade"], round(r["D"], 1), round(an["close_last"], 2),
                     round(pl["trig"]["price"], 2) if pl and pl.get("trig") else "", round(e["sl"], 2), round(e["t1"], 2),
                     round(e["t2"], 2), round(e["rr1"], 2),
                     json.dumps({k: round(v, 2) for k, v in an["cats"].items() if v is not None}),
                     legs_text(opt["card"]) if opt else "", oe, ot, os_, f"{expiry:%Y-%m-%d}" if expiry is not None else ""])
        known.add(sid)
    if not rows:
        return 0
    sheet_append_bg("signals", SIG_HEADERS, rows)
    for rw in rows:
        logged_ids().add(rw[0])
    return len(rows)


# ============================================================================
# TRADE SIMULATION (backtest aur live outcome tracking dono yahi use karte hain)
# ============================================================================
def simulate_trade(d, j0, entry, view, inv, t1, t2, max_bars, intraday, cost_bps=5.0, live_now=None):
    """Bar j0 se aage walk: SL / T1 pehle kaun hit hota hai. Same bar mein dono -> SL pehle (conservative).
    Intraday: same session ke close par time-exit. Return None agar trade valid nahi."""
    n = len(d)
    R = abs(entry - inv)
    if j0 >= n or R <= 1e-9 or (entry - inv) * view <= 0:
        return None
    if (t1 - entry) * view <= 0:
        t1 = entry + view * 1.5 * R
    hi, lo, op, cl = d.high.values, d.low.values, d.open.values, d.close.values
    days = dts_ist(d.date).dt.date.values if intraday else None
    mfe = mae = 0.0
    outcome = exit_px = None
    last_j, t2hit = j0 - 1, False
    for j in range(j0, min(n, j0 + max_bars)):
        if intraday and days[j] != days[j0]:
            break
        last_j = j
        fav = (hi[j] - entry) if view > 0 else (entry - lo[j])
        adv = (entry - lo[j]) if view > 0 else (hi[j] - entry)
        mfe, mae = max(mfe, fav / R), max(mae, adv / R)
        hit_sl = lo[j] <= inv if view > 0 else hi[j] >= inv
        hit_t1 = hi[j] >= t1 if view > 0 else lo[j] <= t1
        if hit_sl:
            gap = (op[j] - inv) * view <= 0
            exit_px, outcome = (op[j] if gap else inv), "SL"
            break
        if hit_t1:
            gap = (op[j] - t1) * view >= 0
            exit_px, outcome = (op[j] if gap else t1), "T1"
            t2hit = hi[j] >= t2 if view > 0 else lo[j] <= t2
            break
    if last_j < j0:
        return None
    done = True
    if outcome is None:
        exit_px, outcome = cl[last_j], "TIME"
        if live_now is not None:
            if intraday:
                done = days[last_j] < live_now.date() or (live_now.hour, live_now.minute) >= (15, 30)
            else:
                done = (last_j - j0 + 1) >= max_bars
            if not done:
                outcome = "OPEN"
    Rm = (exit_px - entry) * view / R - cost_bps / 1e4 * entry / R
    return dict(outcome=outcome, R=float(Rm), bars=last_j - j0 + 1, mfe=float(mfe), mae=float(mae), done=done, t2=bool(t2hit))


def eval_logged_signal(kite, s, now):
    """Sheet ke ek signal ko Kite ke baad ke candles se evaluate karta hai. None = abhi data/window complete nahi."""
    tok, view = int(float(s["token"])), int(float(s["view"]))
    ts = pd.Timestamp(s["ts"]).tz_localize(IST)
    intraday = s["horizon"].startswith("Intra")
    interval = s["tf"] if intraday else "day"
    frm = (ts - timedelta(days=1)).tz_localize(None)
    df = pd.DataFrame(kite.historical_data(tok, frm, now.replace(tzinfo=None), interval))
    if df.empty:
        return None
    dts = dts_ist(df.date)
    d = df[(dts > ts).values].reset_index(drop=True)
    if d.empty:
        return None
    r = simulate_trade(d, 0, float(s["spot"]), view, float(s["inv"]), float(s["t1"]), float(s["t2"]),
                       30 if intraday else 10, intraday, 5.0, live_now=now)
    return r if r and r["done"] else None


# ============================================================================
# BACKTEST: wahi analyze_horizon / make_plan / plan_status, historical candles par replay.
# Sirf underlying-based logic validate hota hai (Regime + Structure). Positioning/news/global ki purani
# history Kite nahi deta, isliye wo backtest mein shamil nahi.
# ============================================================================
@st.cache_data(ttl=3600, show_spinner=False)
def bt_history(_kite, token, interval, days):
    chunk = {"day": 1800, "15minute": 190, "5minute": 90, "60minute": 380}.get(interval, 90)
    end, remaining, parts = datetime.now(IST).replace(tzinfo=None), int(days), []
    while remaining > 0:
        span = min(chunk, remaining)
        start = end - timedelta(days=span)
        p = pd.DataFrame(_kite.historical_data(token, start, end, interval))
        if not p.empty:
            parts.append(p)
        end, remaining = start, remaining - span
        time.sleep(0.35)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts[::-1]).drop_duplicates("date").sort_values("date").reset_index(drop=True)


def bt_core(ds, dd, tf_label, intraday, sym, p):
    """ds/dd: indicators ke saath poore frames (indicators causal hain, isliye slice par lookahead nahi).
    Har step par: analysis -> plan -> status. Entry agle bar ke open par; ek time par ek position."""
    n = len(ds)
    dates = dts_ist(ds.date)
    days = dates.dt.date.values
    dd_days = dts_ist(dd.date).dt.date.values
    scale = math.sqrt(90 / (15 if "15" in tf_label else 5)) if intraday else 1.0
    trades, end_idx = [], {"READY": -1, "VIEW": -1}
    for i in range(p["start"], n - 1, p["step"]):
        if intraday and (dates.iloc[i].hour, dates.iloc[i].minute) >= (15, 0):
            continue
        if intraday and days[i + 1] != days[i]:
            continue
        if i <= min(end_idx.values()):
            continue
        dss = ds.iloc[max(0, i + 1 - 300): i + 1]
        if intraday:
            kd = int(np.searchsorted(dd_days, days[i], side="left"))
            dds = dd.iloc[max(0, kd - 260): kd]
        else:
            dds = ds.iloc[max(0, i + 1 - 260): i + 1]
        if len(dds) < 60 or len(dss) < 60:
            continue
        S = float(dss.close.iloc[-1])
        ctx = dict(spot=S, stats=None, bu=None, oiw=None, glb=None, news=None, now=dates.iloc[i].to_pydatetime(),
                   is_open=False, rv=None, atm_iv=None, ratio=None, regime=None, data_state="live")
        try:
            an = analyze_horizon("Intraday" if intraday else "Swing", dss, dds, None, ctx, tf_label, intraday)
        except Exception:
            continue
        view = an["view"]
        if not view:
            continue
        atr_u = an["atr_n"] * scale
        plan = make_plan(an, view, S, max(atr_u, 1e-9), 3 * atr_u)
        eq = equity_from_plan(view, S, plan, an, atr_u)
        st_, _ = plan_status(plan, ctx, "Intraday")
        modes = ["VIEW"] + (["READY"] if (st_ == ST_READY and eq["rr1"] >= 1.2) else [])
        for mode in modes:
            if i <= end_idx[mode]:
                continue
            r = simulate_trade(ds, i + 1, float(ds.open.iloc[i + 1]), view, eq["sl"], eq["t1"], eq["t2"],
                               30 if intraday else 10, intraday, p["cost_bps"])
            if not r:
                continue
            end_idx[mode] = i + r["bars"]
            trades.append(dict(sym=sym, tf=tf_label, mode=mode, ts=dates.iloc[i], view=view, grade=an["grade"], DS=an["DS"],
                               c_Regime=an["cats"].get("Regime"), c_Structure=an["cats"].get("Structure"), rr1=eq["rr1"], **r))
    return trades


def run_backtest(kite, insts, horizons, tf, days_i, days_d, step_i, cost_bps, progress=None):
    trades, errs = [], []
    for k, (sym, tok) in enumerate(insts):
        try:
            hd = bt_history(kite, int(tok), "day", days_d + 320)
            dd = add_indicators(hd) if len(hd) >= 260 else None
            if dd is None:
                errs.append((sym, "daily history kam"))
            if dd is not None and "Swing" in horizons:
                start = max(210, len(dd) - int(days_d * 0.68))
                trades += bt_core(dd, dd, "1D", False, sym, dict(start=start, step=1, cost_bps=cost_bps))
            if dd is not None and "Intraday" in horizons:
                hi_ = bt_history(kite, int(tok), tf, days_i + 25)
                if len(hi_) >= 400:
                    di = add_indicators(hi_, True)
                    cutoff = dts_ist(di.date).iloc[-1] - timedelta(days=days_i)
                    first = int((dts_ist(di.date) < cutoff).sum())
                    trades += bt_core(di, dd, tf, True, sym, dict(start=max(120, first), step=step_i, cost_bps=cost_bps))
                else:
                    errs.append((sym, "intraday history kam"))
        except Exception as e:
            errs.append((sym, type(e).__name__))
        if progress:
            progress((k + 1) / len(insts), f"{sym} done ({k + 1}/{len(insts)})")
    return pd.DataFrame(trades), errs


def bt_stats(t):
    if t is None or len(t) == 0:
        return dict(n=0)
    w, l = t[t.R > 0], t[t.R <= 0]
    eq = t.sort_values("ts").R.cumsum()
    return dict(n=len(t), hit_T1=float((t.outcome == "T1").mean() * 100), win=float((t.R > 0).mean() * 100), avgR=float(t.R.mean()),
                PF=float(w.R.sum() / abs(l.R.sum())) if len(l) and l.R.sum() != 0 else float("inf"),
                maxDD_R=float((eq.cummax() - eq).max()), bars=float(t.bars.mean()))


def bt_tables(t):
    """Summary, grade, threshold, category-agree, train/test tables."""
    out = {}
    out["summary"] = pd.DataFrame([dict(mode=m, tf=tf_, **bt_stats(g)) for (m, tf_), g in t.groupby(["mode", "tf"])]).round(2)
    rd = t[t["mode"] == "READY"]
    out["grade"] = pd.DataFrame([dict(grade=g, **bt_stats(x)) for g, x in rd.groupby("grade")]).round(2) if len(rd) else pd.DataFrame()
    out["thresh"] = pd.DataFrame([dict(min_score=th, **bt_stats(rd[rd.DS.abs() >= th])) for th in (35, 45, 60)]).round(2) if len(rd) else pd.DataFrame()
    rows = []
    for cat in ("Regime", "Structure"):
        col = f"c_{cat}"
        agree = (t[col].fillna(0) * t.view) > 0.2
        rows.append(dict(category=cat, agree_n=int(agree.sum()), agree_avgR=t[agree].R.mean() if agree.any() else None,
                         other_n=int((~agree).sum()), other_avgR=t[~agree].R.mean() if (~agree).any() else None))
    out["cats"] = pd.DataFrame(rows).round(2)
    if len(rd) >= 10:
        rs = rd.sort_values("ts")
        cut = int(len(rs) * 0.6)
        out["split"] = pd.DataFrame([dict(part="Train (pehle 60%)", **bt_stats(rs.iloc[:cut])),
                                     dict(part="Test (baad ke 40%)", **bt_stats(rs.iloc[cut:]))]).round(2)
    else:
        out["split"] = pd.DataFrame()
    return out


# ============================================================================
# INTRADAY OI: (1) memory/Sheet snapshots, (2) per-contract 15m history (oi=True)
# ============================================================================
@st.cache_resource
def snap_store():
    return {}


SNAPS = snap_store()


def snap_record(und, expiry, spot, rows, now):
    """Poori chain window ka OI snapshot (har >=5 min). Sheet configured ho to wahan bhi save."""
    day = str(now.date())
    for k in [k for k in SNAPS if k[2] != day]:
        del SNAPS[k]
    key = (und, str(expiry.date()), day)
    lst = SNAPS.setdefault(key, [])
    if lst and (now - lst[-1]["ts"]).total_seconds() < 300:
        return False
    oi = {float(k): [num(r.get("CE OI")), num(r.get("PE OI"))] for k, r in rows.items()}
    lst.append(dict(ts=now, spot=float(spot), oi=oi))
    if sheet_ok():
        try:
            sheet_append_bg("oi_snap", ["ts", "und", "expiry", "spot", "oi_json"],
                         [[f"{now:%Y-%m-%d %H:%M:%S}", und, str(expiry.date()), round(spot, 2),
                           json.dumps({str(k): v for k, v in oi.items()})]])
        except Exception:
            pass
    return True


@st.cache_data(ttl=600, show_spinner=False)
def snaps_from_sheet(und, exp_s, day_s):
    out = []
    try:
        df = sheet_read("oi_snap", 900, 5)
        for _, r in df.iterrows():
            if r.get("und") == und and r.get("expiry") == exp_s and str(r.get("ts", "")).startswith(day_s):
                out.append(dict(ts=pd.Timestamp(r["ts"]).tz_localize(IST).to_pydatetime(), spot=float(r["spot"]),
                                oi={float(k): v for k, v in json.loads(r["oi_json"]).items()}))
    except Exception:
        pass
    return out


def snap_list(und, expiry, now):
    key = (und, str(expiry.date()), str(now.date()))
    lst = list(SNAPS.get(key, []))
    if sheet_ok():
        seen = {x["ts"].replace(microsecond=0) for x in lst}
        lst += [x for x in snaps_from_sheet(*key) if x["ts"].replace(microsecond=0) not in seen]
    return sorted(lst, key=lambda x: x["ts"])


def snap_delta(snaps, rows, now):
    """Baseline: ~60 min purana snapshot (>=45 min), warna aaj ka pehla snapshot (>=15 min purana). Per-strike [dCE, dPE]."""
    if not snaps:
        return None
    cand = [s for s in snaps if (now - s["ts"]).total_seconds() >= 45 * 60]
    base = min(cand, key=lambda s: abs((now - s["ts"]).total_seconds() - 3600)) if cand else snaps[0]
    if (now - base["ts"]).total_seconds() < 15 * 60:
        return None
    dm = {}
    for k, r in rows.items():
        b = base["oi"].get(float(k))
        ce, pe = num(r.get("CE OI")), num(r.get("PE OI"))
        if b:
            dm[float(k)] = [None if (ce is None or b[0] is None) else ce - b[0], None if (pe is None or b[1] is None) else pe - b[1]]
    return dict(delta=dm, label=f"since {base['ts']:%H:%M} (snapshot, {int((now - base['ts']).total_seconds() // 60)} min)") if dm else None


@st.cache_data(ttl=300, show_spinner=False)
def oi_hist_chain(_kite, contracts):
    """contracts: tuple of (strike, 'CE'/'PE', token). Har contract ki 15m OI history (~3 req/s limit ke liye sleep)."""
    out, to = [], datetime.now(IST).replace(tzinfo=None)
    for K, tag, tok in contracts:
        try:
            df = pd.DataFrame(_kite.historical_data(int(tok), to - timedelta(days=5), to, "15minute", oi=True))
        except Exception:
            time.sleep(0.5)
            continue
        if df.empty or "oi" not in df or len(df) < 3:
            continue
        days = dts_ist(df.date).dt.date
        prev = df[days < days.iloc[-1]]
        out.append(dict(K=float(K), tag=tag, oi_now=float(df.oi.iloc[-1]),
                        oi_prev=float(prev.oi.iloc[-1]) if not prev.empty else None,
                        oi_1h=float(df.oi.iloc[-5]) if len(df) >= 5 else None,
                        t_now=df.date.iloc[-1], t_prev=prev.date.iloc[-1] if not prev.empty else None,
                        t_1h=df.date.iloc[-5] if len(df) >= 5 else None))
        time.sleep(0.35)
    return pd.DataFrame(out)


def hist_delta(df, which="prev"):
    col = "oi_prev" if which == "prev" else "oi_1h"
    dm = {}
    for _, r in df.iterrows():
        if r[col] is None or pd.isna(r[col]):
            continue
        dm.setdefault(float(r.K), [None, None])[0 if r.tag == "CE" else 1] = float(r.oi_now - r[col])
    return dm


def oi_zone(dm, spot):
    """Put zone = PE strikes <= spot ka net OI change (support build), Call zone = CE strikes >= spot (resistance build)."""
    put = sum(v[1] for k, v in dm.items() if k <= spot and v[1] is not None)
    call = sum(v[0] for k, v in dm.items() if k >= spot and v[0] is not None)
    return dict(put=float(put), call=float(call), zone=True)


# ============================================================================
# App
# ============================================================================
kite = ensure_session()

with st.sidebar:
    st.header("⚙️ Settings")
    st.caption(f"👤 {st.session_state.get('user') or STORE.get('user') or 'Connected'}")
    if APP_PASSWORD and st.query_params.get("k"):
        st.caption("🔖 Is URL ko bookmark / Home screen par add karein - 30 din tak password nahi maangega.")
    cfg = dict(
        tf=st.selectbox("Intraday timeframe", ["15minute", "5minute"]),
        r=st.number_input("Risk-free rate % (Greeks)", 0.0, 15.0, 6.5, 0.1) / 100,
        n=st.slider("Option chain: strikes ±ATM", 4, 20, 10),
        oi_n=st.slider("OI history: strikes ±ATM (API heavy)", 3, 15, 8),
        capital=st.number_input("Trading capital ₹ (sizing ke liye)", 10000, 100000000, 500000, 10000),
        risk=st.number_input("Risk per trade % of capital", 0.25, 5.0, 1.0, 0.25),
        slip=st.number_input("Slippage % per side", 0.0, 5.0, 0.5, 0.1),
        brok=st.number_input("Brokerage ₹ per order", 0.0, 100.0, 20.0, 1.0),
        stt=st.number_input("STT % on option sell premium (verify current rate)", 0.0, 1.0, 0.1, 0.01),
    )
    if st.button("Logout"):
        reset_session()
        st.rerun()

st.title("📈 Nexion F&O Terminal")
inst = guarded(load_instruments, kite, str(datetime.now(IST).date()))
if inst is None or inst.empty:
    st.error("Instrument list load nahi hui. Refresh karein ya dobara login karein.")
    st.stop()


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def build_universe(_inst, day):
    c = _inst[_inst.exchange.isin(["NSE", "BSE"])]
    c = c[(c.segment == "INDICES") | (c.instrument_type == "EQ")]
    nse = c[c.exchange == "NSE"]
    bse = c[(c.exchange == "BSE") & ~c.tradingsymbol.isin(nse.tradingsymbol)]
    u = pd.concat([nse, bse]).drop_duplicates("instrument_token").copy()
    u["pri"] = (u.segment != "INDICES").astype(int)
    u = u.sort_values(["pri", "tradingsymbol"]).reset_index(drop=True)
    u["label"] = u.tradingsymbol + " · " + u.name.fillna("") + " (" + u.exchange + ")"
    return u[["instrument_token", "tradingsymbol", "name", "exchange", "segment", "label"]]


univ = build_universe(inst, str(datetime.now(IST).date()))
deriv = inst[inst.exchange.isin(["NFO", "BFO"])]
deriv_names = set(deriv.name.dropna())
INDEX_MAP = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY", "NIFTY FIN SERVICE": "FINNIFTY",
             "NIFTY MID SELECT": "MIDCPNIFTY", "SENSEX": "SENSEX", "BANKEX": "BANKEX"}
labels = univ.label.tolist()

# favourites: favourites.txt (GitHub) se
fav_labels, missing = [], []
for fsym in load_favs():
    m_ = univ[univ.tradingsymbol.str.upper() == fsym.upper()]
    if m_.empty:
        missing.append(fsym)
    else:
        fav_labels.append(m_.iloc[0].label)
PLACEHOLDER = "— search se chunein —"
fav_choice = st.selectbox("⭐ Favourites", [PLACEHOLDER] + fav_labels)
default_i = next((i for i, l in enumerate(labels) if l.startswith("NIFTY 50 ·")), 0)
search_choice = st.selectbox("🔍 Search - type karein (jaise rel, tcs, bank); list apne aap filter hogi",
                             labels, index=default_i, disabled=(fav_choice != PLACEHOLDER))
choice = search_choice if fav_choice == PLACEHOLDER else fav_choice
if missing:
    st.caption("favourites.txt mein ye symbols nahi mile: " + ", ".join(missing))
row = univ.iloc[labels.index(choice)]
sym, is_index = row.tradingsymbol, row.segment == "INDICES"
key = f"{row.exchange}:{sym}"
und = INDEX_MAP.get(sym, sym)
und = und if und in deriv_names else None

expiry, fut0 = None, None
if und:
    today = pd.Timestamp(datetime.now(IST).date())
    exps = sorted(deriv[(deriv.name == und) & deriv.instrument_type.isin(["CE", "PE"])
                        & (deriv.expiry >= today)].expiry.dropna().unique())
    if exps:
        expiry = pd.Timestamp(st.selectbox("Option expiry", exps, format_func=lambda d: pd.Timestamp(d).strftime("%d-%b-%Y")))
    fl = deriv[(deriv.name == und) & (deriv.instrument_type == "FUT") & (deriv.expiry >= today)].sort_values("expiry")
    fut0 = fl.iloc[0] if not fl.empty else None
else:
    st.caption("ℹ️ Is instrument par F&O contracts nahi hain - sirf equity analysis dikhega.")

c_a, c_b = st.columns(2)
if c_a.button("🔄 Refresh live data"):
    for fn in (get_quotes, get_history, get_oi_change, fetch_news, fetch_global):
        fn.clear()
    st.rerun()
refresh = c_b.selectbox("Auto-refresh", [0, 15, 30, 60], index=0,
                        format_func=lambda x: "Off" if x == 0 else f"Har {x} sec", label_visibility="collapsed")


def dashboard():
    now = datetime.now(IST)
    t0_ = time.time()
    ph = st.empty()
    ph.info("⏳ Live data aur analysis load ho raha hai (pehli baar 10-30 sec lag sakte hain)...")
    tf = cfg["tf"]
    HZ = make_hz(tf)
    qd = guarded(get_quotes, kite, (key, "NSE:INDIA VIX"))
    if not qd or key not in qd:
        ph.empty()
        st.error("Live quote nahi mila. Market data permission/plan ya network check karein.")
        return
    q = qd[key]
    vq = qd.get("NSE:INDIA VIX")
    spot = q["last_price"]
    ohlc = q.get("ohlc") or {}
    prev = ohlc.get("close") or 0
    ts = q.get("timestamp") or q.get("last_trade_time")
    tok = int(row.instrument_token)
    is_open = market_open()
    qts = to_ist(ts)
    qage = (now - qts).total_seconds() if qts is not None else None
    if not is_open:
        data_state, data_label = "closed", "CLOSED - last available close"
    elif qage is not None and qage > 120:
        data_state, data_label = "stale", f"STALE (quote {int(qage)}s purana)"
    elif qage is not None and qage > 30:
        data_state, data_label = "delayed", f"DELAYED ({int(qage)}s)"
    else:
        data_state, data_label = "live", "LIVE" + (f" ({int(qage)}s)" if qage is not None else "")

    h_i = guarded(get_history, kite, tok, tf, 10)
    hday = guarded(get_history, kite, tok, "day", 400)
    hf = guarded(get_history, kite, int(fut0.instrument_token), tf, 10, True) if fut0 is not None else None
    d_i = add_indicators(h_i, True) if h_i is not None and len(h_i) >= 40 else None
    dday = add_indicators(hday) if hday is not None and len(hday) >= 60 else None
    hf_i = add_indicators(hf, True) if hf is not None and len(hf) >= 40 else None
    rv = float(np.log(hday.close).diff().tail(20).std() * math.sqrt(252)) if hday is not None and len(hday) >= 22 else None

    ch, stats, T, rows, strikes = pd.DataFrame(), None, None, {}, []
    s_eff, ps, dex, dte = spot, None, "NFO", 99
    if expiry is not None:
        T = years_to_expiry(expiry)
        dte = (expiry.date() - now.date()).days
        ex_df = deriv[(deriv.name == und) & (deriv.expiry == expiry) & deriv.instrument_type.isin(["CE", "PE"])]
        allk = sorted(ex_df.strike.unique())
        if allk:
            dex = ex_df.exchange.iloc[0]
            i = int(np.argmin([abs(s - spot) for s in allk]))
            half = 1.6 * 0.20 * math.sqrt(T) * spot  # engine ko ~1.6σ tak ki strikes chahiye (hedge leg ke liye bhi)
            win = {k for k in allk if abs(k - spot) <= half} | set(allk[max(0, i - cfg["n"]): i + cfg["n"] + 1])
            if len(win) > 160:
                win = set(sorted(win, key=lambda k: abs(k - spot))[:160])
            sel = ex_df[ex_df.strike.isin(win)]
            keys = tuple(f"{e}:{s}" for e, s in zip(sel.exchange, sel.tradingsymbol))
            cq = guarded(get_quotes, kite, keys)
            if cq:
                atmK = min(win, key=lambda k: abs(k - spot))
                ps = parity_spot(cq, sel, atmK, T, cfg["r"])
                if ps and abs(ps / spot - 1) < 0.01:
                    s_eff = ps  # IV/Greeks CE-PE consistent rakhne ke liye forward-adjusted underlying
                ch = build_chain(cq, sel, s_eff, T, cfg["r"])
                if not ch.empty:
                    stats = chain_stats(ch)
