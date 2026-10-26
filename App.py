import streamlit as st
from kiteconnect import KiteConnect
import pandas as pd
from datetime import datetime, timedelta
import math
import pyotp
import requests
import urllib.parse as up

# Page Configuration
st.set_page_config(
    page_title="Nexion Professional Trading Terminal",
    page_icon="📈",
    layout="wide"
)

st.title("📈 Nexion Live Trading & F&O Intelligence Terminal")
st.markdown("### Fully Automated TOTP Background Login | Mobile Optimized")

# ---------------------------------------------------------
# 1. AUTOMATED ZERODHA LOGIN GATEWAY (USING TOTP)
# ---------------------------------------------------------
st.sidebar.header("🔐 Automated Zerodha Gateway")
st.sidebar.info("💡 Enter your credentials and 16-digit TOTP Secret once. The app will generate the session automatically in the background.")

api_key = st.sidebar.text_input("API Key", type="default", key="input_api_key")
api_secret = st.sidebar.text_input("API Secret", type="password", key="input_api_secret")
user_id = st.sidebar.text_input("Zerodha User ID", type="default", key="input_user_id")
password = st.sidebar.text_input("Password", type="password", key="input_password")
totp_secret = st.sidebar.text_input("TOTP Secret Key (16-digit)", type="password", key="input_totp_secret")

if "access_token" not in st.session_state:
    st.session_state.access_token = None

if st.sidebar.button("🚀 Auto-Login & Connect") and api_key and api_secret and user_id and password and totp_secret:
    try:
        with st.spinner("Authenticating with Zerodha..."):
            # Create a session to handle cookies and redirects
            session = requests.Session()
            
            # Step 1: Post Login credentials
            login_url = "https://kite.zerodha.com/api/login"
            r1 = session.post(login_url, data={
                "user_id": user_id.strip(),
                "password": password.strip()
            })
            res1 = r1.json()
            
            if res1.get("status") != "success":
                raise Exception(res1.get("message", "Invalid User ID or Password"))
                
            request_id = res1["data"]["request_id"]
            
            # Step 2: Generate current 6-digit OTP using pyotp & TOTP secret
            clean_totp_secret = totp_secret.strip().replace(" ", "")
            totp = pyotp.TOTP(clean_totp_secret)
            current_otp = totp.now()
            
            # Step 3: Post TwoFA (TOTP)
            twofa_url = "https://kite.zerodha.com/api/twofa"
            r2 = session.post(twofa_url, data={
                "user_id": user_id.strip(),
                "request_id": request_id,
                "totp": current_otp
            })
            res2 = r2.json()
            
            if res2.get("status") != "success":
                raise Exception(res2.get("message", "TOTP verification failed. Check your 16-digit secret."))
                
            # Step 4: Hit connect URL to capture request_token via redirect
            auth_url = f"https://kite.trade/connect/login?api_key={api_key.strip()}&v=3"
            r3 = session.get(auth_url, allow_redirects=False)
            
            redirect_url = r3.headers.get("Location", "")
            if "request_token=" not in redirect_url:
                raise Exception("Failed to capture request token from redirect header.")
                
            parsed_url = up.urlparse(redirect_url)
            query_params = up.parse_qs(parsed_url.query)
            request_token = query_params.get("request_token", [None])[0]
            
            if not request_token:
                raise Exception("Request token missing.")
                
            # Step 5: Generate final access token
            kite = KiteConnect(api_key=api_key.strip())
            data = kite.generate_session(request_token, api_secret=api_secret.strip())
            st.session_state.access_token = data["access_token"]
            
            st.sidebar.success("🟢 Auto-Login Successful!")
            st.rerun()
            
    except Exception as e:
        st.sidebar.error(f"❌ Login Error: {str(e)}")

if st.session_state.access_token:
    st.sidebar.success("🟢 Active Session Connected")
    if st.sidebar.button("Logout / Reset Session"):
        st.session_state.access_token = None
        st.rerun()

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
        k_client = KiteConnect(api_key=api_key.strip())
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
# 3. LIVE SYSTEM DATA FETCHING
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
        k_client = KiteConnect(api_key=api_key.strip())
        k_client.set_access_token(st.session_state.access_token)
        
        quote_res = k_client.quote([symbol_key])
        if symbol_key in quote_res:
            q_data = quote_res[symbol_key]
            spot_price = q_data["last_price"]
            volume_val = q_data.get("volume", 0)
            oi_val = q_data.get("oi", 0)
            oi_change_val = q_data.get("oi_day_change", 0.0)
            is_live_connected = True

        token = selected_info["token"]
        to_date = datetime.now()
        from_date = to_date - timedelta(days=5 if timeframe != "day" else 60)
        
        hist_data = k_client.historical_data(token, from_date, to_date, timeframe)
        if hist_data:
            historical_df = pd.DataFrame(hist_data)
    except Exception as e:
        pass

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
col_m1.metric(f"{selected_script} LTP", f"₹ {spot_price:,.2f}", "Live Tick" if is_live_connected else "Simulation")
col_m2.metric("Open Interest (OI)", f"{oi_val:,}" if oi_val else "N/A")
col_m3.metric("OI Change (%)", f"{oi_change_val:+.2f}%")
col_m4.metric("Traded Volume", f"{volume_val:,}")
col_m5.metric("Feed Status", "🟢 LIVE KITE" if is_live_connected else "🟡 OFFLINE / MOCK")

