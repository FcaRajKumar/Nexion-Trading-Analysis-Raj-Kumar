"""Nexion F&O Terminal - Streamlit + Zerodha Kite Connect (analysis only, no orders)."""
import hmac
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
    st.query_params.clear()  # request_token URL mein na rahe
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


def password_gate():
    if not APP_PASSWORD or st.session_state.get("gate_ok"):
        return
    st.title("🔒 Nexion F&O Terminal")
    pw = st.text_input("App password", type="password")
    if pw:
        if hmac.compare_digest(pw.encode(), str(APP_PASSWORD).encode()):
            st.session_state.gate_ok = True
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
        st.link_button("🔐 Zerodha se Login karein", kite.login_url(), type="primary")
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
                    f"{tag} lot": c.lot_size, f"{tag} iv": iv})
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


def score_view(d, stats, spot, bu, nws):
    last, f = d.iloc[-1], []
    if last.ema9 > last.ema21 and last.close > last.ema21:
        f.append(("Trend (EMA9/21)", 1, "EMA9 > EMA21, price EMA21 ke upar"))
    elif last.ema9 < last.ema21 and last.close < last.ema21:
        f.append(("Trend (EMA9/21)", -1, "EMA9 < EMA21, price EMA21 ke neeche"))
    else:
        f.append(("Trend (EMA9/21)", 0, "Mixed / sideways"))
    rsi = last.rsi
    if 55 < rsi <= 75:
        f.append(("Momentum (RSI14)", 1, f"RSI {rsi:.1f} > 55"))
    elif 25 <= rsi < 45:
        f.append(("Momentum (RSI14)", -1, f"RSI {rsi:.1f} < 45"))
    else:
        f.append(("Momentum (RSI14)", 0, f"RSI {rsi:.1f} neutral/extreme"))
    if stats:
        p = stats["pcr"]
        f.append(("PCR (OI)", 1 if p > 1.2 else -1 if p < 0.8 else 0, f"PCR {p:.2f}"))
        near = 0.004 * spot
        if 0 <= spot - stats["support"] <= near:
            f.append(("OI support/resistance", 1, f"Price max Put-OI support {stats['support']:.0f} ke paas"))
        elif 0 <= stats["resistance"] - spot <= near:
            f.append(("OI support/resistance", -1, f"Price max Call-OI resistance {stats['resistance']:.0f} ke paas"))
        else:
            f.append(("OI support/resistance", 0, f"Support {stats['support']:.0f} / Resistance {stats['resistance']:.0f}"))
    if bu:
        f.append(("Futures OI build-up", bu[0], bu[1]))
    if nws:
        f.append(("News sentiment", nws[0], nws[1]))
    return f


