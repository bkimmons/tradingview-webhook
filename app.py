from flask import Flask, request, jsonify
from flask_cors import CORS
import os, logging
from datetime import datetime
import pytz, requests
from bs4 import BeautifulSoup
import alpaca_trade_api as tradeapi

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
ALLOWED_TICKERS   = ["TSLA", "AAPL", "NVDA", "AMZN"]
RISK_PCT          = float(os.environ.get("RISK_PCT",        "0.005"))
STOP_LOSS_PCT     = float(os.environ.get("STOP_LOSS_PCT",   "0.02"))
TAKE_PROFIT_PCT   = float(os.environ.get("TAKE_PROFIT_PCT", "0.025"))
STOP_HOUR         = int(os.environ.get("STOP_HOUR",  "12"))
STOP_MIN          = int(os.environ.get("STOP_MIN",   "30"))
MAX_TRADES        = int(os.environ.get("MAX_TRADES", "2"))
MIN_CONFIDENCE    = int(os.environ.get("MIN_CONFIDENCE", "40"))

trade_log = {}
_cache    = {"date": None, "report": None}

# ═════════════════════════════════════════════════════════════════════════════
# MARKET INTELLIGENCE (built-in, no separate file needed)
# ═════════════════════════════════════════════════════════════════════════════

def get_market_report(force=False):
    pdt   = pytz.timezone("America/Los_Angeles")
    today = datetime.now(pdt).date()
    if not force and _cache["date"] == today and _cache["report"]:
        return _cache["report"]

    log.info("Running daily market intelligence scan...")
    report = {
        "date": today.isoformat(), "trade_today": True, "skip_reason": None,
        "market_bias": "neutral", "vix": None, "futures_sp500": None,
        "futures_nasdaq": None, "oil_change_pct": None, "top_headlines": [],
        "best_entry_window": "09:45-11:00 AM PDT", "avoid_tickers": [],
        "confidence": 50, "reasons": [],
    }

    _check_news(report)
    _check_vix_simple(report)
    _final_verdict(report)

    _cache["date"]   = today
    _cache["report"] = report
    log.info(f"Market: trade={report['trade_today']} bias={report['market_bias']} confidence={report['confidence']}")
    return report


def _check_news(report):
    bull_words = ["rally","surge","gain","rise","bull","strong","beats","record","growth","peace","ceasefire"]
    bear_words = ["fall","drop","decline","crash","bear","weak","miss","war","tariff","inflation","layoff","escalat"]
    headlines  = []
    try:
        r = requests.get("https://finviz.com/news.ashx",
                         headers={"User-Agent":"Mozilla/5.0"}, timeout=6)
        soup = BeautifulSoup(r.text, "html.parser")
        headlines = [a.text for a in soup.find_all("a", class_="tab-link")][:25]
    except Exception as e:
        log.warning(f"News fetch failed: {e}")
        return

    report["top_headlines"] = headlines[:8]
    lower = [h.lower() for h in headlines]
    bull  = sum(1 for h in lower for w in bull_words if w in h)
    bear  = sum(1 for h in lower for w in bear_words if w in h)
    log.info(f"Headlines: bull={bull} bear={bear}")

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

    crisis = ["war escalat","market crash","circuit breaker","trading halt","nuclear"]
    for h in lower:
        for w in crisis:
            if w in h:
                report["trade_today"] = False
                report["skip_reason"] = f"Crisis keyword detected: {w}"
                report["reasons"].append(f"CRISIS: {w}")
                return


def _check_vix_simple(report):
    try:
        r    = requests.get("https://finance.yahoo.com/quote/%5EVIX/",
                            headers={"User-Agent":"Mozilla/5.0"}, timeout=6)
        text = BeautifulSoup(r.text, "html.parser").get_text()
        import re
        m = re.search(r'"regularMarketPrice"[^}]*?"raw":([\d.]+)', text)
        if not m:
            m = re.search(r'VIX.*?(\d{2,3}\.\d{2})', text)
        if m:
            vix = float(m.group(1))
            report["vix"] = vix
            if vix > 35:
                report["trade_today"] = False
                report["skip_reason"] = f"VIX={vix:.1f} extreme fear"
                report["confidence"]  = max(report["confidence"] - 40, 0)
                report["reasons"].append(f"VIX={vix:.1f} EXTREME — no trading")
            elif vix > 25:
                report["confidence"]  = max(report["confidence"] - 20, 0)
                report["market_bias"] = "bearish"
                report["reasons"].append(f"VIX={vix:.1f} elevated fear")
            elif vix < 15:
                report["confidence"]  = min(report["confidence"] + 10, 100)
                report["reasons"].append(f"VIX={vix:.1f} low fear, good conditions")
    except Exception as e:
        log.warning(f"VIX fetch failed: {e}")


