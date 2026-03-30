import os
import logging
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


# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Day trading webhook server is running"}), 200


# ── Webhook endpoint ──────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if not data:
            return jsonify({"error": "No JSON payload received"}), 400

        logger.info(f"Received webhook: {data}")

        ticker          = data.get("ticker", "TSLA").upper()
        action          = data.get("action", "").lower()
        risk_percentage = float(data.get("risk_percentage", 95))  # 95% of equity for max trades

        if action not in ("buy", "sell"):
            return jsonify({"error": f"Invalid action: {action}"}), 400

        # ── Get account info ──────────────────────────────────────────────────
        account      = api.get_account()
        equity       = float(account.equity)
        buying_power = float(account.buying_power)
        logger.info(f"Equity: ${equity:,.2f} | Buying Power: ${buying_power:,.2f}")

        # ── Get current price ─────────────────────────────────────────────────
        quote         = api.get_latest_trade(ticker)
        current_price = float(quote.price)
        logger.info(f"Current {ticker} price: ${current_price}")

        # ── Calculate max shares ──────────────────────────────────────────────
        trade_amount = equity * (risk_percentage / 100)
        qty          = max(1, int(trade_amount / current_price))

        logger.info(f"Action: {action} | Qty: {qty} | Price: ${current_price} | Trade value: ${qty * current_price:,.2f}")

        if action == "buy":
            # Cancel any open orders first
            try:
                api.cancel_all_orders()
                logger.info("Cancelled all open orders before buying")
            except Exception:
                pass

            order = api.submit_order(
                symbol        = ticker,
                qty           = qty,
                side          = "buy",
                type          = "market",
                time_in_force = "day"
            )
            logger.info(f"BUY order placed: {qty} shares of {ticker} at ~${current_price}")

        elif action == "sell":
            try:
                position      = api.get_position(ticker)
                held_qty      = int(position.qty)
                avg_cost      = float(position.avg_entry_price)
                unrealized_pl = float(position.unrealized_pl)

                logger.info(f"Position: {held_qty} shares | Avg cost: ${avg_cost} | Unrealized P&L: ${unrealized_pl:,.2f}")

                # Sell entire position for maximum profit
                order = api.submit_order(
                    symbol        = ticker,
                    qty           = held_qty,
                    side          = "sell",
                    type          = "market",
                    time_in_force = "day"
                )
                logger.info(f"SELL entire position: {held_qty} shares | Expected P&L: ${unrealized_pl:,.2f}")

            except Exception:
                logger.warning(f"No {ticker} position to sell.")
                return jsonify({
                    "status":  "skipped",
                    "message": f"No {ticker} position held. Nothing to sell."
                }), 200

        result = {
            "status": "success",
            "order": {
                "id":            order.id,
                "symbol":        order.symbol,
                "qty":           order.qty,
                "side":          order.side,
                "type":          order.type,
                "status":        order.status,
                "current_price": current_price,
                "trade_value":   round(qty * current_price, 2),
            }
        }
        logger.info(f"Order result: {result}")
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Webhook error: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
