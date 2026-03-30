import os
import logging
from datetime import datetime, time
import pytz
from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Alpaca client ─────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")

# ── Config ────────────────────────────────────────────────────────────────────
RISK_PERCENTAGE  = 5
ALLOWED_TICKERS  = {"TSLA", "AAPL", "NVDA", "AMZN"}
STOP_TRADING_AT  = time(12, 30)
MARKET_TZ        = pytz.timezone("America/Los_Angeles")


# ── Helpers ───────────────────────────────────────────────────────────────────
def is_trading_allowed():
    now_pdt = datetime.now(MARKET_TZ).time()
    return now_pdt < STOP_TRADING_AT


def get_current_price(ticker):
    quote = api.get_latest_trade(ticker)
    return float(quote.price)


def get_position(ticker):
    try:
        return api.get_position(ticker)
    except Exception:
        return None


def get_best_momentum_ticker():
    best_ticker = None
    best_move   = 0
    for ticker in ALLOWED_TICKERS:
        try:
            bars = api.get_bars(ticker, "1Day", limit=2).df
            if len(bars) < 2:
                continue
            prev_close = float(bars.iloc[-2]["close"])
            current    = get_current_price(ticker)
            pct_move   = abs((current - prev_close) / prev_close * 100)
            logger.info(f"{ticker}: {pct_move:.2f}% move today")
            if pct_move > best_move:
                best_move   = pct_move
                best_ticker = ticker
        except Exception as e:
            logger.warning(f"Could not scan {ticker}: {e}")
    return best_ticker


# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    now_pdt = datetime.now(MARKET_TZ).strftime("%I:%M %p PDT")
    return jsonify({
        "status":           "ok",
        "message":          "Multi-stock day trading server is running",
        "current_time_pdt": now_pdt,
        "trading_allowed":  is_trading_allowed(),
        "allowed_tickers":  list(ALLOWED_TICKERS),
        "risk_percentage":  RISK_PERCENTAGE,
        "stop_trading_at":  "12:30 PM PDT"
    }), 200


# ── Webhook endpoint ──────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "No JSON payload received"}), 400

        logger.info(f"Received webhook: {data}")

        ticker = data.get("ticker", "").upper()
        action = data.get("action", "").lower()
        risk   = float(data.get("risk_percentage", RISK_PERCENTAGE))

        if ticker not in ALLOWED_TICKERS:
            return jsonify({"error": f"Ticker {ticker} not allowed"}), 400

        if action not in ("buy", "sell"):
            return jsonify({"error": f"Invalid action: {action}"}), 400

        account = api.get_account()
        equity  = float(account.equity)

        if action == "buy":
            if not is_trading_allowed():
                return jsonify({"status": "skipped", "message": "Past 12:30 PM PDT cutoff"}), 200

            # Check existing positions
            current_positions = [t for t in ALLOWED_TICKERS if get_position(t)]

            if current_positions:
                if ticker not in current_positions:
                    for held in current_positions:
                        pos = get_position(held)
                        if pos:
                            api.submit_order(symbol=held, qty=int(pos.qty), side="sell", type="market", time_in_force="day")
                            logger.info(f"Switched: sold {held}, buying {ticker}")
                else:
                    return jsonify({"status": "skipped", "message": f"Already holding {ticker}"}), 200

            # Pick best momentum stock
            best = get_best_momentum_ticker()
            if best and best != ticker:
                logger.info(f"Momentum: trading {best} instead of {ticker}")
                ticker = best

            current_price = get_current_price(ticker)
            qty = max(1, int((equity * risk / 100) / current_price))

            try:
                api.cancel_all_orders()
            except Exception:
                pass

            order = api.submit_order(symbol=ticker, qty=qty, side="buy", type="market", time_in_force="day")
            logger.info(f"BUY {qty} {ticker} @ ${current_price}")

        elif action == "sell":
            position = get_position(ticker)
            if not position:
                sold_any = False
                for t in ALLOWED_TICKERS:
                    pos = get_position(t)
                    if pos:
                        api.submit_order(symbol=t, qty=int(pos.qty), side="sell", type="market", time_in_force="day")
                        sold_any = True
                if not sold_any:
                    return jsonify({"status": "skipped", "message": "No positions to sell"}), 200
                return jsonify({"status": "success", "message": "Sold all positions"}), 200

            held_qty      = int(position.qty)
            current_price = get_current_price(ticker)
            unrealized_pl = (current_price - float(position.avg_entry_price)) * held_qty

            order = api.submit_order(symbol=ticker, qty=held_qty, side="sell", type="market", time_in_force="day")
            logger.info(f"SELL {held_qty} {ticker} | P&L: ${unrealized_pl:,.2f}")

        result = {
            "status": "success",
            "order": {
                "id": order.id, "symbol": order.symbol,
                "qty": order.qty, "side": order.side,
                "type": order.type, "status": order.status,
            }
        }
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Webhook error: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