def _final_verdict(report):
    if not report["trade_today"]:
        return
    if report["confidence"] < MIN_CONFIDENCE:
        report["trade_today"] = False
        report["skip_reason"] = f"Confidence too low ({report['confidence']}/100)"
    if report["market_bias"] == "bullish":
        report["best_entry_window"] = "09:35-10:30 AM PDT"
    elif report["market_bias"] == "bearish":
        report["best_entry_window"] = "10:30-11:30 AM PDT"


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def pdt_now():
    return datetime.now(pytz.timezone("America/Los_Angeles"))

def trading_allowed():
    now = pdt_now()
    return now < now.replace(hour=STOP_HOUR, minute=STOP_MIN, second=0, microsecond=0)

def get_rsi(bars, p=14):
    try:
        d = bars["close"].diff()
        g = d.clip(lower=0).rolling(p).mean()
        l = (-d.clip(upper=0)).rolling(p).mean()
        return float((100-(100/(1+g/l))).iloc[-1])
    except: return None

def get_vwap(bars):
    try:
        t = (bars["high"]+bars["low"]+bars["close"])/3
        return float((t*bars["volume"]).sum()/bars["volume"].sum())
    except: return None

def get_ema(bars, p):
    try: return float(bars["close"].ewm(span=p,adjust=False).mean().iloc[-1])
    except: return None

def too_many(sym):
    today = pdt_now().date()
    return len([t for t in trade_log.get(sym,[]) if t.date()==today]) >= MAX_TRADES

def too_soon(sym, gap=15):
    times = trade_log.get(sym,[])
    if not times: return False
    return (pdt_now()-times[-1]).total_seconds()/60 < gap

def log_trade(sym):
    trade_log.setdefault(sym,[]).append(pdt_now())

def calc_levels(sym, report):
    try:
        bars  = api.get_bars(sym, "5Min", limit=60).df
        price = float(api.get_latest_trade(sym).price)
    except Exception as e:
        log.warning(f"Data fetch failed {sym}: {e}")
        return None

    rsi   = get_rsi(bars)
    vwap  = get_vwap(bars)
    ema9  = get_ema(bars, 9)
    ema21 = get_ema(bars, 21)
    bias  = report.get("market_bias","neutral")
    conf  = report.get("confidence", 50)

    if bias == "bullish" and conf > 65:
        sl, tp = STOP_LOSS_PCT, TAKE_PROFIT_PCT * 1.5
    elif bias == "bearish":
        sl, tp = STOP_LOSS_PCT * 0.75, TAKE_PROFIT_PCT * 0.8
    else:
        sl, tp = STOP_LOSS_PCT, TAKE_PROFIT_PCT

    try:
        support    = float(bars["low"].tail(20).min())
        resistance = float(bars["high"].tail(20).max())
        stop  = round(max(support * 0.995, price * (1-sl)), 2)
        limit = round(min(resistance * 0.998, price * (1+tp)), 2) if resistance < price*(1+tp*2) else round(price*(1+tp),2)
    except:
        stop  = round(price * (1-sl), 2)
        limit = round(price * (1+tp), 2)

    return {"price":price,"stop":stop,"limit":limit,"rsi":rsi,"vwap":vwap,"ema9":ema9,"ema21":ema21}


# ═════════════════════════════════════════════════════════════════════════════
# SMART FILTERS
# ═════════════════════════════════════════════════════════════════════════════

def run_filters(sym, action):
    if action != "BUY":
        return True, "sell always allowed", None

    report = get_market_report()
    if not report["trade_today"]:
        return False, f"Market intelligence: {report['skip_reason']}", None
    if report["confidence"] < MIN_CONFIDENCE:
        return False, f"Market confidence too low ({report['confidence']}/100)", None
    if sym in report.get("avoid_tickers",[]):
        return False, f"{sym} flagged to avoid today", None
    if not trading_allowed():
        return False, f"Past trading cutoff {STOP_HOUR}:{STOP_MIN:02d} PDT", None
    if too_many(sym):
        return False, f"Max {MAX_TRADES} trades/day for {sym}", None
    if too_soon(sym):
        return False, "15min cooldown active", None

    levels = calc_levels(sym, report)
    if not levels:
        return False, "Could not fetch price data", None

    rsi, vwap, ema9, ema21 = levels.get("rsi"), levels.get("vwap"), levels.get("ema9"), levels.get("ema21")
    price = levels["price"]

    if rsi and rsi > 72:
        return False, f"RSI={rsi:.1f} overbought", levels
    if vwap and (price-vwap)/vwap < -0.003:
        return False, f"Price below VWAP — no uptrend", levels
    if ema9 and ema21 and ema9 < ema21:
        return False, f"EMA9 below EMA21 — downtrend", levels
    if report["market_bias"] == "bearish" and report["confidence"] < 40:
        return False, "Market too bearish for new longs", levels

    reason = f"All filters passed | RSI={rsi:.1f if rsi else 'N/A'} | bias={report['market_bias']} | conf={report['confidence']}"
    return True, reason, levels


