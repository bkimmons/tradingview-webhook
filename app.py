ApexTrade Super Bot v2.0
Enhanced with daily market intelligence — searches the web every morning
to determine if it's a good day to trade and finds best entry/exit points.
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import os, secrets, logging
from datetime import datetime
import pytz, requests
from bs4 import BeautifulSoup
import alpaca_trade_api as tradeapi
from market_intelligence import get_market_report

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Alpaca ────────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("ALPACA_API_KEY",    "YOUR_KEY")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY", "YOUR_SECRET")
BASE_URL   = os.environ.get("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")

# ── Settings ──────────────────────────────────────────────────────────────────
ALLOWED_TICKERS    = ["TSLA", "AAPL", "NVDA", "AMZN"]
RISK_PERCENTAGE    = float(os.environ.get("RISK_PCT",        "0.005"))
STOP_LOSS_PCT      = float(os.environ.get("STOP_LOSS_PCT",   "0.02"))
TAKE_PROFIT_PCT    = float(os.environ.get("TAKE_PROFIT_PCT", "0.025"))
STOP_TRADING_HOUR  = int(os.environ.get("STOP_HOUR", "12"))
STOP_TRADING_MIN   = int(os.environ.get("STOP_MIN",  "30"))
MAX_DAILY_TRADES   = int(os.environ.get("MAX_TRADES", "2"))
MIN_CONFIDENCE     = int(os.environ.get("MIN_CONFIDENCE", "40"))  # Skip if market confidence < this

trade_log = {}


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def pdt_now():
    return datetime.now(pytz.timezone("America/Los_Angeles"))

def is_trading_allowed():
    now    = pdt_now()
    cutoff = now.replace(hour=STOP_TRADING_HOUR, minute=STOP_TRADING_MIN, second=0, microsecond=0)
    return now < cutoff

def is_in_best_window(best_window: str) -> bool:
    """Check if current time is within the day's best entry window."""
    try:
        now  = pdt_now()
        part = best_window.split("(")[0].strip()   # e.g. "09:45-11:00 AM PDT"
        times = part.replace(" AM PDT","").replace(" PDT","").split("-")
        def to_minutes(t):
            h, m = map(int, t.strip().split(":"))
            return h * 60 + m
        start = to_minutes(times[0])
        end   = to_minutes(times[1])
        cur   = now.hour * 60 + now.minute
        return start <= cur <= end
    except Exception:
        return True  # default allow if parse fails

def get_rsi(bars, period=14):
    try:
        d = bars["close"].diff()
        g = d.clip(lower=0).rolling(period).mean()
        l = (-d.clip(upper=0)).rolling(period).mean()
        return float((100-(100/(1+g/l))).iloc[-1])
    except: return None

def get_vwap(bars):
    try:
        t = (bars["high"]+bars["low"]+bars["close"])/3
        return float((t*bars["volume"]).sum()/bars["volume"].sum())
    except: return None

def get_support_resistance(bars):
    """Calculate basic support and resistance levels from recent bars."""
    try:
        closes = bars["close"].tail(20)
        highs  = bars["high"].tail(20)
        lows   = bars["low"].tail(20)
        resistance = float(highs.max())
        support    = float(lows.min())
        pivot      = float((highs.max() + lows.min() + closes.iloc[-1]) / 3)
        return support, resistance, pivot
    except: return None, None, None

def get_ema(bars, period):
    try:
        return float(bars["close"].ewm(span=period, adjust=False).mean().iloc[-1])
    except: return None

def too_many(sym):
    today = pdt_now().date()
    return len([t for t in trade_log.get(sym,[]) if t.date()==today]) >= MAX_DAILY_TRADES

def too_soon(sym, gap=15):
    times = trade_log.get(sym,[])
    if not times: return False
    return (pdt_now()-times[-1]).total_seconds()/60 < gap

def log_t(sym):
    trade_log.setdefault(sym,[]).append(pdt_now())


