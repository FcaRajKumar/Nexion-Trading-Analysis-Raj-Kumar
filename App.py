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


def add_indicators(df):
    d = df.copy()
    d["ema9"] = d.close.ewm(span=9, adjust=False).mean()
    d["ema21"] = d.close.ewm(span=21, adjust=False).mean()
    chg = d.close.diff()
    up, dn = chg.clip(lower=0), (-chg.clip(upper=0)).replace(0, 1e-12)
    rs = up.ewm(alpha=1 / 14, adjust=False).mean() / dn.ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + rs)
    pc = d.close.shift()
    tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
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


# ----------------------------------------------------------------------------
# Scoring: weights book-references ke hisaab se (PCR/Max pain = theory, isliye kam weight;
# OI change zyada weight). Score range roughly -6 .. +6.
# ----------------------------------------------------------------------------
def score_view(d, stats, spot, bu, nws, glb, oiw):
    last, f = d.iloc[-1], []
    if last.ema9 > last.ema21 and last.close > last.ema21:
        f.append(("Trend (EMA9/21)", 1.0, "EMA9 > EMA21, price EMA21 ke upar"))
    elif last.ema9 < last.ema21 and last.close < last.ema21:
        f.append(("Trend (EMA9/21)", -1.0, "EMA9 < EMA21, price EMA21 ke neeche"))
    else:
        f.append(("Trend (EMA9/21)", 0.0, "Mixed / sideways"))
    rsi = last.rsi
    if 55 < rsi <= 75:
        f.append(("Momentum (RSI14)", 1.0, f"RSI {rsi:.1f} > 55"))
    elif 25 <= rsi < 45:
        f.append(("Momentum (RSI14)", -1.0, f"RSI {rsi:.1f} < 45"))
    else:
        f.append(("Momentum (RSI14)", 0.0, f"RSI {rsi:.1f} neutral/extreme"))
    if stats:
        p = stats["pcr"]
        f.append(("PCR (OI) - theory, kam weight", 0.5 if p > 1.3 else -0.5 if p < 0.7 else 0.0, f"PCR {p:.2f}"))
        near = 0.004 * spot
        if 0 <= spot - stats["support"] <= near:
            f.append(("OI support/resistance", 0.5, f"Price Put-OI support {stats['support']:.0f} ke paas"))
        elif 0 <= stats["resistance"] - spot <= near:
            f.append(("OI support/resistance", -0.5, f"Price Call-OI resistance {stats['resistance']:.0f} ke paas"))
        else:
            f.append(("OI support/resistance", 0.0, f"Support {stats['support']:.0f} / Resistance {stats['resistance']:.0f}"))
    if oiw:
        put, call = oiw["put"], oiw["call"]
        pts = 1.0 if put > 0 and put > call else -1.0 if call > 0 and call > put else 0.0
        f.append(("OI change at walls", pts, f"Put wall ΔOI {put:+,.0f}, Call wall ΔOI {call:+,.0f}"))
    if bu:
        f.append(("Futures OI build-up", float(bu[0]), bu[1]))
    if nws:
        f.append(("News sentiment (low confidence)", 0.5 * nws[0], nws[1]))
    if glb:
        f.append(("Global cues (low confidence)", 0.5 * glb[0], glb[1]))
    return f