# ═════════════════════════════════════════════════════════════════════════════
# ORDER LOGIC
# ═════════════════════════════════════════════════════════════════════════════

def place_buy(sym):
    ok, reason, levels = run_filters(sym, "BUY")
    if not ok:
        log.info(f"SKIP {sym}: {reason}")
        return {"status":"skipped","reason":reason}

    try:
        pos = api.get_position(sym)
        if int(pos.qty) > 0:
            return {"status":"skipped","reason":f"Already holding {pos.qty} shares"}
    except: pass

    equity = float(api.get_account().equity)
    price  = levels["price"]
    qty    = int((equity * RISK_PCT) / price)
    if qty < 1:
        return {"status":"error","reason":"Insufficient funds"}

    stop  = levels["stop"]
    limit = levels["limit"]

    order = api.submit_order(
        symbol=sym, qty=qty, side="buy", type="market",
        time_in_force="day", order_class="bracket",
        stop_loss={"stop_price":stop}, take_profit={"limit_price":limit}
    )
    log_trade(sym)
    report = get_market_report()
    return {
        "status":"filled","symbol":sym,"qty":qty,"price":price,
        "stop":stop,"take_profit":limit,
        "rsi":levels.get("rsi"),"vwap":levels.get("vwap"),
        "market_bias":report["market_bias"],"confidence":report["confidence"],
        "reason":reason,"order_id":order.id
    }


def place_sell(sym):
    try:
        qty = int(api.get_position(sym).qty)
    except:
        return {"status":"skipped","reason":f"No position in {sym}"}
    for o in api.list_orders(status="open"):
        if o.symbol == sym: api.cancel_order(o.id)
    order = api.submit_order(symbol=sym,qty=qty,side="sell",type="market",time_in_force="day")
    return {"status":"submitted","symbol":sym,"qty":qty,"order_id":order.id}


# ═════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data   = request.get_json(force=True)
    sym    = str(data.get("ticker","")).upper().strip()
    action = str(data.get("action","")).upper().strip()
    log.info(f"Signal: {action} {sym}")
    if sym not in ALLOWED_TICKERS:
        return jsonify({"status":"skipped","reason":f"{sym} not allowed"})
    result = place_buy(sym) if action=="BUY" else place_sell(sym) if action=="SELL" else None
    if result is None:
        return jsonify({"error":f"Unknown action: {action}"}), 400
    return jsonify(result)


@app.route("/", methods=["GET"])
def status():
    report    = get_market_report()
    positions = []
    try:
        for p in api.list_positions():
            positions.append({"symbol":p.symbol,"qty":p.qty,"entry":p.avg_entry_price,
                               "current":p.current_price,"pnl":p.unrealized_pl})
    except: pass
    return jsonify({
        "status":"ok","service":"ApexTrade Super Bot v2.0",
        "time_pdt":pdt_now().strftime("%I:%M %p PDT"),
        "trading_allowed":trading_allowed(),
        "market":{
            "trade_today":   report["trade_today"],
            "skip_reason":   report["skip_reason"],
            "bias":          report["market_bias"],
            "confidence":    report["confidence"],
            "vix":           report["vix"],
            "best_window":   report["best_entry_window"],
            "reasons":       report["reasons"],
        },
        "settings":{"risk_pct":f"{RISK_PCT*100:.1f}%","stop_loss":f"{STOP_LOSS_PCT*100:.1f}%",
                    "take_profit":f"{TAKE_PROFIT_PCT*100:.1f}%","max_trades":MAX_TRADES},
        "trades_today":{k:len(v) for k,v in trade_log.items()},
        "open_positions":positions,
    })


@app.route("/market", methods=["GET"])
def market():
    force = request.args.get("refresh","false").lower()=="true"
    return jsonify(get_market_report(force=force))


@app.route("/analyze/<symbol>", methods=["GET"])
def analyze(symbol):
    sym    = symbol.upper()
    report = get_market_report()
    levels = calc_levels(sym, report)
    if not levels: return jsonify({"error":"Could not fetch data"}), 500
    ok, reason, _ = run_filters(sym, "BUY")
    return jsonify({"symbol":sym,"would_trade":ok,"reason":reason,
                    "bias":report["market_bias"],"confidence":report["confidence"],"levels":levels})


@app.route("/sentiment", methods=["GET"])
def sentiment():
    r = get_market_report()
    return jsonify({"sentiment":r["market_bias"],"confidence":r["confidence"],
                    "trade_today":r["trade_today"],"vix":r["vix"],"headlines":r["top_headlines"][:5]})


@app.route("/close-all", methods=["POST"])
def close_all():
    api.cancel_all_orders()
    api.close_all_positions()
    return jsonify({"status":"ok","message":"All positions closed"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)), debug=False)
