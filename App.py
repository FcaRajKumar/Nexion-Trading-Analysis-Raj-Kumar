"""Nexion F&O Terminal - Streamlit + Zerodha Kite Connect (analysis only, no orders)."""
import hmac
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

import altair as alt
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
    if oiw:
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


def risk_checks(c, ctx, hname):
    """(check, ok, detail, hard)"""
    out = []
    rr = c["rr"]
    out.append(("Net R:R ≥ 1.2 (costs ke baad)", bool(rr and rr >= 1.2), f"{rr:.2f}" if rr else "N/A", True))
    out.append(("Size ≥ 1 lot (risk budget ke andar)", c["lots"] >= 1, f"{c['lots']} lot", True))
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
    return {"t1": (t1, hold * 0.6), "t2": (t2, hold), "flat": (S, hold), "sl": (S - sgn * slu, hold * 0.4)}, note


def equity_levels(view, S, atr_u):
    if view == 0:
        return None
    s = view
    return dict(side="BUY" if s > 0 else "SELL (intraday short, MIS)", zone=(S - s * 0.25 * atr_u, S),
                t1=S + s * 1.5 * atr_u, t2=S + s * 2.5 * atr_u, sl=S - s * 1.0 * atr_u)


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
        out["eq"] = equity_levels(view, S, atr_u)
        if ctx["data_state"] == "stale":
            out["eq_status"] = (ST_NO, "Data stale hai")
        else:
            out["eq_status"] = plan_status(plan, ctx, hname)
    sig = ctx["atm_iv"]
    if not rows or not sig or not T:
        out["err"] = "Option data nahi (is instrument par F&O nahi hai ya ATM IV calculate nahi hua)."
        return out
    lot, regime, dte = ctx["lot"], ctx["regime"], ctx["dte"]
    tdays = T * 365
    move, hold = min(hz["move"], tdays), min(hz["hold"], 0.8 * tdays)
    m = S * sig * math.sqrt(move / 365)
    slu = min(max(atr_u, 0.3 * m), 0.6 * m)  # SL = ATR, 0.3σ-0.6σ ke beech
    out["m"], out["hold"] = m, hold
    if view:
        plan = make_plan(an, view, S, slu, 1.0 * m)
        out["plan"] = plan
    A = {k: dict(action=k, status=ST_NO, reason="", card=None, pref="", checks=[]) for k in ACTIONS}

    def card(legs, scen, notes):
        ev = struct_eval(legs, lot, T, scen, cfg, ctx["basis"])
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
            pt1 = 2 * (1 - nd(abs(ev["t1"]["S"] - S) / m))
            psl = 2 * (1 - nd(abs(ev["sl"]["S"] - S) / m))
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
            finish(kb, card(legs_b, scen, [wnote] if wnote else []), "buy", True, room, bo)
        else:
            A[kb]["reason"] = "Suitable strike/price nahi mila."
        legs, notes = credit_spread(ctx, ts_, hz["ds"])
        if legs:
            legs_s = shift_legs(legs, S, bo["trig"], T, cfg, ctx["basis"]) if bo else legs
            finish(ks, card(legs_s, scen, notes), "sell", True, room, bo)
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
    if not c:
        return {"Action": a["action"], "Status": a["status"], "Entry": "-", "Target": "-", "Stop loss": "-"}
    ev, cr = c["ev"], c["credit"]
    return {"Action": legs_text(c), "Status": a["status"],
            "Entry": f"credit ₹{abs(ev['V0']):.1f}" if cr else f"buy ₹{ev['V0']:.1f}",
            "Target": f"buy-back ₹{abs(ev['t1']['V']):.1f}" if cr else f"sell ₹{ev['t1']['V']:.1f}",
            "Stop loss": f"buy-back ₹{abs(ev['sl']['V']):.1f}" if cr else f"exit ₹{ev['sl']['V']:.1f}"}


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
    with st.expander(f"{a['status']}  {legs_text(c)}", expanded=a["status"] == ST_READY):
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
        st.caption("Levels: ATR-based (SL 1×, T1 1.5×, T2 2.5× ATR) - hypothetical.")
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
# Chart (Altair - Streamlit ke saath aata hai, extra dependency nahi)
# ============================================================================
def make_chart(d, levels, show_ema, show_vwap, show_levels, daily, n=90):
    c = d.tail(n).copy()
    dt = dts_ist(c.date)
    c["label"] = dt.dt.strftime("%d %b" if daily else "%d %H:%M")
    UP, DOWN, AMB, GREY = "#79C99A", "#E58B8B", "#E9C778", "#9CA8B6"
    base = alt.Chart(c).encode(x=alt.X("label:O", sort=None, axis=alt.Axis(title=None, labelAngle=-45, labelOverlap="greedy")))
    col = alt.condition("datum.open <= datum.close", alt.value(UP), alt.value(DOWN))
    ys = alt.Scale(zero=False)
    layers = [base.mark_rule().encode(y=alt.Y("low:Q", scale=ys, title=None), y2="high:Q", color=col),
              base.mark_bar(size=5).encode(y=alt.Y("open:Q", scale=ys), y2="close:Q", color=col,
                                           tooltip=["label", "open", "high", "low", "close"])]
    if show_ema and "ema21" in c:
        layers.append(base.mark_line(strokeWidth=1.2).encode(y=alt.Y("ema21:Q", scale=ys), color=alt.value(AMB)))
    if show_vwap and "vwap" in c and c.vwap.notna().any():
        layers.append(base.mark_line(strokeWidth=1.2, strokeDash=[4, 3]).encode(y=alt.Y("vwap:Q", scale=ys), color=alt.value(GREY)))
    if show_levels and levels:
        lo, hi = float(c.low.min()), float(c.high.max())
        pad = 0.01 * hi
        ld_ = pd.DataFrame([l for l in levels if lo - pad <= l["price"] <= hi + pad])
        if not ld_.empty:
            layers.append(alt.Chart(ld_).mark_rule(strokeDash=[5, 4], strokeWidth=1).encode(
                y=alt.Y("price:Q", scale=ys),
                color=alt.Color("kind:N", scale=alt.Scale(domain=["Support", "Resistance", "Entry", "Target", "Stop"],
                                                          range=[UP, DOWN, AMB, UP, DOWN]), legend=None),
                tooltip=["label", "price", "kind"]))
    return alt.layer(*layers).properties(height=380)


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
refresh = c_b.selectbox("Auto-refresh", [0, 10, 15, 30, 60], index=(2 if market_open() else 0),
                        format_func=lambda x: "Off" if x == 0 else f"Har {x} sec", label_visibility="collapsed")


