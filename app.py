from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
from datetime import datetime
import pytz
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Alpaca config (set these as Railway environment variables) ──────────────
import os
API_KEY    = os.environ.get("ALPACA_API_KEY", "YOUR_KEY_HERE")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY", "YOUR_SECRET_HERE")
BASE_URL   = "https://paper-api.alpaca.markets"

api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")

# ── Trading parameters ──────────────────────────────────────────────────────
ALLOWED_TICKERS   = ["TSLA", "AAPL", "NVDA", "AMZN"]
RISK_PERCENTAGE   = 0.05   # 5% of portfolio per trade
STOP_LOSS_PCT     = 0.02   # 2% stop loss below entry price
TAKE_PROFIT_PCT  = 0.025  # 2.5% take profit above entry
STOP_TRADING_HOUR = 12     # Stop new trades at or after this hour (PDT)
STOP_TRADING_MIN  = 30

# ── Helpers ─────────────────────────────────────────────────────────────────

def is_trading_allowed():
    """Returns True if current PDT time is before the cutoff."""
    pdt = pytz.timezone("America/Los_Angeles")
    now = datetime.now(pdt)
    cutoff = now.replace(hour=STOP_TRADING_HOUR, minute=STOP_TRADING_MIN,
                         second=0, microsecond=0)
    return now < cutoff


def get_buying_power():
    account = api.get_account()
    return float(account.buying_power)


def get_current_price(symbol):
    quote = api.get_latest_trade(symbol)
    return float(quote.price)


def get_position(symbol):
    """Returns (qty, avg_entry_price) or (0, None) if no position."""
    try:
        pos = api.get_position(symbol)
        return int(pos.qty), float(pos.avg_entry_price)
    except Exception:
        return 0, None


def cancel_open_orders_for(symbol):
    """Cancel any open stop/limit orders for the symbol."""
    orders = api.list_orders(status="open")
    cancelled = 0
    for o in orders:
        if o.symbol == symbol:
            api.cancel_order(o.id)
            cancelled += 1
            logger.info(f"Cancelled order {o.id} for {symbol}")
    return cancelled


# ── Core order logic ─────────────────────────────────────────────────────────

def place_buy_with_stops(symbol):
    """
    Buy using 5% of portfolio equity.
    Simultaneously places:
      - stop_loss  : entry * (1 - STOP_LOSS_PCT)   → 2% below
      - take_profit: entry * (1 + TAKE_PROFIT_PCT)  → 4% above
    Uses a bracket order so Alpaca manages both legs automatically.
    """
    current_qty, _ = get_position(symbol)
    if current_qty > 0:
        return {"status": "skipped", "reason": f"Already holding {current_qty} shares of {symbol}"}

    price   = get_current_price(symbol)
    bp      = get_buying_power()
    equity  = float(api.get_account().equity)
    budget  = equity * RISK_PERCENTAGE
    qty     = int(budget / price)

    if qty < 1:
        return {"status": "error", "reason": "Insufficient buying power for 1 share"}

    stop_price   = round(price * (1 - STOP_LOSS_PCT), 2)
    limit_price  = round(price * (1 + TAKE_PROFIT_PCT), 2)

    logger.info(f"BUY {qty} {symbol} @ ~{price:.2f} | stop={stop_price} | tp={limit_price}")

    order = api.submit_order(
        symbol        = symbol,
        qty           = qty,
        side          = "buy",
        type          = "market",
        time_in_force = "day",
        order_class   = "bracket",
        stop_loss     = {"stop_price": stop_price},
        take_profit   = {"limit_price": limit_price},
    )

    return {
        "status"      : "filled",
        "action"      : "BUY",
        "symbol"      : symbol,
        "qty"         : qty,
        "approx_price": price,
        "stop_loss"   : stop_price,
        "take_profit" : limit_price,
        "order_id"    : order.id,
    }


def place_sell(symbol):
    """
    Sell the full position in the symbol.
    Cancels any open bracket legs first (stop/take-profit orders).
    """
    qty, entry = get_position(symbol)
    if qty <= 0:
        return {"status": "skipped", "reason": f"No position in {symbol} to sell"}

    # Cancel open bracket legs so they don't fire after manual sell
    cancelled = cancel_open_orders_for(symbol)
    logger.info(f"Cancelled {cancelled} open orders before selling {symbol}")

    order = api.submit_order(
        symbol        = symbol,
        qty           = qty,
        side          = "sell",
        type          = "market",
        time_in_force = "day",
    )

    pnl_est = None
    if entry:
        price   = get_current_price(symbol)
        pnl_est = round((price - entry) * qty, 2)

    return {
        "status"    : "submitted",
        "action"    : "SELL",
        "symbol"    : symbol,
        "qty"       : qty,
        "entry_price": entry,
        "est_pnl"   : pnl_est,
        "order_id"  : order.id,
    }


# ── Flask routes ─────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    logger.info(f"Webhook received: {data}")

    action = str(data.get("action", "")).upper()
    symbol = str(data.get("ticker",  "")).upper()

    if symbol not in ALLOWED_TICKERS:
        return jsonify({"status": "error", "reason": f"{symbol} not in allowed list"}), 400

    if action == "BUY":
        if not is_trading_allowed():
            return jsonify({"status": "skipped", "reason": "Past trading cutoff time"})
        result = place_buy_with_stops(symbol)

    elif action == "SELL":
        result = place_sell(symbol)   # SELL allowed any time (close positions)

    else:
        return jsonify({"status": "error", "reason": f"Unknown action: {action}"}), 400

    logger.info(f"Result: {result}")
    return jsonify(result)


@app.route("/", methods=["GET"])
def status():
    pdt = pytz.timezone("America/Los_Angeles")
    now = datetime.now(pdt)

    positions = []
    try:
        for p in api.list_positions():
            positions.append({
                "symbol"    : p.symbol,
                "qty"       : p.qty,
                "entry"     : p.avg_entry_price,
                "current"   : p.current_price,
                "unrealized": p.unrealized_pl,
            })
    except Exception as e:
        positions = [{"error": str(e)}]

    return jsonify({
        "message"         : "Multi-stock day trading server is running",
        "status"          : "ok",
        "allowed_tickers" : ALLOWED_TICKERS,
        "risk_percentage" : RISK_PERCENTAGE,
        "stop_loss_pct"   : f"{STOP_LOSS_PCT*100:.0f}%",
        "take_profit_pct" : f"{TAKE_PROFIT_PCT*100:.0f}%",
        "stop_trading_at" : f"{STOP_TRADING_HOUR}:{STOP_TRADING_MIN:02d} PM PDT",
        "trading_allowed" : is_trading_allowed(),
        "current_time_pdt": now.strftime("%I:%M %p PDT"),
        "open_positions"  : positions,
    })


@app.route("/positions", methods=["GET"])
def positions():
    try:
        pos_list = []
        for p in api.list_positions():
            pos_list.append({
                "symbol"         : p.symbol,
                "qty"            : p.qty,
                "avg_entry"      : p.avg_entry_price,
                "current_price"  : p.current_price,
                "unrealized_pl"  : p.unrealized_pl,
                "unrealized_pct" : p.unrealized_plpc,
            })
        return jsonify({"positions": pos_list, "count": len(pos_list)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/close-all", methods=["POST"])
def close_all():
    """Emergency: close all positions and cancel all orders."""
    try:
        api.cancel_all_orders()
        api.close_all_positions()
        return jsonify({"status": "ok", "message": "All positions closed, all orders cancelled"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
