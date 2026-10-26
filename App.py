"""Nexion F&O Terminal - Streamlit + Zerodha Kite Connect (analysis only, no orders)."""
import hmac
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.graph_objects as go
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
                    f"{tag} bid": bid, f"{tag} ask": ask, f"{tag} token": c.instrument_token,
                    f"{tag} sym": c.tradingsymbol, f"{tag} lot": c.lot_size, f"{tag} g": g, f"{tag} iv": iv})
    return pd.DataFrame(rows.values()).sort_values("Strike").reset_index(drop=True) if rows else pd.DataFrame()


def chain_stats(ch):
    ce, pe = ch.get("CE OI", pd.Series(dtype=float)).fillna(0), ch.get("PE OI", pd.Series(dtype=float)).fillna(0)
    if ce.sum() == 0 or pe.sum() == 0:
        return None
    k = ch.Strike.values
    pain = [float((np.maximum(x - k, 0) * ce.values).sum() + (np.maximum(k - x, 0) * pe.values).sum()) for x in k]
    return dict(pcr=pe.sum() / ce.sum(), resistance=float(ch.Strike[ce.idxmax()]),
                support=float(ch.Strike[pe.idxmax()]), max_pain=float(k[int(np.argmin(pain))]),
                ce_oi=float(ce.sum()), pe_oi=float(pe.sum()))


def score_signals(d, stats, spot):
    last, f = d.iloc[-1], []
    if last.ema9 > last.ema21 and last.close > last.ema21:
        f.append(("Trend (EMA9/EMA21)", 1, "EMA9 > EMA21 aur price EMA21 ke upar"))
    elif last.ema9 < last.ema21 and last.close < last.ema21:
        f.append(("Trend (EMA9/EMA21)", -1, "EMA9 < EMA21 aur price EMA21 ke neeche"))
    else:
        f.append(("Trend (EMA9/EMA21)", 0, "Mixed / sideways"))
    rsi = last.rsi
    if 55 < rsi <= 75:
        f.append(("Momentum (RSI14)", 1, f"RSI {rsi:.1f} > 55"))
    elif 25 <= rsi < 45:
        f.append(("Momentum (RSI14)", -1, f"RSI {rsi:.1f} < 45"))
    else:
        f.append(("Momentum (RSI14)", 0, f"RSI {rsi:.1f} (neutral ya extreme zone)"))
    if stats:
        p = stats["pcr"]
        f.append(("PCR (OI, chain window)", 1 if p > 1.2 else -1 if p < 0.8 else 0, f"PCR {p:.2f}"))
        near = 0.004 * spot
        if 0 <= spot - stats["support"] <= near:
            f.append(("Support/Resistance (max OI)", 1, f"Price max Put-OI support {stats['support']:.0f} ke paas"))
        elif 0 <= stats["resistance"] - spot <= near:
            f.append(("Support/Resistance (max OI)", -1, f"Price max Call-OI resistance {stats['resistance']:.0f} ke paas"))
        else:
            f.append(("Support/Resistance (max OI)", 0, f"Support {stats['support']:.0f} / Resistance {stats['resistance']:.0f} door hain"))
    return f


def num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


def trade_costs(entry, exit_, lot, c):
    return 2 * c["brok"] + c["stt"] / 100 * exit_ * lot + c["slip"] / 100 * (entry + exit_) * lot


def make_idea(direction, row, S, T, r, lot, atr, c):
    tag = "CE" if direction > 0 else "PE"
    K, iv, ltp = float(row["Strike"]), num(row.get(f"{tag} iv")), num(row.get(f"{tag} LTP"))
    if not iv or not ltp:
        return None, "ATM option ka IV calculate nahi ho paya (LTP/market data missing)."
    ask, bid = num(row.get(f"{tag} ask")), num(row.get(f"{tag} bid"))
    entry_hi = ask or ltp
    sgn = 1 if direction > 0 else -1
    spots = dict(t1=S + sgn * c["m1"] * atr, t2=S + sgn * c["m2"] * atr, sl=S - sgn * c["ms"] * atr)
    px = {k: max(round(bs(v, K, max(T - c["hold"] / 8760, 1 / 525600), r, iv, tag), 2), 0.05) for k, v in spots.items()}
    risk_net = -((px["sl"] - entry_hi) * lot - trade_costs(entry_hi, px["sl"], lot, c))
    t1_net = (px["t1"] - entry_hi) * lot - trade_costs(entry_hi, px["t1"], lot, c)
    t2_net = (px["t2"] - entry_hi) * lot - trade_costs(entry_hi, px["t2"], lot, c)
    spread = (ask - bid) / ask * 100 if ask and bid else None
    return dict(tag=tag, K=K, sym=row.get(f"{tag} sym"), ltp=ltp, entry_lo=ltp, entry_hi=entry_hi, px=px, spots=spots,
                iv=iv * 100, g=row.get(f"{tag} g"), lot=lot, t1_net=t1_net, t2_net=t2_net, risk_net=risk_net,
                rr=(t1_net / risk_net if risk_net > 0 and t1_net > 0 else None), spread=spread,
                vol=num(row.get(f"{tag} Vol"))), None


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
kite = ensure_session()

