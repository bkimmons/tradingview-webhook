# ═════════════════════════════════════════════════════════════════════════════
# APEXTRADE SUPER BOT v6.0 — SNIPER INSTITUTIONAL EDITION
# Combines: Institutional scoring + Regime detection + Sniper filters
#           + Auto direction + News sentiment + VIX filter + SPY filter
#           + Volume filter + EMA200 filter + Stripe webhook
# ═════════════════════════════════════════════════════════════════════════════

import os, logging, re, stripe
from flask import Flask, request, jsonify
from flask_cors import CORS
import alpaca_trade_api as tradeapi
import pandas as pd
from datetime import datetime
import pytz, requests
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Alpaca ────────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("ALPACA_API_KEY")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")

# ── Stripe ────────────────────────────────────────────────────────────────────
stripe.api_key          = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET   = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_STARTER_PRICE_ID = os.environ.get("STRIPE_STARTER_PRICE_ID", "")
STRIPE_PRO_PRICE_ID     = os.environ.get("STRIPE_PRO_PRICE_ID", "")
STRIPE_HEDGE_PRICE_ID   = os.environ.get("STRIPE_HEDGE_PRICE_ID", "")

# ── Settings ──────────────────────────────────────────────────────────────────
ALLOWED_TICKERS   = ["TSLA", "AAPL", "NVDA", "AMZN"]
RISK_PCT          = float(os.environ.get("RISK_PCT",        "0.01"))
STOP_LOSS_PCT     = float(os.environ.get("STOP_LOSS_PCT",   "0.015"))  # 1.5% stop
TAKE_PROFIT_PCT   = float(os.environ.get("TAKE_PROFIT_PCT", "0.035"))  # 3.5% target
MAX_DAILY_LOSS    = float(os.environ.get("MAX_DAILY_LOSS",  "-0.02"))  # -2% kill switch
MAX_TRADES        = int(os.environ.get("MAX_TRADES",    "2"))
MIN_SCORE         = int(os.environ.get("MIN_SCORE",     "80"))         # Must score 80/100
MAX_VIX           = float(os.environ.get("MAX_VIX",     "25"))         # No trade above VIX 25
SPY_DOWN_LIMIT    = float(os.environ.get("SPY_DOWN_LIMIT", "-0.01"))   # No trade if SPY down >1%
VOLUME_MIN        = float(os.environ.get("VOLUME_MIN",  "1.5"))        # 1.5x avg volume min
STOP_HOUR         = int(os.environ.get("STOP_HOUR",     "10"))         # Stop at 10:30 AM PDT
STOP_MIN          = int(os.environ.get("STOP_MIN",      "30"))
EMA200_FILTER     = os.environ.get("EMA200_FILTER", "true").lower() == "true"

trade_log = {}
_cache    = {"date": None, "report": None}


# ═════════════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def pdt_now():
    return datetime.now(pytz.timezone("America/Los_Angeles"))

def trading_allowed():
    now          = pdt_now()
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=STOP_HOUR, minute=STOP_MIN, second=0, microsecond=0)
    return market_open <= now <= market_close

def too_many(sym):
    today = pdt_now().date()
    return len([t for t in trade_log.get(sym, []) if t.date() == today]) >= MAX_TRADES

def too_soon(sym, gap=15):
    times = trade_log.get(sym, [])
    if not times: return False
    return (pdt_now() - times[-1]).total_seconds() / 60 < gap

def log_trade(sym):
    trade_log.setdefault(sym, []).append(pdt_now())


# ═════════════════════════════════════════════════════════════════════════════
# MARKET REGIME DETECTION (auto-direction)
# ═════════════════════════════════════════════════════════════════════════════