def make_leg(rows, K, tag, sgn):
    r = rows.get(K)
    if r is None:
        return None
    ltp, iv = num(r.get(f"{tag} LTP")), num(r.get(f"{tag} iv"))
    if not ltp or not iv:
        return None
    ask, bid = num(r.get(f"{tag} ask")), num(r.get(f"{tag} bid"))
    return dict(s=sgn, tag=tag, K=K, iv=iv, ltp=ltp, entry=(ask or ltp) if sgn > 0 else (bid or ltp),
                sym=r.get(f"{tag} sym"), vol=num(r.get(f"{tag} Vol")),
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


HZ = {"Intraday": dict(move=0.5, hold=0.25, db=0.50, ds=0.25),
      "Positional (2-5 din)": dict(move=3.0, hold=3.0, db=0.55, ds=0.25),
      "Till expiry": dict(move=None, hold=None, db=0.60, ds=0.20)}


def dir_scen(S, m, sgn, wall, hold):
    t1, t2, note = S + sgn * 0.75 * m, S + sgn * 1.25 * m, None
    if wall is not None:
        if sgn > 0 and wall > S:
            t1, t2 = min(t1, wall), min(t2, wall)
        if sgn < 0 and wall < S:
            t1, t2 = max(t1, wall), max(t2, wall)
        if abs(wall - S) < 0.25 * m:
            note = f"⚠️ OI wall {wall:.0f} bahut paas hai - move limited reh sakta hai."
    return {"t1": (t1, hold), "t2": (t2, hold), "flat": (S, hold), "sl": (S - sgn * 0.5 * m, hold * 0.5)}, note


def horizon_view(hz, d, ctx):
    S, T, cfg, rows, strikes, stats = ctx["spot"], ctx["T"], ctx["cfg"], ctx["rows"], ctx["strikes"], ctx["stats"]
    f = score_view(d, stats, S, ctx["bu"], ctx["news"])
    D, need = sum(x[1] for x in f), (3 if stats else 2)
    view = 1 if D >= need else -1 if D <= -need else 0
    out = dict(f=f, D=D, need=need, view=view, cards=[], skips=[])
    sig = ctx["atm_iv"]
    if not sig:
        out["err"] = "ATM IV calculate nahi ho paya."
        return out
    tdays = T * 365
    move = min(hz["move"] or tdays, tdays)
    hold = min(hz["hold"] or 0.75 * tdays, 0.8 * tdays)
    m = S * sig * math.sqrt(move / 365)
    out["m"], out["hold"] = m, hold
    regime = ctx["regime"]

    def add(title, legs, scen, notes, pref):
        ev = struct_eval(legs, ctx["lot"], T, scen, cfg, ctx["basis"])
        if "dn" in ev:
            ev["sl"] = min(ev["up"], ev["dn"], key=lambda x: x["net"])
        risk = -ev["sl"]["net"]
        rr = ev["t1"]["net"] / risk if risk > 0 and ev["t1"]["net"] > 0 else None
        warn = list(notes)
        for l in legs:
            if not l["vol"]:
                warn.append(f"{l['K']:.0f} {l['tag']}: volume nahi dikh raha (illiquid ho sakta hai).")
            if l["spread"] and l["spread"] > 3:
                warn.append(f"{l['K']:.0f} {l['tag']}: bid-ask spread {l['spread']:.1f}% - slippage risk.")
        if rr is None or rr < 1.2:
            pref = "⚪ Weak R:R (costs ke baad) - skip"
        credit = ev["V0"] < 0
        width = max(abs(legs[0]["K"] - legs[1]["K"]), abs(legs[-1]["K"] - legs[-2]["K"])) if len(legs) >= 2 else None
        maxp = -ev["V0"] * ctx["lot"] if credit else None
        maxl = (width + ev["V0"]) * ctx["lot"] if credit else ev["V0"] * ctx["lot"]
        out["cards"].append(dict(title=title, legs=legs, ev=ev, rr=rr, warn=warn, pref=pref, maxp=maxp, maxl=maxl))

    def pref_for(kind):
        if regime == "rich":
            return "⭐ Preferred (premium mehenga)" if kind == "sell" else "Alternative"
        if regime == "cheap":
            return "⭐ Preferred (premium sasta)" if kind == "buy" else "Alternative"
        return "Valid setup"

    if view != 0:
        tag_b, tag_s, sgn = ("CE", "PE", 1) if view > 0 else ("PE", "CE", -1)
        wall = (stats["resistance"] if view > 0 else stats["support"]) if stats else None
        scen, wnote = dir_scen(S, m, sgn, wall, hold)
        K = pick_by_delta(rows, strikes, tag_b, hz["db"])
        lg = make_leg(rows, K, tag_b, 1) if K else None
        if lg:
            add(f"BUY {lg['K']:.0f} {tag_b}", [lg], scen, [wnote] if wnote else [], pref_for("buy"))
        else:
            out["skips"].append(f"BUY {tag_b}: suitable strike/price nahi mila.")
        legs, notes = credit_spread(ctx, tag_s, hz["ds"])
        if legs:
            add(f"SELL {legs[0]['K']:.0f} {tag_s} + BUY {legs[1]['K']:.0f} {tag_s} (hedged)", legs, scen, notes, pref_for("sell"))
        else:
            out["skips"].append(f"SELL {tag_s} (hedged): {notes}")
        other = ("PE", "CE") if view > 0 else ("CE", "PE")
        out["skips"].append(f"BUY {other[0]} / SELL {other[1]}: current view ke against hai - abhi nahi.")
    else:
        for t in ("BUY CE", "SELL PE", "BUY PE", "SELL CE"):
            out["skips"].append(f"{t}: directional view clear nahi (score {D:+d}, chahiye ±{need}).")
        if regime == "cheap":
            out["skips"].append("Iron condor: options sasta (IV < realised vol) - premium bechne ka edge nahi.")
        else:
            ps, pn = credit_spread(ctx, "PE", hz["ds"])
            cs, cn = credit_spread(ctx, "CE", hz["ds"])
            if ps and cs:
                scen = {"t1": (S, hold), "flat": (S, hold), "up": (S + m, hold * 0.5), "dn": (S - m, hold * 0.5)}
                add("IRON CONDOR (hedged range sell): " + f"{ps[0]['K']:.0f}PE/{ps[1]['K']:.0f}PE + {cs[0]['K']:.0f}CE/{cs[1]['K']:.0f}CE",
                    ps + cs, scen, pn + cn + ["Range-bound view: spot short strikes ke beech rehna chahiye."],
                    "⭐ Preferred (premium mehenga)" if regime == "rich" else "Valid setup")
            else:
                out["skips"].append("Iron condor: dono spreads ka data nahi mila.")
    return out


def render_hz(res, lot, expiry_label):
    st.markdown(f"**View score {res['D']:+d}** (signal ke liye ±{res['need']}) → "
                + {1: "🟢 BULLISH", -1: "🔴 BEARISH", 0: "🟡 NEUTRAL / RANGE"}[res["view"]])
    if res.get("err"):
        st.info(res["err"])
        return
    st.caption(f"Expected 1σ move ≈ ±{res['m']:,.0f} points | assumed hold ≈ {res['hold']:.2f} din | expiry {expiry_label}")
    if not res["cards"]:
        st.info("🟡 Abhi koi reliable trade nahi - indicators align nahi hain. Trade avoid karein.")
    for c in res["cards"]:
        ev, legs = c["ev"], c["legs"]
        st.markdown(f"#### {c['title']}")
        st.caption(c["pref"])
        for l in legs:
            st.markdown(f"- **{'BUY' if l['s'] > 0 else 'SELL'} {l['K']:.0f} {l['tag']}** `{l['sym']}` @ ~₹{l['entry']:.2f} (IV {l['iv'] * 100:.1f}%)")
        credit = ev["V0"] < 0
        labs = ("Net credit (sell price)", "Buy-back target", "Buy-back stop loss") if credit else \
               ("Entry (buy price)", "Target sell price", "Stop-loss price")
        a, b, e = st.columns(3)
        a.metric(labs[0], f"₹{abs(ev['V0']):.2f}")
        b.metric(labs[1] + " (T1)", f"₹{abs(ev['t1']['V']):.2f}", f"index {ev['t1']['S']:,.0f}")
        e.metric(labs[2], f"₹{abs(ev['sl']['V']):.2f}", f"index {ev['sl']['S']:,.0f}")
        rows_ = [("Target 1", ev["t1"]), ("Target 2", ev.get("t2")), ("Market flat (time decay)", ev["flat"]), ("Stop loss", ev["sl"])]
        st.dataframe(pd.DataFrame([{"Scenario": n, "Index level": round(v["S"], 1), "Structure price": round(abs(v["V"]), 2),
                                    "Net P&L / lot (costs ke baad)": round(v["net"])} for n, v in rows_ if v]), hide_index=True)
        lim = f"Lot {lot}. Max loss ≈ ₹{c['maxl']:,.0f}" + (f", max profit ≈ ₹{c['maxp']:,.0f}" if c["maxp"] else " (premium)")
        st.markdown(lim + (f" | Net R:R **{c['rr']:.2f}**" if c["rr"] else " | Net R:R N/A"))
        if c["rr"] is None or c["rr"] < 1.2:
            st.error("❌ Costs ke baad R:R 1.2 se kam - NO-TRADE maanein.")
        for w in c["warn"]:
            st.warning(w)
    for s_ in res["skips"]:
        st.caption("⚪ " + s_)
    with st.expander("🧠 Logic: ye view kyun bana?"):
        st.dataframe(pd.DataFrame(res["f"], columns=["Factor", "Score", "Basis"]), hide_index=True)
        st.caption("Targets/SL = ATM IV se nikla 1σ move (T1 0.75σ, T2 1.25σ, SL 0.5σ), OI wall se capped; prices Black-Scholes se reprice. "
                   "Hypothetical scenario hain, validated signal nahi.")


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
kite = ensure_session()

with st.sidebar:
    st.header("⚙️ Settings")
    st.caption(f"👤 {st.session_state.get('user') or STORE.get('user') or 'Connected'}")
    cfg = dict(
        r=st.number_input("Risk-free rate % (Greeks)", 0.0, 15.0, 6.5, 0.1) / 100,
        n=st.slider("Option chain: strikes ±ATM", 4, 20, 10),
        slip=st.number_input("Slippage % per side", 0.0, 5.0, 0.5, 0.1),
        brok=st.number_input("Brokerage Rs per order", 0.0, 100.0, 20.0, 1.0),
        stt=st.number_input("STT % on option sell premium (verify current rate)", 0.0, 1.0, 0.1, 0.01),
        refresh=st.selectbox("Auto-refresh", [0, 10, 30, 60], format_func=lambda x: "Off" if x == 0 else f"{x} sec"),
    )
    if st.button("🔄 Refresh now"):
        st.cache_data.clear()
        st.rerun()
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
default_i = next((i for i, l in enumerate(labels) if l.startswith("NIFTY 50 ·")), 0)
choice = st.selectbox("🔍 Instrument - type karein (jaise rel, tcs, bank), list apne aap filter hogi",
                      labels, index=default_i)
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
    st.caption("ℹ️ Is instrument par F&O contracts nahi hain - sirf cash overview + news dikhega.")


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
    h15, hday = guarded(get_history, kite, tok, "15minute", 10), guarded(get_history, kite, tok, "day", 150)
    d15 = add_indicators(h15) if h15 is not None and len(h15) >= 30 else None
    dday = add_indicators(hday) if hday is not None and len(hday) >= 30 else None
    rv = float(np.log(hday.close).diff().tail(20).std() * math.sqrt(252)) if hday is not None and len(hday) >= 22 else None
    is_open = market_open()

    ch, stats, T, rows, strikes = pd.DataFrame(), None, None, {}, []
    s_eff, ps = spot, None
    if expiry is not None:
        T = years_to_expiry(expiry)
        ex_df = deriv[(deriv.name == und) & (deriv.expiry == expiry) & deriv.instrument_type.isin(["CE", "PE"])]
        allk = sorted(ex_df.strike.unique())
        if allk:
            i = int(np.argmin([abs(s - spot) for s in allk]))
            win = set(allk[max(0, i - cfg["n"]): i + cfg["n"] + 1])
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
    regime = "rich" if ratio and ratio > 1.2 else "cheap" if ratio and ratio < 0.9 else "fair" if ratio else None

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

    ctx = dict(spot=spot, T=T, cfg=cfg, rows=rows, strikes=strikes, stats=stats, atm_iv=atm_iv,
               regime=regime, bu=bu, news=nws, lot=lot, basis=s_eff - spot)
    tabs = st.tabs(["📊 Overview", "🎯 Trade Ideas", "📰 News", "⛓ Option Chain", "🌐 Indices", "ℹ️ Logic"])

    with tabs[0], section("Overview"):
        if not is_open:
            st.warning("Market abhi band hai (9:15-15:30 IST, Mon-Fri; holidays app nahi jaanta). Data last available hai.")
        a, b, c = st.columns(3)
        a.metric(sym, f"{spot:,.2f}", f"{spot - prev:+,.2f} ({(spot - prev) / prev * 100:+.2f}%)" if prev else None)
        b.metric("Open / High", f"{ohlc.get('open', 0):,.2f} / {ohlc.get('high', 0):,.2f}")
        c.metric("Low / Prev close", f"{ohlc.get('low', 0):,.2f} / {prev:,.2f}")
        a, b, c = st.columns(3)
        a.metric("Volume", f"{q.get('volume', 0):,}" if q.get("volume") else "N/A (index)")
        b.metric("Avg price", f"{q['average_price']:,.2f}" if q.get("average_price") else "N/A")
        c.metric("Circuit L / U", f"{q.get('lower_circuit_limit') or 0:,.2f} / {q.get('upper_circuit_limit') or 0:,.2f}")
        st.caption(f"Last trade: {ts} | Fetched: {now:%H:%M:%S} IST | Feed: {'🟢 LIVE' if is_open else '🟡 LAST CLOSE'}")
        if fut_info:
            name_, fq, oc, fchg = fut_info
            st.markdown(f"**Near-month Future** `{name_}`")
            a, b, c = st.columns(3)
            a.metric("Fut LTP", f"{fq['last_price']:,.2f}", f"{fchg:+.2f}%")
            b.metric("Fut OI", f"{int(fq.get('oi') or 0):,}")
            c.metric("OI change", f"{oc[1]:+,.0f}" if oc else "N/A")
            if bu:
                st.caption("📌 " + bu[1])
        if atm_iv:
            st.caption(f"ATM IV {atm_iv * 100:.1f}%" + (f" | 20d realised vol {rv * 100:.1f}% | options {regime}" if rv else ""))
        if q.get("depth"):
            with st.expander("Market depth (top 5)"):
                dp = q["depth"]
                st.dataframe(pd.DataFrame({"Bid qty": [x["quantity"] for x in dp["buy"]], "Bid": [x["price"] for x in dp["buy"]],
                                           "Ask": [x["price"] for x in dp["sell"]], "Ask qty": [x["quantity"] for x in dp["sell"]]}))

    with tabs[1], section("Trade Ideas"):
        st.warning("⚠️ Hypothetical analysis hai - advice ya profit guarantee nahi. App koi order place nahi karta. "
                   "Selling hamesha hedged dikhayi gayi hai.")
        if expiry is None or not rows:
            st.info("Trade ideas ke liye F&O chain chahiye - is instrument par available nahi ya data nahi mila.")
        elif dday is None:
            st.info("Signal generate nahi ho sakta: historical data kam hai.")
        else:
            hts = st.tabs(list(HZ))
            for tab_, (hname, hz) in zip(hts, HZ.items()):
                with tab_, section(hname):
                    dd = d15 if (hname == "Intraday" and d15 is not None) else dday
                    render_hz(horizon_view(hz, dd, ctx), lot, f"{expiry:%d-%b-%Y}")

    with tabs[2], section("News"):
        if nerr:
            st.warning(nerr)
        src = "instrument-specific" if len(items) >= 3 else "market-wide (instrument news kam mila)"
        st.caption(f"Query: {news_q} | Source: Google News RSS | Sentiment source: {src}")
        if nws:
            st.markdown(f"**Sentiment score: {nws[0]:+d}** - {nws[1]}")
        for it in nsrc[:12]:
            age = f"{int((now - it['when']).total_seconds() // 3600)}h" if it["when"] else ""
            st.markdown(f"{'🟢' if it['score'] > 0 else '🔴' if it['score'] < 0 else '⚪'} [{it['title']}]({it['link']}) {age}")
        st.caption("Keyword-based sentiment crude hai (sarcasm/context nahi samajhta); iska weight sirf ±1 hai. Headlines khud bhi padhein.")

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
            if not (lo <= spot <= hi):
                st.error("⚠️ Spot chain ke strike range ke bahar hai - data mismatch ho sakta hai.")
            atm_k = min(strikes, key=lambda k: abs(k - spot))
            cols = ["CE OI", "CE Vol", "CE IV%", "CE Delta", "CE LTP", "Strike", "PE LTP", "PE Delta", "PE IV%", "PE Vol", "PE OI"]
            show = ch[[c for c in cols if c in ch.columns]].round(2)
            styled = show.style.apply(lambda r_: ["background-color: rgba(255,200,0,0.25)" if r_["Strike"] == atm_k else "" for _ in r_], axis=1)
            st.dataframe(styled, height=min(38 + 35 * len(show), 900), hide_index=True)
            st.caption(f"Peeli row = ATM {atm_k:.0f}. Poori chain dikh rahi hai (andar scroll nahi).")
            if stats:
                a_, b_, c_, e_ = st.columns(4)
                a_.metric("PCR (OI)", f"{stats['pcr']:.2f}")
                b_.metric("Max Call OI (resist.)", f"{stats['resistance']:.0f}")
                c_.metric("Max Put OI (support)", f"{stats['support']:.0f}")
                e_.metric("Max pain (window)", f"{stats['max_pain']:.0f}")

    with tabs[4], section("Indices"):
        ix = inst[(inst.segment == "INDICES")]
        ikeys = tuple(f"{e}:{s}" for e, s in zip(ix.exchange, ix.tradingsymbol))
        iq = guarded(get_quotes, kite, ikeys) if ikeys else None
        if iq:
            rws = []
            for k, v in iq.items():
                pc = (v.get("ohlc") or {}).get("close") or 0
                rws.append({"Index": k, "LTP": v["last_price"], "Chg %": (v["last_price"] - pc) / pc * 100 if pc else None,
                            "High": (v.get("ohlc") or {}).get("high"), "Low": (v.get("ohlc") or {}).get("low")})
            st.dataframe(pd.DataFrame(rws).sort_values("Chg %", ascending=False).round(2), height=480, hide_index=True)
        else:
            st.info("Index data nahi mila.")

    with tabs[5], section("Logic"):
        st.markdown("""
**Observed (Zerodha):** LTP, OHLC, volume, depth, OI, historical candles, instrument master.
**Calculated (app):** EMA9/21, RSI14, PCR, max-OI support/resistance, max pain (sirf dikhayi strikes window), IV + Greeks (Black-Scholes, dividend ignore),
20-day realised vol, futures OI build-up (daily candle OI diff), 1σ expected move = spot × ATM IV × √time.
**News:** Google News RSS headlines + keyword sentiment (low confidence, weight ±1).
**Strategy rules:** Bullish → Buy CE / Sell PE spread; Bearish → Buy PE / Sell CE spread; Neutral → iron condor (agar options mehenge). Buy vs sell ka preference
ATM IV / realised vol se: >1.2 mehenga (sell), <0.9 sasta (buy). Sell side hamesha hedge leg ke saath. Costs ke baad R:R < 1.2 ho to NO-TRADE.
**Limitations:** Targets/SL hypothetical scenarios hain, backtested nahi. Costs mein exchange charges, GST, stamp duty nahi. Margin requirement app nahi dikhata.
Market holidays app ko nahi pata. Streamlit Cloud restart par token chala jata hai. Ye educational analysis hai, financial advice nahi.""")


run_every = cfg["refresh"] if cfg["refresh"] > 0 else None
st.fragment(run_every=run_every)(dashboard)()
st.caption("Nexion Consulting | Analysis-only. Data: Zerodha Kite Connect.")
