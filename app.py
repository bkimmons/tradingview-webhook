import os
import logging
from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Alpaca client (reads env vars set in Railway) ─────────────────────────────
API_KEY    = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version="v2")


# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Webhook server is running"}), 200


# ── Webhook endpoint ──────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "No JSON payload received"}), 400

        logger.info(f"Received webhook: {data}")

        # ── Parse fields (all have safe defaults) ─────────────────────────────
        ticker          = data.get("ticker", "").upper()
        action          = data.get("action", "").lower()   # "buy" or "sell"
        risk_percentage = float(data.get("risk_percentage", 2))  # default 2%

        if not ticker:
            return jsonify({"error": "Missing 'ticker' field"}), 400
        if action not in ("buy", "sell"):
            return jsonify({"error": f"Invalid action '{action}'. Use 'buy' or 'sell'"}), 400

        # ── Get account equity ────────────────────────────────────────────────
        account = api.get_account()
        equity  = float(account.equity)
        logger.info(f"Account equity: ${equity:,.2f}")

        # ── Calculate dollar amount to trade ──────────────────────────────────
        trade_amount = equity * (risk_percentage / 100)

        # ── Get current price ─────────────────────────────────────────────────
        quote        = api.get_latest_trade(ticker)
        current_price = float(quote.price)
        qty          = max(1, int(trade_amount / current_price))

        logger.info(f"Action: {action} | Ticker: {ticker} | Qty: {qty} | Price: ${current_price}")

        # ── Execute order ─────────────────────────────────────────────────────
        if action == "buy":
            order = api.submit_order(
                symbol     = ticker,
                qty        = qty,
                side       = "buy",
                type       = "market",
                time_in_force = "gtc"
            )

        elif action == "sell":
            # Check if we actually hold a position before selling
            try:
                position = api.get_position(ticker)
                held_qty = int(position.qty)
                sell_qty = min(qty, held_qty)   # never sell more than we own
                logger.info(f"Selling {sell_qty} of {held_qty} held shares")

                order = api.submit_order(
                    symbol        = ticker,
                    qty           = sell_qty,
                    side          = "sell",
                    type          = "market",
                    time_in_force = "gtc"
                )
            except Exception:
                # No position held — nothing to sell
                logger.warning(f"No {ticker} position to sell. Skipping.")
                return jsonify({
                    "status":  "skipped",
                    "message": f"No {ticker} position held. Nothing to sell."
                }), 200

        # ── Return order details ──────────────────────────────────────────────
        result = {
            "status": "success",
            "order": {
                "id":     order.id,
                "symbol": order.symbol,
                "qty":    order.qty,
                "side":   order.side,
                "type":   order.type,
                "status": order.status,
            }
        }
        logger.info(f"Order placed: {result}")
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Webhook error: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