def detect_regime():
    """
    Detects market regime using SPY EMA9/21 on 5m chart.
    Returns: 'bull', 'bear', or 'chop'
    If regime is bear -> bot automatically flips to SELL signals
    If regime is bull -> bot trades BUY signals
    If regime is chop -> bot sits out
    """
    try:
        bars  = api.get_bars("SPY", "5Min", limit=50).df
        ema9  = float(bars["close"].ewm(span=9).mean().iloc[-1])
        ema21 = float(bars["close"].ewm(span=21).mean().iloc[-1])
        price = float(bars["close"].iloc[-1])
        prev  = float(bars["close"].iloc[-2])
        momentum = (price - prev) / prev

        if ema9 > ema21 and momentum > 0:
            return "bull"
        elif ema9 < ema21 and momentum < 0:
            return "bear"
        else:
            return "chop"
    except Exception as e:
        log.warning(f"Regime detection failed: {e}")
        return "chop"

def auto_direction(signal_action, regime):
    """
    Overrides trade direction based on market regime.
    Bear market -> convert BUY to SELL (short the weakness)
    Bull market -> keep BUY signals
    Chop -> sit out
    """
    if regime == "chop":
        return None, "Market in chop — no trades"
    if regime == "bear" and signal_action == "BUY":
        log.info("Regime override: BUY -> SELL (bear market detected)")
        return "SELL", "Bear regime auto-flip"
    if regime == "bull" and signal_action == "SELL":
        log.info("Regime override: SELL -> BUY (bull market detected)")
        return "BUY", "Bull regime auto-flip"
    return signal_action, "Direction confirmed by regime"


# ═════════════════════════════════════════════════════════════════════════════
# MARKET INTELLIGENCE (news + VIX + SPY)
# ═════════════════════════════════════════════════════════════════════════════

def get_market_report(force=False):
    pdt   = pytz.timezone("America/Los_Angeles")
    today = datetime.now(pdt).date()
    if not force and _cache["date"] == today and _cache["report"]:
        return _cache["report"]

    log.info("Running daily market intelligence scan...")
    report = {
        "date": today.isoformat(), "trade_today": True, "skip_reason": None,
        "market_bias": "neutral", "vix": None, "spy_change": None,
        "top_headlines": [], "best_entry_window": "09:45-10:30 AM PDT",
        "avoid_tickers": [], "confidence": 50, "reasons": [],
    }

    _check_news(report)
    _check_vix(report)
    _check_spy(report)
    _final_verdict(report)

    _cache["date"]   = today
    _cache["report"] = report
    log.info(f"Market intel: trade={report['trade_today']} bias={report['market_bias']} conf={report['confidence']} vix={report['vix']}")
    return report


