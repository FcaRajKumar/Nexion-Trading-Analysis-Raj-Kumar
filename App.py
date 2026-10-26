import streamlit as st
from kiteconnect import KiteConnect
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timedelta
import math

# Page Configuration
st.set_page_config(
    page_title="Nexion Professional Trading Terminal",
    page_icon="📈",
    layout="wide"
)

st.title("📈 Nexion Live Trading & F&O Intelligence Terminal")
st.markdown("### Production-Grade Zerodha Integration | Real-Time LTP, OI, Charts & Automated Targets")

# ---------------------------------------------------------
# 1. SECURE OAUTH GATEWAY (LOOP-FREE ARCHITECTURE)
# ---------------------------------------------------------
st.sidebar.header("🔐 Zerodha Kite Gateway")

api_key = st.sidebar.text_input("API Key", key="input_api_key", type="default")
api_secret = st.sidebar.text_input("API Secret", key="input_api_secret", type="password")

if "access_token" not in st.session_state:
    st.session_state.access_token = None
if "pending_request_token" not in st.session_state:
    st.session_state.pending_request_token = None

# Intercept request_token from URL and clear immediately to prevent redirect loops
query_params = dict(st.query_params)
if "request_token" in query_params:
    st.session_state.pending_request_token = query_params["request_token"]
    st.query_params.clear()
    st.rerun()

if api_key:
    try:
        kite = KiteConnect(api_key=api_key)
        login_url = kite.login_url()
        
        if st.session_state.pending_request_token and not st.session_state.access_token:
            if api_secret:
                try:
                    data = kite.generate_session(st.session_state.pending_request_token, api_secret=api_secret)
                    st.session_state.access_token = data["access_token"]
                    st.session_state.pending_request_token = None
                    st.sidebar.success("✅ Login Successful! Session Active.")
                    st.rerun()
                except Exception as e:
                    st.sidebar.error(f"❌ Session Gen Error: {str(e)}")
            else:
                st.sidebar.warning("⚠️ Enter your API Secret above to finalize session generation.")

        if not st.session_state.access_token:
            st.sidebar.markdown(f"""
                <div style="text-align: center; margin-top: 15px; margin-bottom: 15px;">
                    <a href="{login_url}" target="_self" style="background-color: #D32F2F; color: white; padding: 14px 20px; text-align: center; text-decoration: none; display: block; font-size: 16px; font-weight: bold; border-radius: 6px; box-shadow: 0 4px 6px rgba(0,0,0,0.3);">
                        🔴 Login with Zerodha Kite
                    </a>
                </div>
            """, unsafe_allow_html=True)
            st.sidebar.info("👆 Click to authenticate securely via mobile or browser.")
        else:
            st.sidebar.success("🟢 Active Session Connected via Kite API")
            if st.sidebar.button("Logout / Reset Session"):
                st.session_state.access_token = None
                st.session_state.pending_request_token = None
                st.query_params.clear()
                st.rerun()
    except Exception as e:
        st.sidebar.error(f"Kite Init Error: {e}")
else:
    st.sidebar.warning("⚠️ Enter your API Key above to activate the login gateway.")

st.sidebar.markdown("---")

# ---------------------------------------------------------
# 2. UNIVERSAL INSTRUMENT FETCHING & SEARCH
# ---------------------------------------------------------
default_instruments = {
    "SENSEX": {"exchange": "BSE", "tradingsymbol": "SENSEX", "token": 265},
    "NIFTY 50": {"exchange": "NSE", "tradingsymbol": "NIFTY 50", "token": 256265},
    "BANK NIFTY": {"exchange": "NSE", "tradingsymbol": "NIFTY BANK", "token": 260105},
    "RELIANCE": {"exchange": "NSE", "tradingsymbol": "RELIANCE", "token": 738561},
    "TCS": {"exchange": "NSE", "tradingsymbol": "TCS", "token": 2953215},
    "HDFC BANK": {"exchange": "NSE", "tradingsymbol": "HDFCBNK", "token": 341249},
    "INFY": {"exchange": "NSE", "tradingsymbol": "INFY", "token": 408065},
    "ITC": {"exchange": "NSE", "tradingsymbol": "ITC", "token": 424961},
    "SBIN": {"exchange": "NSE", "tradingsymbol": "SBIN", "token": 779521}
}

instrument_map = default_instruments

if st.session_state.access_token and api_key:
    try:
        k_client = KiteConnect(api_key=api_key)
        k_client.set_access_token(st.session_state.access_token)
        live_insts = k_client.instruments("NSE")
        if live_insts:
            for inst in live_insts:
                sym = inst['tradingsymbol']
                instrument_map[sym] = {
                    "exchange": inst['exchange'],
                    "tradingsymbol": sym,
                    "token": inst['instrument_token']
                }
    except Exception:
        pass

st.markdown("### 🔍 Live Script Selection & Decision Parameters")
col_s1, col_s2, col_s3 = st.columns([2, 1, 1])

with col_s1:
    selected_script = st.selectbox("Search or Select Equity / Index", options=list(instrument_map.keys()), index=0)
with col_s2:
    timeframe = st.selectbox("Chart Timeframe", ["3minute", "5minute", "15minute", "day"])
with col_s3:
    bias_view = st.selectbox("Strategy Bias", ["Bullish (Long)", "Bearish (Short)", "Neutral / Range"])

st.markdown("---")

# ---------------------------------------------------------
# 3. LIVE SYSTEM DATA FETCHING (LTP, QUOTE, OI, HISTORY)
# ---------------------------------------------------------
spot_price = 0.0
oi_val = 0
oi_change_val = 0.0
volume_val = 0
historical_df = pd.DataFrame()
is_live_connected = False

selected_info = instrument_map.get(selected_script, {"exchange": "NSE", "tradingsymbol": selected_script, "token": 256265})
symbol_key = f"{selected_info['exchange']}:{selected_info['tradingsymbol']}"

if st.session_state.access_token and api_key:
    try:
        k_client = KiteConnect(api_key=api_key)
        k_client.set_access_token(st.session_state.access_token)
        
        # Fetch Live Quote (LTP, OI, Volume)
        quote_res = k_client.quote([symbol_key])
        if symbol_key in quote_res:
            q_data = quote_res[symbol_key]
            spot_price = q_data["last_price"]
            volume_val = q_data.get("volume", 0)
            oi_val = q_data.get("oi", 0)
            oi_change_val = q_data.get("oi_day_change", 0.0)
            is_live_connected = True

        # Fetch Historical Data for Candlestick Charts
        token = selected_info["token"]
        to_date = datetime.now()
        from_date = to_date - timedelta(days=5 if timeframe != "day" else 60)
        
        hist_data = k_client.historical_data(token, from_date, to_date, timeframe)
        if hist_data:
            historical_df = pd.DataFrame(hist_data)
    except Exception as e:
        pass

# Fallback Simulation if offline or unauthenticated
if not is_live_connected or spot_price == 0.0:
    fallback_prices = {
        "SENSEX": 83150.00, "NIFTY 50": 24550.00, "BANK NIFTY": 51300.00,
        "RELIANCE": 2980.00, "TCS": 2530.00, "HDFC BANK": 1725.00
    }
    spot_price = fallback_prices.get(selected_script, 1500.00)
    oi_val = 1250400
    oi_change_val = 4.25
    volume_val = 8450000

# Top Metrics Row
col_m1, col_m2, col_m3, col_m4, col_m5 = st.columns(5)
col