# ----------------------------------------------------------------------------
# Trade engine - har instrument ke liye ek hi flow
# ----------------------------------------------------------------------------
HZ = {"Intraday": dict(move=0.5, hold=0.25, db=0.50, ds=0.25, scale=math.sqrt(6)),
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
            note = f"⚠️ OI wall {wall:.0f} bahut paas hai - move limited reh sakta hai."
    # Target/SL beech raaste mein hit hote hain: T1 ~0.6, SL ~0.4 of hold; T2 aur flat poore hold par.
    return {"t1": (t1, hold * 0.6), "t2": (t2, hold), "flat": (S, hold), "sl": (S - sgn * slu, hold * 0.4)}, note


def equity_levels(view, S, atr_u):
    if view == 0:
        return None
    s = view
    return dict(side="BUY" if s > 0 else "SELL (intraday short, MIS)", zone=(S - s * 0.25 * atr_u, S),
                t1=S + s * 1.5 * atr_u, t2=S + s * 2.5 * atr_u, sl=S - s * 1.0 * atr_u)


def horizon_view(hname, hz, d, ctx):
    S, T, cfg, rows, strikes, stats = ctx["spot"], ctx["T"], ctx["cfg"], ctx["rows"], ctx["strikes"], ctx["stats"]
    f = score_view(d, stats, S, ctx["bu"], ctx["news"], ctx["glb"], ctx["oiw"])
    D, need = sum(x[1] for x in f), (2.5 if stats else 2.0)
    view = 1 if D >= need else -1 if D <= -need else 0
    grade = ("A" if abs(D) >= 4 else "B" if abs(D) >= 3 else "C") if view else "-"
    atr_u = float(d.atr.iloc[-1]) * hz["scale"]
    out = dict(name=hname, f=f, D=D, need=need, view=view, grade=grade, atr=atr_u,
               eq=equity_levels(view, S, atr_u), actions=[], err=None)
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
    A = {k: dict(action=k, verdict="⚪ AVOID", reason="", card=None) for k in ACTIONS}

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
            ds = [abs(l["delta"]) for l in legs if l["s"] < 0 and l["delta"] is not None]
            if ds:
                pop = max(0.0, 1 - sum(ds))
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

    def finish(key, c, kind):
        pref = "buy" if regime == "cheap" else "sell" if regime == "rich" else "both"
        good = c["rr"] is not None and c["rr"] >= 1.2
        v = "⚠️ WEAK R:R" if not good else ("✅ TAKE" if pref in ("both", kind) else "🟡 ALTERNATIVE")
        A[key].update(card=c, verdict=v)

    lbl = {1: "bullish", -1: "bearish", 0: "neutral"}[view]
    if view != 0:
        tb, ts_, sgn = ("CE", "PE", 1) if view > 0 else ("PE", "CE", -1)
        kb, ks = ("BUY CE", "SELL PE (hedged)") if view > 0 else ("BUY PE", "SELL CE (hedged)")
        wall = (stats["resistance"] if view > 0 else stats["support"]) if stats else None
        scen, wnote = dir_scen(S, m, sgn, wall, hold, slu)
        Kb = pick_by_delta(rows, strikes, tb, hz["db"])
        lg = make_leg(rows, Kb, tb, 1) if Kb else None
        if lg:
            finish(kb, card([lg], scen, [wnote] if wnote else []), "buy")
        else:
            A[kb]["reason"] = "Suitable strike/price nahi mila."
        legs, notes = credit_spread(ctx, ts_, hz["ds"])
        if legs:
            finish(ks, card(legs, scen, notes), "sell")
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
                finish("RANGE: Iron Condor", card(ps + cs, scen, pn + cn + ["Range view: spot short strikes ke beech rehna chahiye."]), "sell")
            else:
                A["RANGE: Iron Condor"]["reason"] = "Dono spreads ka data nahi mila."
    for k in ACTIONS:
        if not A[k]["card"] and not A[k]["reason"]:
            A[k]["reason"] = (f"View {lbl} hai - ye side ke against." if view != 0 else
                              f"Direction clear nahi (score {D:+.1f}, chahiye ±{need:g}).")
    out["actions"] = [A[k] for k in ACTIONS]
    return out


# ----------------------------------------------------------------------------
# Rendering helpers
# ----------------------------------------------------------------------------
VIEW_LBL = {1: "UPSIDE ⬆️", -1: "DOWNSIDE ⬇️", 0: "SIDEWAYS ↔️"}


def legs_text(c):
    return " / ".join(f"{'BUY' if l['s'] > 0 else 'SELL'} {l['K']:.0f} {l['tag']}" for l in c["legs"])


def action_row(a):
    c = a["card"]
    if not c:
        return {"Action": a["action"], "Verdict": a["verdict"], "Entry": "-", "Target": "-", "Stop loss": "-"}
    ev, cr = c["ev"], c["credit"]
    return {"Action": legs_text(c), "Verdict": a["verdict"],
            "Entry": f"credit ₹{abs(ev['V0']):.1f}" if cr else f"buy ₹{ev['V0']:.1f}",
            "Target": f"buy-back ₹{abs(ev['t1']['V']):.1f}" if cr else f"sell ₹{ev['t1']['V']:.1f}",
            "Stop loss": f"buy-back ₹{abs(ev['sl']['V']):.1f}" if cr else f"exit ₹{ev['sl']['V']:.1f}"}


def margin_check(kite, legs, lot, dex, nlots):
    params = [dict(exchange=dex, tradingsymbol=l["sym"], transaction_type="BUY" if l["s"] > 0 else "SELL",
                   variety="regular", product="NRML", order_type="MARKET", quantity=int(lot * nlots)) for l in legs]
    res = kite.basket_order_margins(params)  # sirf margin calculate karta hai, order place nahi hota
    fin = res.get("final") or res.get("initial") or {}
    return fin.get("total")


def render_card(a, ctx, hkey):
    c = a["card"]
    ev, cr, lot = c["ev"], c["credit"], ctx["lot"]
    with st.expander(f"{a['verdict']}  {legs_text(c)}", expanded=a["verdict"].startswith("✅")):
        labs = ("Net credit (sell price)", "Buy-back target T1", "Buy-back stop loss") if cr else \
               ("Entry (buy price)", "Target sell price T1", "Stop-loss price")
        x, y, z = st.columns(3)
        x.metric(labs[0], f"₹{abs(ev['V0']):.2f}")
        y.metric(labs[1], f"₹{abs(ev['t1']['V']):.2f}", f"index {ev['t1']['S']:,.0f}")
        z.metric(labs[2], f"₹{abs(ev['sl']['V']):.2f}", f"index {ev['sl']['S']:,.0f}")
        rows_ = [("Target 1", ev["t1"]), ("Target 2", ev.get("t2")), ("Market flat (time decay)", ev["flat"]), ("Stop loss", ev["sl"])]
        st.dataframe(pd.DataFrame([{"Scenario": n, "Index": round(v["S"], 1), "Structure price": round(abs(v["V"]), 2),
                                    "Net P&L/lot": round(v["net"])} for n, v in rows_ if v]), hide_index=True)
        txt = f"Lot {lot} | Max loss ≈ ₹{c['maxl']:,.0f}" + (f" | Max profit ≈ ₹{c['maxp']:,.0f}" if c["maxp"] else " (premium)")
        txt += f" | Net R:R {c['rr']:.2f}" if c["rr"] else " | Net R:R N/A"
        st.markdown(txt)
        st.markdown(f"📐 Risk budget ke hisaab se suggested size: **{c['lots']} lot**" +
                    (f" | short strike OTM expire hone ki approx probability ≈ {c['pop'] * 100:.0f}%" if c["pop"] else ""))
        if c.get("pt1") is not None:
            st.caption(f"Baseline touch probability (edge ke bina, random-walk maan kar): T1 ≈ {c['pt1'] * 100:.0f}%, SL ≈ {c['psl'] * 100:.0f}% - "
                       "signal ka edge isse upar hona chahiye.")
        if c["lots"] == 0:
            st.warning("Risk budget (capital × risk%) is trade ke max loss se chhota hai - size 0 lot. Risk % badhayein ya hedged spread chunein.")
        for l in c["legs"]:
            st.caption(f"{'BUY' if l['s'] > 0 else 'SELL'} {l['K']:.0f} {l['tag']} `{l['sym']}` ~₹{l['entry']:.2f} (IV {l['iv'] * 100:.1f}%)")
        for w in c["warn"]:
            st.warning(w)
        st.caption("Unvalidated, hypothetical scenario - backtest nahi hua.")
        mk = f"margin_{hkey}_{a['action']}"
        if st.button("💰 Margin check (Kite)", key="b_" + mk):
            try:
                tot = margin_check(ctx["kite"], c["legs"], lot, ctx["dex"], max(1, c["lots"]))
                st.session_state[mk] = f"≈ ₹{tot:,.0f} ({max(1, c['lots'])} lot)" if tot else "Margin data nahi mila."
            except Exception as e:
                st.session_state[mk] = f"Margin check fail ({type(e).__name__}) - broker terminal mein check karein."
        if st.session_state.get(mk):
            st.info("Estimated margin " + str(st.session_state[mk]) + " - order se pehle Zerodha mein confirm karein.")


def render_hz(res, ctx, hkey, spot):
    if res is None:
        st.info("Is horizon ke liye historical data kam hai - signal generate nahi ho sakta.")
        return
    st.markdown(f"**View: {VIEW_LBL[res['view']]}**  |  Score {res['D']:+.1f} (signal ke liye ±{res['need']:g})  |  Grade **{res['grade']}**")
    if res["eq"] and not ctx.get("is_index"):
        e = res["eq"]
        st.markdown(f"**Equity (cash) - {e['side']}**")
        x, y, z = st.columns(3)
        x.metric("Zone", f"{min(e['zone']):,.2f} - {max(e['zone']):,.2f}")
        y.metric("Target 1 / 2", f"{e['t1']:,.2f} / {e['t2']:,.2f}")
        z.metric("Stop loss", f"{e['sl']:,.2f}")
        st.caption("Levels: ATR-based (SL 1×, T1 1.5×, T2 2.5× ATR) - hypothetical.")
    elif not ctx.get("is_index") and not res["eq"]:
        st.caption("Equity: abhi koi clear direction nahi - wait karein.")
    if res["err"]:
        st.info(res["err"])
        return
    st.caption(f"Expected 1σ move ≈ ±{res['m']:,.0f} pts | hold ≈ {res['hold']:.2f} din")
    st.dataframe(pd.DataFrame([action_row(a) for a in res["actions"]]), hide_index=True)
    for a in res["actions"]:
        if a["card"]:
            render_card(a, ctx, hkey)
        else:
            st.caption(f"⚪ {a['action']}: {a['reason']}")
    with st.expander("🧠 Logic: ye view kyun bana?"):
        st.dataframe(pd.DataFrame([(x[0], round(x[1], 1), x[2]) for x in res["f"]], columns=["Factor", "Score", "Basis"]), hide_index=True)
        st.caption("Targets/SL: SL = ATR (0.3σ-0.6σ ke beech), T1 = 1.5×SL (max 1σ), T2 = 2.5×SL (max 1.5σ); OI wall se cap. Prices Black-Scholes se reprice (T1 ~0.6, SL ~0.4, T2/flat poore hold par theta).")


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
kite = ensure_session()

with st.sidebar:
    st.header("⚙️ Settings")
    st.caption(f"👤 {st.session_state.get('user') or STORE.get('user') or 'Connected'}")
    if APP_PASSWORD and st.query_params.get("k"):
        st.caption("🔖 Is URL ko bookmark / Home screen par add karein - 30 din tak password nahi maangega.")
    cfg = dict(
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

expiry = None
if und:
    today = pd.Timestamp(datetime.now(IST).date())
    exps = sorted(deriv[(deriv.name == und) & deriv.instrument_type.isin(["CE", "PE"])
                        & (deriv.expiry >= today)].expiry.dropna().unique())
    if exps:
        expiry = pd.Timestamp(st.selectbox("Option expiry", exps, format_func=lambda d: pd.Timestamp(d).strftime("%d-%b-%Y")))
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
    qd = guarded(get_quotes, kite, (key,))
    if not qd or key not in qd:
        st.error("Live quote nahi mila. Market data permission/plan ya network check karein.")
        return
    q = qd[key]
    spot = q["last_price"]
    ohlc = q.get("ohlc") or {}
    prev = ohlc.get("close") or 0
    ts = q.get("last_trade_time") or q.get("timestamp")
    tok = int(row.instrument_token)
    h15, hday = guarded(get_history, kite, tok, "15minute", 10), guarded(get_history, kite, tok, "day", 400)
    d15 = add_indicators(h15) if h15 is not None and len(h15) >= 30 else None
    dday = add_indicators(hday) if hday is not None and len(hday) >= 30 else None
    rv = float(np.log(hday.close).diff().tail(20).std() * math.sqrt(252)) if hday is not None and len(hday) >= 22 else None
    is_open = market_open()

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

    fut_info, bu = None, None
    if und:
        try:
            fut = deriv[(deriv.name == und) & (deriv.instrument_type == "FUT")
                        & (deriv.expiry >= pd.Timestamp(now.date()))].sort_values("expiry")
            if not fut.empty:
                f0 = fut.iloc[0]
                fk = f"{f0.exchange}:{f0.tradingsymbol}"
                fq = (guarded(get_quotes, kite, (fk,)) or {}).get(fk)
                if fq:
                    oc = guarded(get_oi_change, kite, int(f0.instrument_token))
                    fp = (fq.get("ohlc") or {}).get("close") or 0
                    fchg = (fq["last_price"] - fp) / fp * 100 if fp else 0
                    bu = buildup(fchg, oc[1] if oc else None)
                    fut_info = (f0.tradingsymbol, fq, oc, fchg)
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

    ctx = dict(spot=spot, T=T, cfg=cfg, rows=rows, strikes=strikes, stats=stats, atm_iv=atm_iv, regime=regime, bu=bu,
               news=nws, glb=glb, oiw=oiw, lot=lot, basis=s_eff - spot, dex=dex, dte=dte, kite=kite, is_index=is_index)
    res = {}
    for hname, hz in HZ.items():
        dd = d15 if hname == "Intraday" else dday
        try:
            res[hname] = horizon_view(hname, hz, dd, ctx) if dd is not None else None
        except Exception as e:
            res[hname] = None
            st.warning(f"{hname} analysis error ({type(e).__name__}): {str(e)[:120]}")

    tabs = st.tabs(["📊 Overview", "🎯 Trade Idea", "🌍 News & Global", "⛓ Option Chain", "ℹ️ Logic"])

    with tabs[0], section("Overview"):
        ri, rs = res.get("Intraday"), res.get("Swing (2-3 din / expiry)")
        main = rs or ri
        if main:
            txt = f"**Market push: {VIEW_LBL[main['view']]}**"
            if ri and rs:
                txt += f"  \nIntraday: {VIEW_LBL[ri['view']]} ({ri['D']:+.1f}) | Swing: {VIEW_LBL[rs['view']]} ({rs['D']:+.1f})"
            aligned = [f"{x[0]} ({x[1]:+.1f})" for x in main["f"] if x[1] * main["view"] > 0][:4] if main["view"] else []
            if aligned:
                txt += "  \nWajah: " + ", ".join(aligned)
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
        st.caption(f"Last trade: {ts} | Fetched: {now:%H:%M:%S} IST | Feed: {'🟢 LIVE' if is_open else '🟡 LAST CLOSE'}")
        lv = []
        if hday is not None and len(hday) >= 3:
            pcd = hday.iloc[-1] if not is_open else hday.iloc[-2]
            P = (pcd.high + pcd.low + pcd.close) / 3
            lv += [("Pivot", P), ("R1", 2 * P - pcd.low), ("S1", 2 * P - pcd.high),
                   ("20d High", hday.high.tail(20).max()), ("20d Low", hday.low.tail(20).min()),
                   ("52w High", hday.high.tail(250).max()), ("52w Low", hday.low.tail(250).min())]
            if dday is not None:
                lv.append(("ATR14 (daily)", float(dday.atr.iloc[-1])))
        if stats:
            lv += [("OI Resistance", stats["resistance"]), ("OI Support", stats["support"]), ("Max pain", stats["max_pain"])]
        if lv:
            st.dataframe(pd.DataFrame([(n, round(float(v), 2)) for n, v in lv], columns=["Key level", "Value"]), hide_index=True)
        if fut_info:
            name_, fq, oc, fchg = fut_info
            a, b, c = st.columns(3)
            a.metric(f"Future {name_}", f"{fq['last_price']:,.2f}", f"{fchg:+.2f}%")
            b.metric("Fut OI", f"{int(fq.get('oi') or 0):,}")
            c.metric("OI change", f"{oc[1]:+,.0f}" if oc else "N/A")
            if bu:
                st.caption("📌 " + bu[1])
        if atm_iv:
            st.caption(f"ATM IV {atm_iv * 100:.1f}%" + (f" | 20d realised vol {rv * 100:.1f}% | IV/RV {ratio:.2f} → options {regime}" if rv else ""))
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
                                     grade=r_["grade"], spot=spot, **{k.lower().replace(" ", "_"): v for k, v in rw.items()},
                                     rr=round(a_["card"]["rr"], 2) if a_["card"] and a_["card"]["rr"] else None))
            if recs:
                st.download_button("⬇️ Suggestions CSV (paper-trade log)", pd.DataFrame(recs).to_csv(index=False),
                                   file_name=f"nexion_{sym.replace(' ', '_')}_{now:%Y%m%d_%H%M}.csv", mime="text/csv", key="dl_csv")
        except Exception:
            pass

    with tabs[2], section("News & Global"):
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
        st.caption("Keyword sentiment crude hai; trade score mein iska weight sirf ±0.5 hai. Headlines khud bhi padhein.")

    with tabs[3], section("Option Chain"):
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

    with tabs[4], section("Logic"):
        st.markdown("""
**Observed (Zerodha):** LTP, OHLC, volume, depth, OI, historical candles, instrument master.
**Calculated (app):** EMA9/21, RSI14, ATR, PCR, max-OI support/resistance, max pain (dikhayi strikes window tak), IV + Greeks (Black-Scholes, forward-adjusted),
20-din realised vol, futures OI build-up, OI change at walls, 1σ expected move = spot × ATM IV × √time.
**Score weights:** Trend ±1, RSI ±1, OI change at walls ±1, Futures build-up ±1, PCR ±0.5 (theory), OI wall proximity ±0.5, News ±0.5, Global ±0.5.
Signal: |score| ≥ 2.5 (chain ke saath) ya ≥ 2 (chain ke bina). Grade A ≥4, B ≥3, C baaki.
**Strategy:** Bullish → Buy CE / Sell PE (hedged spread). Bearish → Buy PE / Sell CE (hedged). Sideways → Iron condor (agar options mehenge). Buy strike ≈ ATM (delta 0.5-0.55);
sell strike OI wall ke bahar (delta ≤0.30) + hedge leg. IV/RV ≥1.3 → selling preferred, <1.0 → buying preferred.
**Risk:** Costs ke baad R:R <1.2 ho to WEAK. Size = capital × risk% ÷ max loss. Margin check Kite API se (best-effort).
**Limitations:** Targets/SL hypothetical, backtest nahi. Costs mein exchange charges, GST, stamp duty nahi. Market holidays app ko nahi pata.
Zerodha token roz ~6 AM expire hota hai - din mein ek baar login. Educational analysis hai, financial advice nahi.""")


run_every = refresh if refresh > 0 else None
st.fragment(run_every=run_every)(dashboard)()
st.caption("Nexion Consulting | Analysis-only. Data: Zerodha Kite Connect.")