def _check_news(report):
    bull_words = ["rally","surge","gain","rise","bull","strong","beats","record","growth","peace","ceasefire","upgrade"]
    bear_words = ["fall","drop","decline","crash","bear","weak","miss","war","tariff","inflation","layoff","escalat","downgrade"]
    headlines  = []
    try:
        r = requests.get("https://finviz.com/news.ashx",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        soup      = BeautifulSoup(r.text, "html.parser")
        headlines = [a.text for a in soup.find_all("a", class_="tab-link")][:25]
    except Exception as e:
        log.warning(f"News fetch failed: {e}")
        return

    report["top_headlines"] = headlines[:8]
    lower = [h.lower() for h in headlines]
    bull  = sum(1 for h in lower for w in bull_words if w in h)
    bear  = sum(1 for h in lower for w in bear_words if w in h)

    if bear > bull + 5:
        report["market_bias"] = "bearish"
        report["confidence"]  = max(report["confidence"] - 20, 0)
        report["reasons"].append(f"News bearish (bull={bull} bear={bear})")
    elif bull > bear + 5:
        report["market_bias"] = "bullish"
        report["confidence"]  = min(report["confidence"] + 15, 100)
        report["reasons"].append(f"News bullish (bull={bull} bear={bear})")
    else:
        report["reasons"].append(f"News neutral (bull={bull} bear={bear})")

    crisis = ["war escalat", "market crash", "circuit breaker", "trading halt", "nuclear"]
    for h in lower:
        for w in crisis:
            if w in h:
                report["trade_today"] = False
                report["skip_reason"] = f"Crisis keyword: {w}"
                return


def _check_vix(report):
    try:
        r    = requests.get("https://finance.yahoo.com/quote/%5EVIX/",
                            headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        text = BeautifulSoup(r.text, "html.parser").get_text()
        m    = re.search(r'"regularMarketPrice"[^}]*?"raw":([\d.]+)', text)
        if not m:
            m = re.search(r'VIX.*?(\d{2,3}\.\d{2})', text)
        if m:
            vix = float(m.group(1))
            report["vix"] = vix
            if vix > 35:
                report["trade_today"] = False
                report["skip_reason"] = f"VIX={vix:.1f} extreme fear"
                report["reasons"].append(f"VIX={vix:.1f} EXTREME — no trading")
            elif vix > MAX_VIX:
                report["trade_today"] = False
                report["skip_reason"] = f"VIX={vix:.1f} above max {MAX_VIX}"
                report["reasons"].append(f"VIX={vix:.1f} too high — skipping")
            elif vix < 15:
                report["confidence"]  = min(report["confidence"] + 10, 100)
                report["reasons"].append(f"VIX={vix:.1f} ideal conditions")
            else:
                report["reasons"].append(f"VIX={vix:.1f} acceptable")
    except Exception as e:
        log.warning(f"VIX fetch failed: {e}")


def _check_spy(report):
    try:
        spy_bars   = api.get_bars("SPY", "1Day", limit=2).df
        if len(spy_bars) >= 2:
            prev       = float(spy_bars["close"].iloc[-2])
            curr       = float(spy_bars["close"].iloc[-1])
            spy_change = (curr - prev) / prev
            report["spy_change"] = round(spy_change * 100, 2)
            if spy_change < SPY_DOWN_LIMIT:
                report["trade_today"] = False
                report["skip_reason"] = f"SPY down {spy_change*100:.1f}% — defensive mode"
                report["reasons"].append(f"SPY={spy_change*100:.1f}% — no long trades")
            else:
                report["reasons"].append(f"SPY={spy_change*100:.1f}% — market OK")
    except Exception as e:
        log.warning(f"SPY check failed: {e}")


def _final_verdict(report):
    if not report["trade_today"]: return
    if report["confidence"] < 40:
        report["trade_today"] = False
        report["skip_reason"] = f"Confidence too low ({report['confidence']}/100)"


# ═════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ═════════════════════════════════════════════════════════════════════════════

def compute_rsi(df, period=14):
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = -delta.clip(upper=0).rolling(period).mean()
    rs    = gain / loss
    return float((100 - (100 / (1 + rs))).iloc[-1])

def compute_vwap(df):
    return float((df["close"] * df["volume"]).sum() / df["volume"].sum())

def get_indicators(symbol):
    bars_5m  = api.get_bars(symbol, "5Min", limit=250).df
    bars_day = api.get_bars(symbol, "1Day", limit=210).df
    price    = float(api.get_latest_trade(symbol).price)

    rsi          = compute_rsi(bars_5m)
    vwap         = compute_vwap(bars_5m)
    ema9         = float(bars_5m["close"].ewm(span=9,   adjust=False).mean().iloc[-1])
    ema21        = float(bars_5m["close"].ewm(span=21,  adjust=False).mean().iloc[-1])
    ema200_daily = float(bars_day["close"].ewm(span=200, adjust=False).mean().iloc[-1])
    volume_ratio = float(bars_5m["volume"].iloc[-1] / bars_5m["volume"].iloc[:-1].mean())

    return {
        "price":        price,
        "rsi":          round(rsi, 2),
        "vwap":         round(vwap, 2),
        "ema9":         round(ema9, 2),
        "ema21":        round(ema21, 2),
        "ema200_daily": round(ema200_daily, 2),
        "volume_ratio": round(volume_ratio, 2),
    }


# ═════════════════════════════════════════════════════════════════════════════
# AI SCORING SYSTEM (0-100)
# ═════════════════════════════════════════════════════════════════════════════

def score_trade(data, regime, report):
    """
    Institutional scoring system. Must hit MIN_SCORE (80) to execute.
    Each condition adds points. All must align for a sniper entry.
    """
    score   = 0
    details = []

    # RSI in sweet spot (50-65 = momentum without overbought)
    if 50 < data["rsi"] < 65:
        score += 20
        details.append(f"RSI={data['rsi']} +20")
    elif 45 < data["rsi"] <= 50:
        score += 10
        details.append(f"RSI={data['rsi']} borderline +10")
    else:
        details.append(f"RSI={data['rsi']} fail +0")

    # Price above VWAP
    if data["price"] > data["vwap"]:
        score += 20
        details.append(f"Above VWAP +20")
    else:
        details.append(f"Below VWAP fail +0")

    # EMA 9 above EMA 21
    if data["ema9"] > data["ema21"]:
        score += 20
        details.append(f"EMA9>EMA21 +20")
    else:
        details.append(f"EMA9<EMA21 fail +0")

    # Volume confirmation
    if data["volume_ratio"] > VOLUME_MIN:
        score += 20
        details.append(f"Volume={data['volume_ratio']}x +20")
    else:
        details.append(f"Volume={data['volume_ratio']}x fail +0")

    # 200 EMA major trend filter
    if EMA200_FILTER:
        if data["price"] > data["ema200_daily"]:
            score += 10
            details.append(f"Above EMA200 +10")
        else:
            details.append(f"Below EMA200 fail +0")

    # Market regime bonus
    if regime == "bull":
        score += 10
        details.append(f"Bull regime +10")
    elif regime == "bear":
        score += 5
        details.append(f"Bear regime +5")
    else:
        details.append(f"Chop regime +0")

    log.info(f"Score: {' | '.join(details)} = {score}/100")
    return score, details


# ═════════════════════════════════════════════════════════════════════════════
# RISK CONTROLS
# ═════════════════════════════════════════════════════════════════════════════

def kill_switch():
    try:
        acc = api.get_account()
        pnl = float(acc.equity) - float(acc.last_equity)
        if pnl / float(acc.equity) < MAX_DAILY_LOSS:
            log.warning(f"KILL SWITCH: Daily P&L=${pnl:.2f} exceeded max loss")
            return True, pnl
        return False, pnl
    except Exception as e:
        log.warning(f"Kill switch check failed: {e}")
        return False, 0

def position_size(price, equity):
    if equity > 50000:
        risk = 0.02
    elif equity > 20000:
        risk = 0.015
    else:
        risk = RISK_PCT
    return max(int((equity * risk) / price), 1)

def calc_bracket(price, action):
    if action == "BUY":
        stop  = round(price * (1 - STOP_LOSS_PCT), 2)
        limit = round(price * (1 + TAKE_PROFIT_PCT), 2)
    else:
        stop  = round(price * (1 + STOP_LOSS_PCT), 2)
        limit = round(price * (1 - TAKE_PROFIT_PCT), 2)
    return stop, limit


# ═════════════════════════════════════════════════════════════════════════════
# EXECUTION ENGINE
# ═════════════════════════════════════════════════════════════════════════════

def execute_trade(symbol, signal_action):
    symbol = symbol.upper()

    killed, pnl = kill_switch()
    if killed:
        return {"status": "stopped", "reason": f"Daily loss kill switch (P&L=${pnl:.2f})"}

    if not trading_allowed():
        return {"status": "skipped", "reason": f"Outside sniper window (9:30-{STOP_HOUR}:{STOP_MIN:02d} AM PDT)"}

    if too_many(symbol):
        return {"status": "skipped", "reason": f"Max {MAX_TRADES} trades/day for {symbol}"}

    if too_soon(symbol):
        return {"status": "skipped", "reason": "15min cooldown active"}

    report = get_market_report()
    if not report["trade_today"]:
        return {"status": "skipped", "reason": f"Market intel: {report['skip_reason']}"}

    regime = detect_regime()
    action, regime_reason = auto_direction(signal_action, regime)
    if action is None:
        return {"status": "skipped", "reason": regime_reason}

    try:
        data = get_indicators(symbol)
    except Exception as e:
        return {"status": "error", "reason": f"Indicator fetch failed: {e}"}

    score, score_details = score_trade(data, regime, report)
    if score < MIN_SCORE:
        return {
            "status":  "skipped",
            "reason":  f"Score {score}/100 below minimum {MIN_SCORE}",
            "score":   score,
            "details": score_details,
        }

    try:
        pos = api.get_position(symbol)
        if int(pos.qty) > 0 and action == "BUY":
            return {"status": "skipped", "reason": f"Already holding {pos.qty} shares of {symbol}"}
    except: pass

    equity      = float(api.get_account().equity)
    price       = data["price"]
    qty         = position_size(price, equity)
    stop, limit = calc_bracket(price, action)
    side        = "buy" if action == "BUY" else "sell"

    try:
        order = api.submit_order(
            symbol=symbol, qty=qty, side=side,
            type="market", time_in_force="day",
            order_class="bracket",
            stop_loss={"stop_price": stop},
            take_profit={"limit_price": limit}
        )
        log_trade(symbol)
        log.info(f"SNIPER ENTRY: {action} {symbol} qty={qty} price={price} stop={stop} tp={limit} score={score}")

        return {
            "status":        "filled",
            "symbol":        symbol,
            "action":        action,
            "signal":        signal_action,
            "regime":        regime,
            "regime_reason": regime_reason,
            "qty":           qty,
            "price":         price,
            "stop":          stop,
            "take_profit":   limit,
            "score":         f"{score}/100",
            "score_details": score_details,
            "indicators":    data,
            "market_bias":   report["market_bias"],
            "confidence":    report["confidence"],
            "order_id":      order.id,
        }
    except Exception as e:
        log.error(f"Order failed: {e}")
        return {"status": "error", "reason": str(e)}


def close_position(symbol):
    try:
        qty = int(api.get_position(symbol).qty)
    except:
        return {"status": "skipped", "reason": f"No position in {symbol}"}
    for o in api.list_orders(status="open"):
        if o.symbol == symbol:
            api.cancel_order(o.id)
    order = api.submit_order(symbol=symbol, qty=qty, side="sell", type="market", time_in_force="day")
    log.info(f"CLOSE: {symbol} qty={qty}")
    return {"status": "submitted", "symbol": symbol, "qty": qty, "order_id": order.id}


# ═════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data   = request.get_json(force=True)
    symbol = str(data.get("ticker", "")).upper().strip()
    action = str(data.get("action", "")).upper().strip()
    log.info(f"Signal received: {action} {symbol}")

    if symbol not in ALLOWED_TICKERS:
        return jsonify({"status": "skipped", "reason": f"{symbol} not in allowed list"})

    if action == "SELL":
        result = close_position(symbol)
    elif action == "BUY":
        result = execute_trade(symbol, action)
    else:
        return jsonify({"error": f"Unknown action: {action}"}), 400

    return jsonify(result)


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload    = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        log.warning(f"Stripe webhook error: {e}")
        return jsonify({"error": str(e)}), 400

    etype = event["type"]
    data  = event["data"]["object"]
    log.info(f"Stripe event: {etype}")

    if etype == "customer.subscription.created":
        customer_id = data.get("customer")
        plan        = data.get("items", {}).get("data", [{}])[0].get("price", {}).get("id", "")
        log.info(f"NEW SUBSCRIBER: customer={customer_id} plan={plan}")
    elif etype == "invoice.payment_succeeded":
        amount = data.get("amount_paid", 0) / 100
        log.info(f"PAYMENT: ${amount:.2f}")
    elif etype == "invoice.payment_failed":
        log.warning(f"PAYMENT FAILED: customer={data.get('customer')}")
    elif etype == "customer.subscription.deleted":
        log.info(f"CANCELLED: customer={data.get('customer')}")

    return jsonify({"status": "ok"})


@app.route("/", methods=["GET"])
def home():
    report      = get_market_report()
    regime      = detect_regime()
    killed, pnl = kill_switch()
    positions   = []
    try:
        for p in api.list_positions():
            positions.append({
                "symbol":  p.symbol,
                "qty":     p.qty,
                "entry":   p.avg_entry_price,
                "current": p.current_price,
                "pnl":     p.unrealized_pl,
            })
    except: pass

    return jsonify({
        "status":          "ok",
        "service":         "ApexTrade Super Bot v6.0 — Sniper Institutional Edition",
        "time_pdt":        pdt_now().strftime("%I:%M %p PDT"),
        "trading_allowed": trading_allowed() and not killed,
        "kill_switch":     killed,
        "daily_pnl":       round(pnl, 2),
        "regime":          regime,
        "market": {
            "trade_today":  report["trade_today"],
            "skip_reason":  report["skip_reason"],
            "bias":         report["market_bias"],
            "confidence":   report["confidence"],
            "vix":          report["vix"],
            "spy_change":   report.get("spy_change"),
            "best_window":  report["best_entry_window"],
            "reasons":      report["reasons"],
        },
        "settings": {
            "risk_pct":       f"{RISK_PCT*100:.1f}%",
            "stop_loss":      f"{STOP_LOSS_PCT*100:.1f}%",
            "take_profit":    f"{TAKE_PROFIT_PCT*100:.1f}%",
            "max_trades":     MAX_TRADES,
            "min_score":      f"{MIN_SCORE}/100",
            "max_vix":        MAX_VIX,
            "volume_min":     f"{VOLUME_MIN}x",
            "sniper_window":  f"9:30-{STOP_HOUR}:{STOP_MIN:02d} AM PDT",
            "ema200_filter":  EMA200_FILTER,
            "auto_direction": "enabled",
        },
        "trades_today":   {k: len(v) for k, v in trade_log.items()},
        "open_positions": positions,
    })


@app.route("/market", methods=["GET"])
def market():
    force = request.args.get("refresh", "false").lower() == "true"
    return jsonify(get_market_report(force=force))


@app.route("/regime", methods=["GET"])
def regime_check():
    regime = detect_regime()
    return jsonify({"regime": regime, "time": pdt_now().strftime("%I:%M %p PDT")})


@app.route("/analyze/<symbol>", methods=["GET"])
def analyze(symbol):
    sym    = symbol.upper()
    report = get_market_report()
    regime = detect_regime()
    try:
        data = get_indicators(sym)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    score, details = score_trade(data, regime, report)
    action, _ = auto_direction("BUY", regime)
    return jsonify({
        "symbol":      sym,
        "would_trade": score >= MIN_SCORE and report["trade_today"],
        "score":       f"{score}/100",
        "min_score":   MIN_SCORE,
        "direction":   action,
        "regime":      regime,
        "indicators":  data,
        "details":     details,
        "market_bias": report["market_bias"],
        "confidence":  report["confidence"],
    })


@app.route("/sentiment", methods=["GET"])
def sentiment():
    r = get_market_report()
    return jsonify({
        "sentiment":   r["market_bias"],
        "confidence":  r["confidence"],
        "trade_today": r["trade_today"],
        "vix":         r["vix"],
        "spy_change":  r.get("spy_change"),
        "headlines":   r["top_headlines"][:5],
    })


@app.route("/close-all", methods=["POST"])
def close_all():
    api.cancel_all_orders()
    api.close_all_positions()
    log.info("All positions closed manually")
    return jsonify({"status": "ok", "message": "All positions closed"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
