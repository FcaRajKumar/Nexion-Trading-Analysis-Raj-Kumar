import streamlit as st
from kiteconnect import KiteConnect
import pandas as pd
import math

# Page Configuration
st.set_page_config(
    page_title="Nexion Trading Analysis & F&O Advisor",
    page_icon="📈",
    layout="wide"
)

st.title("📈 Nexion Trading Analysis & Universal F&O/Equity Advisor")
st.markdown("### Production-Grade Zerodha Integration, Live LTP & Real-Market Option Pricing Engine")

# ---------------------------------------------------------
# 1. SIDEBAR: ZERODHA KITE AUTHENTICATION
#    Credentials are read from Streamlit server-side Secrets.
#    This avoids losing typed credentials when Zerodha redirects
#    the browser back to the app.
# ---------------------------------------------------------
st.sidebar.header("🔐 Zerodha Kite Gateway")

# Required Streamlit Cloud Secrets format:
# [kite]
# api_key = "YOUR_KITE_API_KEY"
# api_secret = "YOUR_KITE_API_SECRET"
try:
    _kite_secrets = st.secrets["kite"]
    api_key = str(_kite_secrets["api_key"]).strip()
    api_secret = str(_kite_secrets["api_secret"]).strip()
    if not api_key or not api_secret:
        raise ValueError("The Kite API key or API secret is blank.")
    credentials_available = True
except Exception:
    api_key = ""
    api_secret = ""
    credentials_available = False
    st.sidebar.error(
        "Kite API credentials are not configured. Open Streamlit Cloud → "
        "Manage app → Settings → Secrets and add the [kite] configuration."
    )
    st.sidebar.code(
        '[kite]\napi_key = "YOUR_KITE_API_KEY"\napi_secret = "YOUR_KITE_API_SECRET"',
        language="toml",
    )
    st.sidebar.caption("Do not put API credentials in your Python file or GitHub repository.")

# Session state is per Streamlit browser session. Zerodha request tokens are
# short-lived and single-use, so exchange them immediately on callback.
if "access_token" not in st.session_state:
    st.session_state.access_token = None
if "access_token_api_key" not in st.session_state:
    st.session_state.access_token_api_key = None
if "kite_profile" not in st.session_state:
    st.session_state.kite_profile = None
if "kite_profile_token_marker" not in st.session_state:
    st.session_state.kite_profile_token_marker = None
if "kite_auth_error" not in st.session_state:
    st.session_state.kite_auth_error = None

if credentials_available:
    kite = KiteConnect(api_key=api_key)
    callback_request_token = st.query_params.get("request_token")
    callback_status = str(st.query_params.get("status", "")).lower()
    callback_error = st.query_params.get("error")

    # If the browser returns an OAuth error instead of a request token, report
    # it and remove the callback parameters so that the app does not loop.
    if callback_error:
        st.session_state.kite_auth_error = (
            f"Zerodha login returned an error: {callback_error}. Start a fresh login."
        )
        st.query_params.clear()

    elif callback_request_token:
        if callback_status and callback_status != "success":
            st.session_state.kite_auth_error = (
                f"Zerodha login status was '{callback_status}'. Start a fresh login."
            )
            st.query_params.clear()
        elif not st.session_state.access_token:
            try:
                session_data = kite.generate_session(
                    str(callback_request_token),
                    api_secret=api_secret,
                )
                st.session_state.access_token = session_data["access_token"]
                st.session_state.access_token_api_key = api_key
                st.session_state.kite_profile = {
                    "user_name": session_data.get("user_name", ""),
                    "user_shortname": session_data.get("user_shortname", ""),
                    "user_id": session_data.get("user_id", ""),
                }
                st.session_state.kite_profile_token_marker = session_data["access_token"]
                st.session_state.kite_auth_error = None