st.markdown("---")

# ---------------------------------------------------------
# 4. PRICE ACTION CHART
# ---------------------------------------------------------
st.markdown(f"### 📊 Price Action Trend for `{selected_script}`")

if not historical_df.empty and 'close' in historical_df.columns:
    chart_data = historical_df.set_index('date')[['close']]
    st.line_chart(chart_data)
else:
    st.info("💡 Run Auto-Login in the sidebar above to load live historical price action data.")

st.markdown("---")

# ---------------------------------------------------------
# 5. EQUITY INVESTMENT TARGETS & STOP LOSS
# ---------------------------------------------------------
st.markdown(f"### 💼 Equity Decision Matrix & Targets for `{selected_script}`")

eq_buy_low = spot_price * 0.992
eq_buy_high = spot_price
eq_target = spot_price * 1.045
eq_sl = spot_price * 0.975

col_eq1, col_eq2, col_eq3 = st.columns(3)
col_eq1.metric("Recommended Buy Zone", f"₹ {eq_buy_low:,.2f} - ₹ {eq_buy_high:,.2f}")
col_eq2.metric("Target Sell Price", f"₹ {eq_target:,.2f} (+4.5%)")
col_eq3.metric("Capital Protection Stop Loss", f"₹ {eq_sl:,.2f} (-2.5%)")

# ---------------------------------------------------------
# 6. DYNAMIC F&O STRATEGY & OPTION PRICING ENGINE
# ---------------------------------------------------------
st.markdown(f"### ⚡ F&O Derivative Recommendations & Option Pricing for `{selected_script}`")

strike_interval = 100 if spot_price > 10000 else (50 if spot_price > 2000 else 10)
atm_strike = round(spot_price / strike_interval) * strike_interval

def calculate_option_premium(spot, strike, option_type, iv=0.22, days_to_expiry=4):
    t = max(days_to_expiry / 365.0, 0.001)
    if option_type == 'CE':
        intrinsic = max(0.0, spot - strike)
        time_value = spot * (iv * 0.35) * math.sqrt(t) * max(0.4, 1.0 - abs(spot - strike) / spot)
        premium = intrinsic + max(time_value, spot * 0.008)
    else:
        intrinsic = max(0.0, strike - spot)
        time_value = spot * (iv * 0.35) * math.sqrt(t) * max(0.4, 1.0 - abs(spot - strike) / spot)
        premium = intrinsic + max(time_value, spot * 0.008)
    return round(max(premium, 2.00), 2)

ce_entry = calculate_option_premium(spot_price, atm_strike, 'CE')
ce_t1 = round(ce_entry * 1.35, 2)
ce_t2 = round(ce_entry * 1.80, 2)
ce_sl = round(ce_entry * 0.65, 2)

pe_entry = calculate_option_premium(spot_price, atm_strike, 'PE')
pe_t1 = round(pe_entry * 1.30, 2)
pe_t2 = round(pe_entry * 1.70, 2)
pe_sl = round(pe_entry * 0.70, 2)

col_call, col_put = st.columns(2)

with col_call:
    st.markdown("#### 🟢 Call Option (CE) Setup - Bullish Momentum")
    st.success(
        f"* **Instrument Contract:** `{selected_script} {atm_strike} CE`\n"
        f"* **Action:** **BUY / LONG**\n"
        f"* **Dynamic Entry Premium:** `₹ {ce_entry:,.2f}`\n"
        f"* **Target 1:** `₹ {ce_t1:,.2f}` (+35%)\n"
        f"* **Target 2:** `₹ {ce_t2:,.2f}` (+80%)\n"
        f"* **Stop Loss:** `₹ {ce_sl:,.2f}`"
    )

with col_put:
    st.markdown("#### 🔴 Put Option (PE) Setup - Bearish Hedge")
    st.error(
        f"* **Instrument Contract:** `{selected_script} {atm_strike} PE`\n"
        f"* **Action:** **SELL / SHORT ON RALLY**\n"
        f"* **Dynamic Entry Premium:** `₹ {pe_entry:,.2f}`\n"
        f"* **Target 1:** `₹ {pe_t1:,.2f}`\n"
        f"* **Target 2:** `₹ {pe_t2:,.2f}`\n"
        f"* **Stop Loss:** `₹ {pe_sl:,.2f}`"
    )

# ---------------------------------------------------------
# 7. DECISION LOGIC & RATIONALE
# ---------------------------------------------------------
st.markdown("### 🧠 Decision-Making Rationale & Analysis")
st.info(
    f"* **System Feed:** {'Active connection streaming live LTP and OI metrics.' if is_live_connected else 'Operating in simulation mode; run Auto-Login in the sidebar.'}\n"
    f"* **Derivative Calibration:** ATM strikes are pinned at `{atm_strike}` for `{selected_script}`. Option premiums adjust dynamically according to the live underlying equity/index spot price.\n"
    f"* **Risk Management:** Adhere strictly to the designated stop-loss thresholds to protect capital against sudden volatility swings."
)

st.markdown("---")
st.caption("Nexion Consulting • Professional Algorithmic F&O & Equity Intelligence | Powered by Zerodha Kite Connect")
