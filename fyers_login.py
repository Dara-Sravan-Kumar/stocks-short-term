"""One-time (and roughly fortnightly) Fyers auth bootstrap.

Fyers access tokens expire daily; refresh tokens last ~15 days. The bot
auto-refreshes the daily access token on every run (see
mcxbot/data_providers/fyers.py), so you only need to run THIS script when the
refresh token itself dies — i.e. after first setup and then every ~15 days
(the bot's warnings will tell you when).

Prerequisites (once, at https://myapi.fyers.in):
1. Create an app -> note the App ID (like "AB01234-100") and Secret ID.
2. Set the app's redirect URL to
   https://trade.fyers.in/api-login/redirect-uri/index.html
   (or your own; then also set FYERS_REDIRECT_URI in .env).
3. Put FYERS_APP_ID, FYERS_SECRET_ID and FYERS_PIN in .env.

Usage:
    .venv\\Scripts\\python.exe fyers_login.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
from dotenv import load_dotenv

import config
from stockbot.fyers_data import app_id_hash, save_token_cache


def main() -> int:
    load_dotenv(config.PROJECT_ROOT / ".env")
    creds = config.fyers_settings()
    if not creds["app_id"] or not creds["secret_id"]:
        print("FYERS_APP_ID / FYERS_SECRET_ID missing in .env - create an app "
              "at https://myapi.fyers.in and fill them in first.")
        return 1
    if not creds["pin"]:
        print("WARNING: FYERS_PIN is empty in .env - without it the bot cannot "
              "auto-refresh the daily access token.")

    auth_url = (f"{config.FYERS_API_BASE}/generate-authcode"
                f"?client_id={quote_plus(creds['app_id'])}"
                f"&redirect_uri={quote_plus(creds['redirect_uri'])}"
                f"&response_type=code&state=stockbot")
    print("\n1. Open this URL in a browser and log in to Fyers:\n")
    print(f"   {auth_url}\n")
    print("2. After login you land on the redirect page; copy the auth code")
    print("   (or just paste the ENTIRE redirect URL here).\n")
    raw = input("Auth code or redirect URL: ").strip()

    auth_code = raw
    if raw.lower().startswith("http"):
        qs = parse_qs(urlparse(raw).query)
        auth_code = (qs.get("auth_code") or qs.get("code") or [""])[0]
    if not auth_code:
        print("Could not extract an auth code from that input.")
        return 1

    resp = requests.post(
        f"{config.FYERS_API_BASE}/validate-authcode",
        json={"grant_type": "authorization_code",
              "appIdHash": app_id_hash(creds["app_id"], creds["secret_id"]),
              "code": auth_code},
        timeout=config.FYERS_TIMEOUT)
    data = resp.json()
    if resp.status_code >= 400 or data.get("s") != "ok" or not data.get("access_token"):
        print(f"Token exchange failed: {data.get('message', resp.text[:200])}")
        return 1

    save_token_cache({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "issued": datetime.now().strftime("%Y-%m-%d"),
    })
    print("\nSuccess - tokens saved to data/fyers_token.json.")
    print("The bot will now use Fyers for real MCX candles and auto-refresh")
    print("the access token daily. Re-run this script when the refresh token")
    print("expires (~15 days; the bot's run warnings will say so).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