with st.sidebar:
    st.header("⚙️ Settings")
    st.caption(f"👤 {st.session_state.get('user') or STORE.get('user') or 'Connected'}")
    cfg = dict(
        r=st.number_input("Risk-free rate % (Greeks)", 0.0, 15.0, 6.5, 0.1) / 100,
        n=st.slider("Option chain: strikes ±ATM", 4, 20, 8),
        hold=st.number_input("Holding period (hours)", 0.25, 48.0, 2.0, 0.25),
        m1=st.number_input("Target 1 (x ATR)", 0.5, 5.0, 1.5, 0.25),
        m2=st.number_input("Target 2 (x ATR)", 1.0, 8.0, 2.5, 0.25),
        ms=st.number_input("Stop loss (x ATR)", 0.25, 3.0, 1.0, 0.25),
        slip=st.number_input("Slippage % per side", 0.0, 5.0, 0.5, 0.1),
        brok=st.number_input("Brokerage Rs per order", 0.0, 100.0, 20.0, 1.0),
        stt=st.number_input("STT % on option sell premium (verify current rate)", 0.0, 1.0, 0.1, 0.01),
        refresh=st.selectbox("Auto-refresh", [0, 5, 10, 30, 60], format_func=lambda x: "Off" if x == 0 else f"{x} sec"),
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

cash = inst[inst.exchange.isin(["NSE", "BSE"])]
deriv = inst[inst.exchange.isin(["NFO", "BFO"])]
deriv_names = set(deriv.name.dropna())
INDEX_MAP = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY", "NIFTY FIN SERVICE": "FINNIFTY",
             "NIFTY MID SELECT": "MIDCPNIFTY", "SENSEX": "SENSEX", "BANKEX": "BANKEX"}

c1, c2 = st.columns([3, 2])
q_txt = c1.text_input("🔍 Search (NIFTY 50, SENSEX, RELIANCE ...)", "NIFTY 50").strip().lower()
interval = c2.selectbox("Chart timeframe", ["5minute", "15minute", "60minute", "day"], index=1)
mask = (cash.tradingsymbol.str.lower().str.contains(q_txt, regex=False)
        | cash.name.fillna("").str.lower().str.contains(q_txt, regex=False))
m = cash[mask].copy()
if m.empty:
    st.warning("Koi instrument nahi mila.")
    st.stop()
m["rk"] = ((m.tradingsymbol.str.lower() != q_txt) * 4 + (m.segment != "INDICES") * 2 + (m.exchange != "NSE") * 1)
m = m.sort_values(["rk", "tradingsymbol"]).head(60)
pick = st.selectbox("Instrument", list(m.index),
                    format_func=lambda i: f"{m.at[i, 'exchange']}:{m.at[i, 'tradingsymbol']} - {m.at[i, 'name'] or ''}")
row = m.loc[pick]
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
    st.caption("ℹ️ Is instrument par F&O contracts available nahi hain - sirf cash analysis dikhega.")

DAYS = {"5minute": 5, "15minute": 10, "60minute": 30, "day": 365}