# ═══════════════════════════════════════════════════════════════════════════════
# SMART ENTRY/EXIT CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_entry_exit(symbol, report):
    """
    Uses technical analysis + market conditions to calculate
    optimal entry price, stop loss, and take profit levels.
    """
    try:
        bars  = api.get_bars(symbol, "5Min", limit=100).df
        price = float(api.get_latest_trade(symbol).price)
    except Exception as e:
        log.warning(f"Could not fetch data for {symbol}: {e}")
        return None

    rsi        = get_rsi(bars)
    vwap_price = get_vwap(bars)
    ema9       = get_ema(bars, 9)
    ema21      = get_ema(bars, 21)
    support, resistance, pivot = get_support_resistance(bars)

    # Dynamic stop/take-profit based on market conditions
    bias = report.get("market_bias", "neutral")
    conf = report.get("confidence", 50)

    # Tighter stops on bearish days, wider on bullish
    if bias == "bullish" and conf > 65:
        sl_pct = STOP_LOSS_PCT          # 2%
        tp_pct = TAKE_PROFIT_PCT * 1.5  # 3.75% — let winners run on good days
    elif bias == "bearish":
        sl_pct = STOP_LOSS_PCT * 0.75   # 1.5% — tighter stop on bad days
        tp_pct = TAKE_PROFIT_PCT * 0.8  # 2% — take profit faster on bad days
    else:
        sl_pct = STOP_LOSS_PCT          # 2%
        tp_pct = TAKE_PROFIT_PCT        # 2.5%

    # Adjust stop to use support level if it's better than % stop
    if support and support > price * (1 - sl_pct * 1.5):
        stop_price = round(support * 0.995, 2)  # just below support
    else:
        stop_price = round(price * (1 - sl_pct), 2)

    # Adjust take-profit to use resistance if it's reasonable
    if resistance and resistance < price * (1 + tp_pct * 2):
        limit_price = round(resistance * 0.998, 2)  # just below resistance
    else:
        limit_price = round(price * (1 + tp_pct), 2)

    return {
        "price":        price,
        "stop_price":   stop_price,
        "limit_price":  limit_price,
        "rsi":          rsi,
        "vwap":         vwap_price,
        "ema9":         ema9,
        "ema21":        ema21,
        "support":      support,
        "resistance":   resistance,
        "pivot":        pivot,
        "sl_pct":       round(sl_pct * 100, 1),
        "tp_pct":       round(tp_pct * 100, 1),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MASTER FILTER CHAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run_all_filters(symbol, action):
    """
    Complete filter chain for BUY signals.
    Returns (allowed: bool, reason: str, levels: dict|None)
    """
    if action != "BUY":
        return True, "sell always allowed", None

    # ── 1. Daily market intelligence ─────────────────────────────────────────
    report = get_market_report()

    if not report["trade_today"]:
        return False, f"🚫 Market intelligence: {report['skip_reason']}", None

    if report["confidence"] < MIN_CONFIDENCE:
        return False, f"🚫 Market confidence too low ({report['confidence']}/100)", None

    if symbol in report.get("avoid_tickers", []):
        return False, f"🚫 {symbol} flagged to avoid today: {', '.join(report['reasons'][:2])}", None

    # ── 2. Time checks ────────────────────────────────────────────────────────
    if not is_trading_allowed():
        return False, f"⏰ Past trading cutoff ({STOP_TRADING_HOUR}:{STOP_TRADING_MIN:02d} PDT)", None

    best_window = report.get("best_entry_window", "")
    if not is_in_best_window(best_window):
        return False, f"⏰ Outside best entry window ({best_window})", None

    # ── 3. Trade frequency ────────────────────────────────────────────────────
    if too_many(symbol):
        return False, f"📊 Max {MAX_DAILY_TRADES} trades/day reached for {symbol}", None

    if too_soon(symbol):
        return False, f"⏳ Signal within 15min cooldown for {symbol}", None

    # ── 4. Technical analysis ─────────────────────────────────────────────────
    levels = calculate_entry_exit(symbol, report)
    if not levels:
        return False, "❌ Could not fetch technical data", None

    rsi        = levels.get("rsi")
    vwap_price = levels.get("vwap")
    price      = levels.get("price")
    ema9       = levels.get("ema9")
    ema21      = levels.get("ema21")

    # RSI filter
    if rsi and rsi > 72:
        return False, f"📈 RSI={rsi:.1f} overbought — skip", levels

    if rsi and rsi < 35:
        log.info(f"{symbol} RSI={rsi:.1f} oversold — allowing contrarian buy")

    # VWAP filter — price must be above VWAP
    if vwap_price and price:
        diff = (price - vwap_price) / vwap_price
        if diff < -0.003:
            return False, f"📉 {symbol} ${price:.2f} below VWAP ${vwap_price:.2f} — no uptrend", levels

    # EMA trend confirmation — EMA9 must be above EMA21
    if ema9 and ema21:
        if ema9 < ema21:
            return False, f"📉 EMA9 ({ema9:.2f}) below EMA21 ({ema21:.2f}) — downtrend, skip", levels

    # ── 5. Market sentiment from news ─────────────────────────────────────────
    if report.get("market_bias") == "bearish" and report.get("confidence", 50) < 40:
        return False, f"📰 Market too bearish for new longs today", levels

    reason = (f"✅ All filters passed | RSI={rsi:.1f if rsi else 'N/A'} | "
              f"VWAP={vwap_price:.2f if vwap_price else 'N/A'} | "
              f"Bias={report['market_bias']} | Confidence={report['confidence']}/100")

    return True, reason, levels


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════

def place_buy(symbol):
    ok, reason, levels = run_all_filters(symbol, "BUY")
    if not ok:
        log.info(f"SKIP {symbol}: {reason}")
        return {"status": "skipped", "reason": reason}

    equity = float(api.get_account().equity)
    price  = levels["price"]
    qty    = int((equity * RISK_PERCENTAGE) / price)
    if qty < 1:
        return {"status": "error", "reason": "Insufficient funds for 1 share"}

    stop  = levels["stop_price"]
    tp    = levels["limit_price"]

    # Don't buy if already holding
    try:
        pos = api.get_position(symbol)
        if int(pos.qty) > 0:
            return {"status": "skipped", "reason": f"Already holding {pos.qty} shares of {symbol}"}
    except: pass

    log.info(f"🟢 BUY {qty} {symbol} @ ${price:.2f} | SL=${stop} TP=${tp} | {reason}")

    order = api.submit_order(
        symbol=symbol, qty=qty, side="buy", type="market",
        time_in_force="day", order_class="bracket",
        stop_loss={"stop_price": stop},
        take_profit={"limit_price": tp}
    )

    log_t(symbol)
    report = get_market_report()

    return {
        "status":       "filled",
        "symbol":       symbol,
        "qty":          qty,
        "price":        price,
        "stop":         stop,
        "take_profit":  tp,
        "sl_pct":       levels["sl_pct"],
        "tp_pct":       levels["tp_pct"],
        "rsi":          levels.get("rsi"),
        "vwap":         levels.get("vwap"),
        "ema9":         levels.get("ema9"),
        "ema21":        levels.get("ema21"),
        "support":      levels.get("support"),
        "resistance":   levels.get("resistance"),
        "market_bias":  report.get("market_bias"),
        "confidence":   report.get("confidence"),
        "filter_reason":reason,
        "order_id":     order.id,
    }


def place_sell(symbol):
    try:
        pos = api.get_position(symbol)
        qty = int(pos.qty)
    except:
        return {"status": "skipped", "reason": f"No position in {symbol}"}

    for o in api.list_orders(status="open"):
        if o.symbol == symbol: api.cancel_order(o.id)

    order = api.submit_order(symbol=symbol, qty=qty, side="sell",
                             type="market", time_in_force="day")
    return {"status": "submitted", "symbol": symbol, "qty": qty, "order_id": order.id}


# ═══════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data   = request.get_json(force=True)
    symbol = str(data.get("ticker","")).upper().strip()
    action = str(data.get("action","")).upper().strip()
    log.info(f"Signal: {action} {symbol}")

    if symbol not in ALLOWED_TICKERS:
        return jsonify({"status":"skipped","reason":f"{symbol} not in allowed list"})

    result = place_buy(symbol) if action == "BUY" else place_sell(symbol) if action == "SELL" else None
    if result is None:
        return jsonify({"error":f"Unknown action: {action}"}), 400

    return jsonify(result)


@app.route("/", methods=["GET"])
def status():
    report = get_market_report()
    now    = pdt_now()
    positions = []
    try:
        for p in api.list_positions():
            positions.append({"symbol":p.symbol,"qty":p.qty,"entry":p.avg_entry_price,
                               "current":p.current_price,"pnl":p.unrealized_pl,"pnl_pct":p.unrealized_plpc})
    except: pass

    return jsonify({
        "status":             "ok",
        "service":            "ApexTrade Super Bot v2.0",
        "time_pdt":           now.strftime("%I:%M %p PDT"),
        "trading_allowed":    is_trading_allowed(),
        "market_report": {
            "trade_today":        report["trade_today"],
            "skip_reason":        report["skip_reason"],
            "market_bias":        report["market_bias"],
            "confidence":         report["confidence"],
            "vix":                report["vix"],
            "futures_sp500":      report["futures_sp500"],
            "futures_nasdaq":     report["futures_nasdaq"],
            "oil_change_pct":     report["oil_change_pct"],
            "best_entry_window":  report["best_entry_window"],
            "avoid_tickers":      report["avoid_tickers"],
            "reasons":            report["reasons"],
            "top_headlines":      report["top_headlines"][:5],
        },
        "settings": {
            "risk_pct":    f"{RISK_PERCENTAGE*100:.1f}%",
            "stop_loss":   f"{STOP_LOSS_PCT*100:.1f}%",
            "take_profit": f"{TAKE_PROFIT_PCT*100:.1f}%",
            "max_trades":  MAX_DAILY_TRADES,
            "tickers":     ALLOWED_TICKERS,
        },
        "trades_today":    {k: len(v) for k, v in trade_log.items()},
        "open_positions":  positions,
    })


@app.route("/market", methods=["GET"])
def market_report():
    """Get today's full market intelligence report."""
    force  = request.args.get("refresh","false").lower() == "true"
    report = get_market_report(force=force)
    return jsonify(report)


@app.route("/analyze/<symbol>", methods=["GET"])
def analyze(symbol):
    """Analyze a specific symbol — show entry/exit levels without trading."""
    symbol = symbol.upper()
    report = get_market_report()
    levels = calculate_entry_exit(symbol, report)
    if not levels:
        return jsonify({"error":"Could not fetch data"}), 500

    ok, reason, _ = run_all_filters(symbol, "BUY")
    return jsonify({
        "symbol":        symbol,
        "would_trade":   ok,
        "reason":        reason,
        "market_bias":   report["market_bias"],
        "confidence":    report["confidence"],
        "levels":        levels,
        "avoid_today":   symbol in report.get("avoid_tickers",[]),
    })


@app.route("/sentiment", methods=["GET"])
def sentiment_route():
    report = get_market_report()
    return jsonify({
        "sentiment":   report["market_bias"],
        "confidence":  report["confidence"],
        "trade_today": report["trade_today"],
        "vix":         report["vix"],
        "headlines":   report["top_headlines"][:5],
    })


@app.route("/close-all", methods=["POST"])
def close_all():
    api.cancel_all_orders()
    api.close_all_positions()
    return jsonify({"status":"ok","message":"All positions closed"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)), debug=False)