def dashboard():
    now = datetime.now(IST)
    tf = cfg["tf"]
    HZ = make_hz(tf)
    qd = guarded(get_quotes, kite, (key, "NSE:INDIA VIX"))
    if not qd or key not in qd:
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
                    rows = {r_["Strike"]: r_ for r_ in ch.to_dict("records")}
                    strikes = sorted(rows)

    atm_iv, lot = None, 1
    if rows:
        atm = rows[min(strikes, key=lambda k: abs(k - spot))]
        ivs = [x for x in (num(atm.get("CE iv")), num(atm.get("PE iv"))) if x]
        atm_iv = sum(ivs) / len(ivs) if ivs else None
        lot = int(num(atm.get("CE lot")) or num(atm.get("PE lot")) or 1)
    ratio = atm_iv / rv if atm_iv and rv else None
    regime = "rich" if ratio and ratio >= 1.3 else "cheap" if ratio and ratio < 1.0 else "fair" if ratio else None

    oiw = None
    if stats:
        try:
            pw, cw = rows.get(stats["support"]), rows.get(stats["resistance"])
            pc = guarded(get_oi_change, kite, int(pw["PE token"])) if pw and num(pw.get("PE token")) else None
            cc = guarded(get_oi_change, kite, int(cw["CE token"])) if cw and num(cw.get("CE token")) else None
            if pc and cc:
                oiw = dict(put=pc[1], call=cc[1])
        except Exception:
            oiw = None

    fut_info, bu, oi_i = None, None, None
    if fut0 is not None:
        try:
            fk = f"{fut0.exchange}:{fut0.tradingsymbol}"
            fq = (guarded(get_quotes, kite, (fk,)) or {}).get(fk)
            if fq:
                oi_i = fut_oi_info(hf, tf)
                day_chg = oi_i["day"] if oi_i and oi_i.get("day") is not None else None
                if day_chg is None:
                    oc = guarded(get_oi_change, kite, int(fut0.instrument_token))
                    day_chg = oc[1] if oc else None
                fp = (fq.get("ohlc") or {}).get("close") or 0
                fchg = (fq["last_price"] - fp) / fp * 100 if fp else 0
                bu = buildup(fchg, day_chg)
                fut_info = (fut0.tradingsymbol, fq, day_chg, fchg)
        except Exception:
            fut_info, bu = None, None

    news_q = {"NIFTY 50": "Nifty 50 India stock market", "NIFTY BANK": "Bank Nifty India",
              "SENSEX": "Sensex India stock market"}.get(sym) or \
             (f"{row['name'] or sym} India index" if is_index else f"\"{row['name'] or sym}\" share price OR stock")
    try:
        items, nerr = fetch_news(news_q)
        mitems, _ = fetch_news("Sensex Nifty market today")
        nsrc = items if len(items) >= 3 else mitems
        nws = news_factor(nsrc)
    except Exception:
        items, nerr, nsrc, nws = [], "News section error - baaki app normal hai.", [], None
    try:
        glob = fetch_global()
        glb = global_factor(glob)
    except Exception:
        glob, glb = [], None

    adx_d = float(dday.adx.iloc[-1]) if dday is not None and not pd.isna(dday.adx.iloc[-1]) else None
    ctx = dict(spot=spot, T=T, cfg=cfg, rows=rows, strikes=strikes, stats=stats, atm_iv=atm_iv, regime=regime, bu=bu,
               news=nws, glb=glb, oiw=oiw, lot=lot, basis=s_eff - spot, dex=dex, dte=dte, kite=kite, is_index=is_index,
               now=now, is_open=is_open, data_state=data_state, data_label=data_label, rv=rv, ratio=ratio, adx_d=adx_d,
               expiry_txt=f"{expiry:%d-%b-%Y}" if expiry is not None else "-")
    res, errs = {}, []
    for hname, hz in HZ.items():
        intraday = hname == "Intraday"
        ds = d_i if intraday else dday
        if ds is None or dday is None:
            res[hname] = None
            continue
        try:
            an = analyze_horizon(hname, ds, dday, hf_i if intraday else None, ctx, tf if intraday else "1D", intraday)
            res[hname] = horizon_view(hname, hz, an, ctx)
        except Exception as e:
            res[hname] = None
            errs.append(f"{hname}: {type(e).__name__}: {str(e)[:120]}")

    # ---- status bar
    sess = "🟢 MARKET OPEN" if is_open else "⚪ MARKET CLOSED"
    dchip = {"live": "🟢", "delayed": "🟡", "stale": "🔴", "closed": "⚪"}[data_state]
    st.caption(f"{sess} | Data: {dchip} {data_label} | Last update {now:%H:%M:%S} IST | {sym}")
    for e_ in errs:
        st.warning("Analysis error - " + e_)

    tabs = st.tabs(["📊 Overview", "🎯 Trade Idea", "📈 Chart & Levels", "🌍 News & Global", "⛓ Option Chain", "ℹ️ Logic"])

    with tabs[0], section("Overview"):
        ri, rs = res.get("Intraday"), res.get("Swing (2-3 din / expiry)")
        main = rs or ri
        if main:
            txt = f"**Market push: {VIEW_LBL[main['view']]}**"
            if ri and rs:
                txt += f"  \nIntraday: {VIEW_LBL[ri['view']]} ({ri['D']:+.0f}) | Swing: {VIEW_LBL[rs['view']]} ({rs['D']:+.0f})"
            aligned = [f"{x['name']}" for x in main["an"]["rows"] if x["w"] > 0 and main["view"] and x["sig"] * main["view"] > 0][:4]
            if aligned:
                txt += "  \nWajah: " + ", ".join(aligned)
            if main["an"]["conflict"]:
                txt += "  \n" + main["an"]["conflict"]
            (st.success if main["view"] > 0 else st.error if main["view"] < 0 else st.info)(txt)
        if not is_open:
            st.warning("Market abhi band hai (9:15-15:30 IST, Mon-Fri; holidays app nahi jaanta). Data last available hai.")
        a, b, c = st.columns(3)
        a.metric(sym, f"{spot:,.2f}", f"{spot - prev:+,.2f} ({(spot - prev) / prev * 100:+.2f}%)" if prev else None)
        b.metric("Day Low - High", f"{ohlc.get('low', 0):,.2f} - {ohlc.get('high', 0):,.2f}")
        c.metric("Prev close", f"{prev:,.2f}")
        a, b, c = st.columns(3)
        a.metric("Open", f"{ohlc.get('open', 0):,.2f}")
        b.metric("Volume", f"{q.get('volume', 0):,}" if q.get("volume") else "N/A (index)")
        c.metric("Circuit L / U", f"{q.get('lower_circuit_limit') or 0:,.2f} / {q.get('upper_circuit_limit') or 0:,.2f}")
        reg = []
        if adx_d is not None:
            reg.append(f"ADX(1D) {adx_d:.0f} - {'strong trend' if adx_d >= 25 else 'weak/range' if adx_d < 20 else 'developing'}")
        if rs:
            reg.append("structure (swing): " + {1: "HH/HL up", -1: "LH/LL down", 0: "range"}[rs["an"]["state"]])
        if vq:
            vp = (vq.get("ohlc") or {}).get("close") or 0
            reg.append(f"India VIX {vq['last_price']:.2f}" + (f" ({(vq['last_price'] - vp) / vp * 100:+.1f}%)" if vp else ""))
        if atm_iv:
            reg.append(f"ATM IV {atm_iv * 100:.1f}%" + (f", IV/RV {ratio:.2f} → options {regime}" if ratio else ""))
        if reg:
            st.caption("Regime: " + " | ".join(reg))
        if fut_info:
            name_, fq, day_chg, fchg = fut_info
            a, b, c = st.columns(3)
            a.metric(f"Future {name_}", f"{fq['last_price']:,.2f}", f"{fchg:+.2f}%")
            b.metric("Basis (fut - spot)", f"{fq['last_price'] - spot:+,.1f}")
            c.metric("Fut OI", f"{int(fq.get('oi') or 0):,}")
            parts = []
            if day_chg is not None:
                parts.append(f"day Δ {day_chg:+,.0f}" + (f" (vs {str(to_ist(oi_i['t_prev']))[:16]})" if oi_i and oi_i.get("t_prev") is not None else ""))
            if oi_i and oi_i.get("hr") is not None:
                parts.append(f"~1h Δ {oi_i['hr']:+,.0f} (since {str(to_ist(oi_i['t_hr']))[11:16]})")
            if parts:
                st.caption("Futures OI change: " + " | ".join(parts))
            if bu:
                st.caption("📌 " + bu[1] + " - inferred signal, verified trader intent nahi.")
        if q.get("depth"):
            with st.expander("Market depth (top 5)"):
                dp = q["depth"]
                st.dataframe(pd.DataFrame({"Bid qty": [x["quantity"] for x in dp["buy"]], "Bid": [x["price"] for x in dp["buy"]],
                                           "Ask": [x["price"] for x in dp["sell"]], "Ask qty": [x["quantity"] for x in dp["sell"]]}),
                             hide_index=True)

    with tabs[1], section("Trade Idea"):
        st.warning("⚠️ Hypothetical analysis - advice ya profit guarantee nahi. App koi order place nahi karta. Selling hamesha hedged.")
        if dte == 0:
            st.error("📅 Aaj expiry hai: theta bahut tez, short options par extra margin (ELM).")
        hts = st.tabs(list(HZ))
        for tab_, hname in zip(hts, HZ):
            with tab_, section(hname):
                render_hz(res.get(hname), ctx, hname[:3], spot)
        try:
            recs = []
            for hname, r_ in res.items():
                for a_ in (r_ or {}).get("actions", []):
                    rw = action_row(a_)
                    recs.append(dict(time=f"{now:%Y-%m-%d %H:%M}", instrument=sym, horizon=hname, view=VIEW_LBL[r_["view"]],
                                     grade=r_["grade"], score=round(r_["D"], 1), spot=spot,
                                     **{k.lower().replace(" ", "_"): v for k, v in rw.items()},
                                     rr=round(a_["card"]["rr"], 2) if a_["card"] and a_["card"]["rr"] else None))
            if recs:
                st.download_button("⬇️ Suggestions CSV (paper-trade log)", pd.DataFrame(recs).to_csv(index=False),
                                   file_name=f"nexion_{sym.replace(' ', '_')}_{now:%Y%m%d_%H%M}.csv", mime="text/csv", key="dl_csv")
        except Exception:
            pass

    with tabs[2], section("Chart & Levels"):
        cc1, cc2, cc3, cc4 = st.columns(4)
        which = st.radio("Timeframe", [f"Intraday ({tf})", "Daily"], horizontal=True, label_visibility="collapsed")
        sh_ema, sh_vw, sh_lv, sh_plan = cc1.checkbox("EMA21", True), cc2.checkbox("VWAP", True), cc3.checkbox("S/R levels", True), cc4.checkbox("Trade plan", True)
        intr = which.startswith("Intraday")
        cd = d_i if intr else dday
        an_c = (res.get("Intraday") if intr else res.get("Swing (2-3 din / expiry)"))
        if cd is None:
            st.info("Chart ke liye historical data kam hai.")
        else:
            lv_ = list((an_c["an"]["levels"]) if an_c else [])
            if sh_plan and an_c and an_c.get("plan"):
                pl_ = an_c["plan"]
                if pl_["trig"]:
                    lv_.append(dict(price=pl_["trig"]["price"], label="Entry trigger", kind="Entry"))
                lv_.append(dict(price=pl_["inv"], label="Invalidation", kind="Stop"))
                card_ = next((a_["card"] for a_ in an_c["actions"] if a_["card"] and a_["status"] in (ST_READY, ST_WAIT)), None)
                if card_:
                    lv_ += [dict(price=card_["ev"]["t1"]["S"], label="Target 1 (index)", kind="Target"),
                            dict(price=card_["ev"]["sl"]["S"], label="SL (index)", kind="Stop")]
            st.altair_chart(make_chart(cd, lv_, sh_ema, sh_vw, sh_lv, not intr), use_container_width=True)
            if intr and is_index and hf_i is not None:
                st.caption("Index mein volume nahi hota - VWAP (futures candles se) trade signal mein use hota hai, spot chart par nahi.")
            if an_c:
                st.dataframe(pd.DataFrame([dict(Level=l["label"], Price=round(l["price"], 2), Type=l["kind"], Source=l.get("src", "")) for l in
                                           sorted(an_c["an"]["levels"], key=lambda x: -x["price"])]), hide_index=True)
                st.caption("Levels: swing clusters (touches ke saath), prev-day H/L/C, opening range (first 15 min), 20d H/L, OI walls - sab asli data se.")

    with tabs[3], section("News & Global"):
        st.markdown("**🌍 Global cues** (Yahoo Finance, unofficial - kabhi fail ho sakta hai)")
        if glob and any(x["last"] is not None for x in glob):
            st.dataframe(pd.DataFrame([{"Market": ("🟢 " if (x["chg"] or 0) > 0 else "🔴 " if (x["chg"] or 0) < 0 else "⚪ ") + x["name"],
                                        "Level": round(x["last"], 2) if x["last"] is not None else None,
                                        "Chg %": round(x["chg"], 2) if x["chg"] is not None else None} for x in glob]), hide_index=True)
            if glb:
                st.caption(f"Global bias: {'positive' if glb[0] > 0 else 'negative' if glb[0] < 0 else 'mixed'} - {glb[1]}")
        else:
            st.warning("Global data abhi nahi mila.")
        st.markdown("**GIFT Nifty:** free API se reliable feed nahi mila, isliye live level [Zerodha GIFT Nifty page](https://zerodha.com/market/giftnifty/) par dekhein.")
        st.markdown("---")
        st.markdown("**📰 Headlines**")
        if nerr:
            st.warning(nerr)
        src = "instrument-specific" if len(items) >= 3 else "market-wide (instrument news kam mila)"
        st.caption(f"Query: {news_q} | Google News RSS | Sentiment source: {src}")
        if nws:
            st.markdown(f"**Sentiment score: {nws[0]:+d}** - {nws[1]}")
        for it in nsrc[:10]:
            age = f"{int((now - it['when']).total_seconds() // 3600)}h" if it["when"] else ""
            st.markdown(f"{'🟢' if it['score'] > 0 else '🔴' if it['score'] < 0 else '⚪'} [{it['title']}]({it['link']}) {age}")
        st.caption("Keyword sentiment crude hai; Regime category mein iska weight sirf 10% hai. Headlines khud bhi padhein.")

    with tabs[4], section("Option Chain"):
        if ch.empty:
            st.info("Option chain available nahi (F&O nahi hai ya data nahi mila).")
        else:
            st.markdown(f"**{und} option chain** | Selected: {sym} ({row.exchange}) | Expiry {expiry:%d-%b-%Y} | Spot {spot:,.2f}")
            lo, hi = ch.Strike.min(), ch.Strike.max()
            sample = rows[strikes[0]].get("CE sym") or rows[strikes[0]].get("PE sym")
            st.caption(f"Strikes {lo:.0f} - {hi:.0f} ({len(ch)} rows) | sample contract: {sample}")
            if ps is None:
                st.warning("Underlying check nahi ho paya (ATM CE/PE price missing).")
            elif abs(ps / spot - 1) < 0.01:
                st.success(f"✅ Underlying check: put-call parity se implied {ps:,.0f} vs {sym} spot {spot:,.0f} "
                           f"(diff {abs(ps / spot - 1) * 100:.2f}%) - chain isi underlying ki hai.")
            else:
                st.error(f"⚠️ Parity-implied {ps:,.0f} vs spot {spot:,.0f} (diff >1%) - chain mismatch ho sakti hai. Refresh karein.")
            atm_k = min(strikes, key=lambda k: abs(k - spot))
            cols = ["CE OI", "CE Vol", "CE IV%", "CE Delta", "CE LTP", "Strike", "PE LTP", "PE Delta", "PE IV%", "PE Vol", "PE OI"]
            ia = strikes.index(atm_k)
            vs = set(strikes[max(0, ia - cfg["n"]): ia + cfg["n"] + 1])
            show = ch[ch.Strike.isin(vs)][[c for c in cols if c in ch.columns]].round(2)
            styled = show.style.apply(lambda r_: ["background-color: rgba(255,200,0,0.25)" if r_["Strike"] == atm_k else "" for _ in r_], axis=1)
            st.dataframe(styled, height=min(38 + 35 * len(show), 900), hide_index=True)
            st.caption(f"Peeli row = ATM {atm_k:.0f}. Table ±{cfg['n']} strikes dikhata hai; PCR/OI walls aur trade engine poori {len(ch)}-strike window use karte hain.")
            if stats:
                a_, b_, c_, e_ = st.columns(4)
                a_.metric("PCR (OI)", f"{stats['pcr']:.2f}")
                b_.metric("Max Call OI (resist.)", f"{stats['resistance']:.0f}")
                c_.metric("Max Put OI (support)", f"{stats['support']:.0f}")
                e_.metric("Max pain (window)", f"{stats['max_pain']:.0f}")

    with tabs[5], section("Logic"):
        st.markdown("""
**Data:** Observed = Zerodha (quotes, OI, candles, instruments). Calculated = indicators/levels/Greeks. Hypothetical = entry/target/SL scenarios.
**Step 1 - Gate (veto):** data fresh, R:R ≥ 1.2 (costs ke baad), size ≥ 1 lot, liquidity, intraday entry 15:00 se pehle. Fail = ⚪ NO TRADE.
**Step 2 - Direction (3 categories, har ek ke andar weighted average, max ±1):**
- Regime 25%: EMA50/200 (daily), ADX/DMI, Supertrend, Global cues, News (low conf.).
- Structure 25%: EMA9/21, Supertrend, MACD, RSI, BOS/CHOCH, VWAP, candle patterns (engulfing, rejection at level, breakout+retest) - sirf closed candles.
- Positioning 20%: futures price/OI build-up, OI change at walls, PCR (theory), price vs OI walls.
Score = weighted average ×100. |score| ≥ 35 = view; koi category 0.4 se zyada ulti ho to view neutral (conflict). Grade A: ≥60 aur sab categories align; B: ≥45; C baaki.
**Step 3 - Volatility:** IV/RV ≥ 1.3 → selling preferred, < 1.0 → buying preferred. Direction nahi, instrument decide karta hai.
**Step 4 - Construction:** Buy ≈ ATM (delta 0.5-0.55); sell short strike nearest S/R/OI wall ke bahar (delta ≤ 0.30) + hedge leg. SL = ATR (0.3σ-0.6σ), T1 = 1.5×SL, T2 = 2.5×SL, nearest level par cap.
**Step 5 - Status:** 🟢 READY (confirmation + VWAP ok + extended nahi + gates pass), 🟡 WAIT (setup ban raha / market band), 🔴 INVALIDATED (opposite BOS/CHOCH), ⚪ NO TRADE (gate fail ya view neutral).
**Limitations:** thresholds/weights abhi unvalidated hain (backtest nahi). Costs mein exchange charges, GST, stamp duty nahi. Holidays app ko nahi pata.
OI change observation: day = pichle session ke last candle se; ~1h = futures 15m/5m candles se. Streamlit Cloud restart par token chala jata hai. Educational analysis, financial advice nahi.""")


run_every = refresh if refresh > 0 else None
st.fragment(run_every=run_every)(dashboard)()
st.caption("Nexion Consulting | Analysis-only. Data: Zerodha Kite Connect.")
