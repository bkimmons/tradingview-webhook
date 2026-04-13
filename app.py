import os
import requests
import logging
import uuid
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TRADOVATE_USERNAME = os.environ.get("TRADOVATE_USERNAME", "allproductssoldwell@gmail.com")
TRADOVATE_PASSWORD = os.environ.get("TRADOVATE_PASSWORD", "Trymenow60!")
TRADOVATE_ACCOUNT = os.environ.get("TRADOVATE_ACCOUNT", "DEMO7267132")
TRADOVATE_URL = "https://demo.tradovateapi.com/v1"
DEVICE_ID = os.environ.get("DEVICE_ID", str(uuid.uuid4()))

daily_pnl = 0.0
trades_today = 0
kill_switch = False
DAILY_MAX_LOSS = -50
DAILY_PROFIT = 600
MAX_TRADES = 4
access_token = None

def login():
    global access_token
    payload = {
        "name": TRADOVATE_USERNAME,
        "password": TRADOVATE_PASSWORD,
        "appId": "Sample App",
        "appVersion": "1.0",
        "deviceId": DEVICE_ID,
        "cid": 8,
        "sec": "eyJhbGciOiJHS"
    }
    r = requests.post(f"{TRADOVATE_URL}/auth/accesstokenrequest", json=payload)
    logger.info(f"Login response: {r.status_code} {r.text}")
    data = r.json()
    access_token = data.get("accessToken")
    return access_token

def get_account_id():
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(f"{TRADOVATE_URL}/account/list", headers=headers)
    logger.info(f"Account list: {r.status_code} {r.text}")
    accounts = r.json()
    for acc in accounts:
        if str(acc.get("name")) == str(TRADOVATE_ACCOUNT):
            return acc["id"]
    if accounts:
        return accounts[0]["id"]
    return None

def place_order(action):
    global access_token
    if not access_token:
        login()
    account_id = get_account_id()
    if not account_id:
        logger.error("Could not find account ID")
        return {"error": True, "msg": "account not found"}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    order = {
        "accountId": account_id,
        "action": "Buy" if action == "buy" else "Sell",
        "symbol": "MNQM6",
        "orderQty": 1,
        "orderType": "Market",
        "timeInForce": "Day",
        "isAutomated": True
    }
    r = requests.post(f"{TRADOVATE_URL}/order/placeorder", headers=headers, json=order)
    logger.info(f"Order response: {r.status_code} {r.text}")
    if r.status_code == 401:
        login()
        headers["Authorization"] = f"Bearer {access_token}"
        r = requests.post(f"{TRADOVATE_URL}/order/placeorder", headers=headers, json=order)
        logger.info(f"Retry response: {r.status_code} {r.text}")
    return r.json()

@app.route("/webhook", methods=["POST"])
def webhook():
    global trades_today, kill_switch
    data = request.get_json()
    logger.info(f"Signal received: {data}")
    if kill_switch:
        return jsonify({"status": "blocked", "reason": "kill switch"}), 200
    if daily_pnl <= DAILY_MAX_LOSS:
        return jsonify({"status": "blocked", "reason": "daily loss limit"}), 200
    if daily_pnl >= DAILY_PROFIT:
        return jsonify({"status": "blocked", "reason": "profit target hit"}), 200
    if trades_today >= MAX_TRADES:
        return jsonify({"status": "blocked", "reason": "max trades reached"}), 200
    action = data.get("action", "").lower()
    if action not in ["buy", "sell"]:
        return jsonify({"status": "ignored"}), 200
    result = place_order(action)
    trades_today += 1
    return jsonify({"status": "executed", "response": str(result)}), 200

@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status": "running",
        "trades_today": trades_today,
        "daily_pnl": daily_pnl,
        "kill_switch": kill_switch
    })

@app.route("/kill", methods=["POST"])
def kill():
    global kill_switch
    kill_switch = True
    return jsonify({"status": "kill switch activated"})

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "ApexTrade Pro running", "symbol": "MNQM6"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