def dashboard():
    now = datetime.now(IST)
    qd = guarded(get_quotes, kite, (key,))
    if not qd or key not in qd:
        st.error("Live quote nahi mila. Market data permission/plan ya network check karein.")
        return
    q = qd[key]
    spot = q["last_price"]
    prev = (q.get("ohlc") or {}).get("close") or 0
    ts = q.get("last_trade_time") or q.get("timestamp")
    hist = guarded(get_history, kite, int(row.instrument_token), interval, DAYS[interval])
    d = add_indicators(hist) if hist is not None and len(hist) >= 30 else None
    is_open = market_open()

    # chain
    ch, stats, T = pd.DataFrame(), None, None
    if expiry is not None:
        T = years_to_expiry(expiry)
        ex_df = deriv[(deriv.name == und) & (deriv.expiry == expiry) & deriv.instrument_type.isin(["CE", "PE"])]
        strikes = sorted(ex_df.strike.unique())
        if strikes:
            i = int(np.argmin([abs(s - spot) for s in strikes]))
            win = set(strikes[max(0, i - cfg["n"]): i + cfg["n"] + 1])
            sel = ex_df[ex_df.strike.isin(win)]
            keys = tuple(f"{e}:{s}" for e, s in zip(sel.exchange, sel.tradingsymbol))
            cq = guarded(get_quotes, kite, keys)
            if cq:
                ch = build_chain(cq, sel, spot, T, cfg["r"])
                stats = chain_stats(ch) if not ch.empty else None

    tabs = st.tabs(["📊 Overview", "🕯 Chart", "⛓ Option Chain", "🎯 Trade Idea", "🌐 Indices", "ℹ️ Logic"])

    with tabs[0]:
        if not is_open:
            st.warning("Market abhi band hai (9:15-15:30 IST, Mon-Fri; holidays app nahi jaanta). Data last available hai, live nahi.")
        a, b, c = st.columns(3)
        a.metric(sym, f"{spot:,.2f}", f"{spot - prev:+,.2f} ({(spot - prev) / prev * 100:+.2f}%)" if prev else None)
        ohlc = q.get("ohlc") or {}
        b.metric("Open / High", f"{ohlc.get('open', 0):,.2f} / {ohlc.get('high', 0):,.2f}")
        c.metric("Low / Prev close", f"{ohlc.get('low', 0):,.2f} / {prev:,.2f}")
        a, b, c = st.columns(3)
        a.metric("Volume", f"{q.get('volume', 0):,}" if q.get("volume") else "N/A (index)")
        b.metric("Avg price", f"{q['average_price']:,.2f}" if q.get("average_price") else "N/A")
        c.metric("Circuit L / U", f"{q.get('lower_circuit_limit') or 0:,.2f} / {q.get('upper_circuit_limit') or 0:,.2f}")
        st.caption(f"Last trade: {ts} | Fetched: {now:%H:%M:%S} IST | Feed: {'🟢 LIVE' if is_open else '🟡 LAST CLOSE'}")
        if q.get("depth"):
            with st.expander("Market depth (top 5)"):
                dp = q["depth"]
                st.dataframe(pd.DataFrame({"Bid qty": [x["quantity"] for x in dp["buy"]], "Bid": [x["price"] for x in dp["buy"]],
                                           "Ask": [x["price"] for x in dp["sell"]], "Ask qty": [x["quantity"] for x in dp["sell"]]}))
        if und:
            fut = deriv[(deriv.name == und) & (deriv.instrument_type == "FUT")
                        & (deriv.expiry >= pd.Timestamp(now.date()))].sort_values("expiry")
            if not fut.empty:
                f0 = fut.iloc[0]
                fq = (guarded(get_quotes, kite, (f"{f0.exchange}:{f0.tradingsymbol}",)) or {}).get(f"{f0.exchange}:{f0.tradingsymbol}")
                if fq:
                    oc = guarded(get_oi_change, kite, int(f0.instrument_token))
                    st.markdown(f"**Near-month Future** `{f0.tradingsymbol}`")
                    a, b, c = st.columns(3)
                    a.metric("Fut LTP", f"{fq['last_price']:,.2f}", f"basis {fq['last_price'] - spot:+,.2f}")
                    b.metric("Fut OI", f"{int(fq.get('oi') or 0):,}")
                    c.metric("OI change (vs prev day candle)", f"{oc[1]:+,.0f}" if oc else "N/A")
        if not is_index and d is not None:
            st.markdown("**Equity levels (ATR-based, hypothetical)**")
            atr, last = d.atr.iloc[-1], d.iloc[-1]
            if last.ema9 > last.ema21 and last.close > last.ema21:
                a, b, c = st.columns(3)
                a.metric("Buy zone", f"{spot - 0.25 * atr:,.2f} - {spot:,.2f}")
                b.metric("Targets", f"{spot + cfg['m1'] * atr:,.2f} / {spot + cfg['m2'] * atr:,.2f}")
                c.metric("Stop loss", f"{spot - cfg['ms'] * atr:,.2f}")
            else:
                st.info("Trend bullish nahi hai - fresh long ke liye reliable setup nahi.")

    with tabs[1]:
        if d is None:
            st.info("Chart ke liye kam se kam 30 candles chahiye - historical data nahi mila.")
        else:
            x = d.date if interval == "day" else d.date.dt.strftime("%d-%b %H:%M")
            fig = go.Figure(go.Candlestick(x=x, open=d.open, high=d.high, low=d.low, close=d.close, name=sym))
            fig.add_scatter(x=x, y=d.ema9, name="EMA9", line=dict(width=1))
            fig.add_scatter(x=x, y=d.ema21, name="EMA21", line=dict(width=1))
            fig.update_layout(height=430, xaxis_rangeslider_visible=False, margin=dict(l=0, r=0, t=10, b=0),
                              legend=dict(orientation="h"))
            if interval != "day":
                fig.update_xaxes(type="category", nticks=6)
            st.plotly_chart(fig, use_container_width=True)
            l = d.iloc[-1]
            st.caption(f"RSI14 {l.rsi:.1f} | ATR14 {l.atr:.2f} | EMA9 {l.ema9:,.2f} | EMA21 {l.ema21:,.2f}")

    with tabs[2]:
        if ch.empty:
            st.info("Option chain available nahi (F&O nahi hai ya data nahi mila).")
        else:
            cols = ["CE OI", "CE Vol", "CE IV%", "CE Delta", "CE LTP", "Strike", "PE LTP", "PE Delta", "PE IV%", "PE Vol", "PE OI"]
            st.dataframe(ch[[c for c in cols if c in ch.columns]].round(2), height=420)
            if stats:
                a, b, c, e = st.columns(4)
                a.metric("PCR (OI)", f"{stats['pcr']:.2f}")
                b.metric("Max Call OI (resist.)", f"{stats['resistance']:.0f}")
                c.metric("Max Put OI (support)", f"{stats['support']:.0f}")
                e.metric("Max pain (window)", f"{stats['max_pain']:.0f}")
            st.caption(f"Expiry {expiry:%d-%b-%Y} | ±{cfg['n']} strikes. IV/Delta = app ki calculation (Black-Scholes), Zerodha ka data nahi. "
                       "Day OI-change sirf selected ATM contracts ke liye Trade Idea tab mein.")

    with tabs[3]:
        st.warning("⚠️ Ye hypothetical analysis hai, trading advice ya profit guarantee nahi. Orders ye app place nahi karta.")
        if d is None:
            st.info("Signal generate nahi ho sakta: historical data kam hai.")
        else:
            factors = score_signals(d, stats, spot)
            total, need = sum(f[1] for f in factors), (3 if stats else 2)
            st.dataframe(pd.DataFrame(factors, columns=["Factor", "Score", "Observation"]), hide_index=True)
            st.markdown(f"**Net score: {total:+d}** (signal ke liye |score| ≥ {need} chahiye)")
            if abs(total) < need:
                st.info("🟡 Koi reliable signal nahi - indicators ek direction mein align nahi hain. Trade avoid karein.")
            elif ch.empty or und is None:
                st.info("Directional bias hai, lekin option chain data nahi - CE/PE suggestion generate nahi ho sakta.")
            else:
                direction = 1 if total > 0 else -1
                atm = ch.iloc[int(np.argmin((ch.Strike - spot).abs()))]
                lot = num(atm.get("CE lot" if direction > 0 else "PE lot"))
                idea, why = make_idea(direction, atm, spot, T, cfg["r"], int(lot or 1), float(d.atr.iloc[-1]), cfg)
                if idea is None:
                    st.info(f"Signal hai par suggestion nahi ban saka: {why}")
                else:
                    tag = idea["tag"]
                    st.subheader(f"{'🟢' if tag == 'CE' else '🔴'} BUY {und} {idea['K']:.0f} {tag} ({expiry:%d-%b})")
                    st.caption(f"Contract: {idea['sym']} | Lot {idea['lot']} | IV {idea['iv']:.1f}%")
                    a, b = st.columns(2)
                    a.metric("Entry zone (LTP-Ask)", f"{idea['entry_lo']:.2f} - {idea['entry_hi']:.2f}")
                    b.metric("Stop loss (premium)", f"{idea['px']['sl']:.2f}", f"underlying {idea['spots']['sl']:,.0f}")
                    a, b = st.columns(2)
                    a.metric("Target 1", f"{idea['px']['t1']:.2f}", f"underlying {idea['spots']['t1']:,.0f}")
                    b.metric("Target 2", f"{idea['px']['t2']:.2f}", f"underlying {idea['spots']['t2']:,.0f}")
                    st.markdown(f"Per lot (costs+slippage ke baad): T1 **₹{idea['t1_net']:,.0f}**, T2 **₹{idea['t2_net']:,.0f}**, "
                                f"SL par loss **₹{idea['risk_net']:,.0f}** → net R:R "
                                f"**{idea['rr']:.2f}**" if idea["rr"] else "Net R:R calculate nahi ho paya.")
                    if idea["g"]:
                        g = idea["g"]
                        st.caption(f"Greeks: Δ {g['delta']:.2f} | Γ {g['gamma']:.5f} | Θ/day {g['theta']:.2f} | Vega/1% {g['vega']:.2f}")
                    if idea["rr"] is None or idea["rr"] < 1.2:
                        st.error("❌ Costs ke baad risk:reward 1.2 se kam hai - ye setup NO-TRADE maana jaye.")
                    if idea["spread"] and idea["spread"] > 2:
                        st.warning(f"Bid-ask spread {idea['spread']:.1f}% - liquidity/slippage risk zyada.")
                    if not idea["vol"]:
                        st.warning("Is contract mein volume nahi dikh raha - illiquid ho sakta hai.")
                    oc = guarded(get_oi_change, kite, int(atm[f"{tag} token"]))
                    st.caption(f"Contract OI: {oc[0]:,.0f}, change vs prev day candle: {oc[1]:+,.0f}" if oc else "OI change data nahi mila.")
                    st.markdown("**Logic**\n" + "\n".join(f"- {f[0]}: {f[2]} ({f[1]:+d})" for f in factors)
                                + f"\n- Targets/SL: underlying ATR14({interval}) x {cfg['m1']}/{cfg['m2']}/{cfg['ms']}; "
                                  f"option price same IV par Black-Scholes se reprice, {cfg['hold']}h theta ke saath.")

    with tabs[4]:
        ix = inst[(inst.segment == "INDICES")]
        ikeys = tuple(f"{e}:{s}" for e, s in zip(ix.exchange, ix.tradingsymbol))
        iq = guarded(get_quotes, kite, ikeys) if ikeys else None
        if iq:
            rows = []
            for k, v in iq.items():
                pc = (v.get("ohlc") or {}).get("close") or 0
                rows.append({"Index": k, "LTP": v["last_price"], "Chg %": (v["last_price"] - pc) / pc * 100 if pc else None,
                             "High": (v.get("ohlc") or {}).get("high"), "Low": (v.get("ohlc") or {}).get("low")})
            st.dataframe(pd.DataFrame(rows).sort_values("Chg %", ascending=False).round(2), height=480, hide_index=True)
        else:
            st.info("Index data nahi mila.")

    with tabs[5]:
        st.markdown("""
**Observed (Zerodha se seedha):** LTP, OHLC, prev close, volume, avg price, depth, circuit limits, OI, historical candles.
**Calculated (is app mein):** EMA9/21, RSI14, ATR14, PCR, max-pain (sirf dikhayi gayi strikes window), IV (option LTP se Black-Scholes bisection),
Greeks (r = sidebar rate, dividend ignore, spot-based), OI change (daily candle OI diff), targets/SL, costs.
**Hypothetical:** Entry/targets/stop-loss - ATR-based scenario, backtest/validate nahi kiya gaya.
**Limitations:** Zerodha quote() mein IV/Greeks nahi aate; day OI-change quote mein direct nahi milta (isliye candle diff). Costs mein exchange charges, GST, stamp duty
shamil nahi. Market holidays app ko nahi pata. Streamlit Cloud restart par server-side token chala jata hai - dobara login karna hoga.
Ye educational analysis hai, financial advice nahi.""")


run_every = cfg["refresh"] if cfg["refresh"] > 0 else None
st.fragment(run_every=run_every)(dashboard)()
st.caption("Nexion Consulting | Analysis-only. Data: Zerodha Kite Connect.")
