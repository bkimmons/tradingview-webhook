# ═══════════════════════════════════════════════════
# APEXTRADE SUPER BOT v7.0 — FULL SNIPER EDITION
# Added to v6.0: ATR dynamic stops, partial profits,
# trailing stops, MACD, Bollinger, support/resistance
# ═══════════════════════════════════════════════════

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

API_KEY    = os.environ.get("ALPACA_API_KEY")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version="v2")

stripe.api_key          = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET   = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

ALLOWED_TICKERS   = ["TSLA", "AAPL", "NVDA", "AMZN"]
RISK_PCT          = float(os.environ.get("RISK_PCT",        "0.01"))
STOP_LOSS_PCT     = float(os.environ.get("STOP_LOSS_PCT",   "0.015"))
TAKE_PROFIT_PCT   = float(os.environ.get("TAKE_PROFIT_PCT", "0.035"))
MAX_DAILY_LOSS    = float(os.environ.get("MAX_DAILY_LOSS",  "-0.02"))
MAX_TRADES        = int(os.environ.get("MAX_TRADES",    "2"))
MIN_SCORE         = int(os.environ.get("MIN_SCORE",     "80"))
MAX_VIX           = float(os.environ.get("MAX_VIX",     "25"))
SPY_DOWN_LIMIT    = float(os.environ.get("SPY_DOWN_LIMIT", "-0.01"))
VOLUME_MIN        = float(os.environ.get("VOLUME_MIN",  "1.5"))
STOP_HOUR         = int(os.environ.get("STOP_HOUR",     "10"))
STOP_MIN          = int(os.environ.get("STOP_MIN",      "30"))
EMA200_FILTER     = os.environ.get("EMA200_FILTER", "true").lower() == "true"
USE_ATR_STOPS     = os.environ.get("USE_ATR_STOPS", "true").lower() == "true"
ATR_STOP_MULT     = float(os.environ.get("ATR_STOP_MULT",  "1.5"))
ATR_TP1_MULT      = float(os.environ.get("ATR_TP1_MULT",   "2.0"))
ATR_TP2_MULT      = float(os.environ.get("ATR_TP2_MULT",   "3.5"))
MIN_RR            = float(os.environ.get("MIN_RR",         "1.2"))

trade_log       = {}
_cache          = {"date": None, "report": None}
_trailing_stops = {}
_open_snipes    = {}

def pdt_now():
    return datetime.now(pytz.timezone("America/Los_Angeles"))

def trading_allowed():
    now = pdt_now()
    return now.replace(hour=9,minute=30,second=0,microsecond=0) <= now <= now.replace(hour=STOP_HOUR,minute=STOP_MIN,second=0,microsecond=0)

def too_many(sym):
    today = pdt_now().date()
    return len([t for t in trade_log.get(sym,[]) if t.date()==today]) >= MAX_TRADES

def too_soon(sym, gap=15):
    times = trade_log.get(sym,[])
    if not times: return False
    return (pdt_now()-times[-1]).total_seconds()/60 < gap

def log_trade(sym):
    trade_log.setdefault(sym,[]).append(pdt_now())

def detect_regime():
    try:
        bars = api.get_bars("SPY","5Min",limit=50).df
        ema9  = float(bars["close"].ewm(span=9).mean().iloc[-1])
        ema21 = float(bars["close"].ewm(span=21).mean().iloc[-1])
        price = float(bars["close"].iloc[-1])
        prev  = float(bars["close"].iloc[-2])
        mom   = (price-prev)/prev
        if ema9>ema21 and mom>0:   return "bull"
        elif ema9<ema21 and mom<0: return "bear"
        else:                      return "chop"
    except: return "chop"

def auto_direction(signal_action, regime):
    if regime=="chop":               return None, "Market chop — no trades"
    if regime=="bear" and signal_action=="BUY":  return "SELL","Bear regime auto-flip"
    if regime=="bull" and signal_action=="SELL": return "BUY", "Bull regime auto-flip"
    return signal_action, "Direction confirmed"

def get_market_report(force=False):
    pdt   = pytz.timezone("America/Los_Angeles")
    today = datetime.now(pdt).date()
    if not force and _cache["date"]==today and _cache["report"]: return _cache["report"]
    report = {"date":today.isoformat(),"trade_today":True,"skip_reason":None,
              "market_bias":"neutral","vix":None,"spy_change":None,"top_headlines":[],
              "best_entry_window":"09:45-10:30 AM PDT","avoid_tickers":[],"confidence":50,"reasons":[]}
    _check_news(report); _check_vix(report); _check_spy(report); _final_verdict(report)
    _cache["date"]=today; _cache["report"]=report
    return report

def _check_news(r):
    bull = ["rally","surge","gain","rise","bull","strong","beats","record","growth","peace","ceasefire","upgrade"]
    bear = ["fall","drop","decline","crash","bear","weak","miss","war","tariff","inflation","layoff","escalat","downgrade"]
    try:
        res   = requests.get("https://finviz.com/news.ashx",headers={"User-Agent":"Mozilla/5.0"},timeout=6)
        heads = [a.text for a in BeautifulSoup(res.text,"html.parser").find_all("a",class_="tab-link")][:25]
        r["top_headlines"]=heads[:8]; lower=[h.lower() for h in heads]
        bs=sum(1 for h in lower for w in bull if w in h); be=sum(1 for h in lower for w in bear if w in h)
        if be>bs+5: r["market_bias"]="bearish"; r["confidence"]=max(r["confidence"]-20,0); r["reasons"].append(f"News bearish bs={bs} be={be}")
        elif bs>be+5: r["market_bias"]="bullish"; r["confidence"]=min(r["confidence"]+15,100); r["reasons"].append(f"News bullish bs={bs} be={be}")
        else: r["reasons"].append(f"News neutral bs={bs} be={be}")
        for h in lower:
            for w in ["war escalat","market crash","circuit breaker","trading halt","nuclear"]:
                if w in h: r["trade_today"]=False; r["skip_reason"]=f"Crisis:{w}"; return
    except Exception as e: log.warning(f"News: {e}")

def _check_vix(r):
    try:
        res=requests.get("https://finance.yahoo.com/quote/%5EVIX/",headers={"User-Agent":"Mozilla/5.0"},timeout=6)
        text=BeautifulSoup(res.text,"html.parser").get_text()
        m=re.search(r'"regularMarketPrice"[^}]*?"raw":([\d.]+)',text)
        if not m: m=re.search(r'VIX.*?(\d{2,3}\.\d{2})',text)
        if m:
            vix=float(m.group(1)); r["vix"]=vix
            if vix>35: r["trade_today"]=False; r["skip_reason"]=f"VIX={vix:.1f} extreme"; r["reasons"].append(f"VIX={vix:.1f} EXTREME")
            elif vix>MAX_VIX: r["trade_today"]=False; r["skip_reason"]=f"VIX={vix:.1f} above max {MAX_VIX}"; r["reasons"].append(f"VIX too high")
            elif vix<15: r["confidence"]=min(r["confidence"]+10,100); r["reasons"].append(f"VIX={vix:.1f} ideal")
            else: r["reasons"].append(f"VIX={vix:.1f} OK")
    except Exception as e: log.warning(f"VIX: {e}")

def _check_spy(r):
    try:
        bars=api.get_bars("SPY","1Day",limit=2).df
        if len(bars)>=2:
            chg=(float(bars["close"].iloc[-1])-float(bars["close"].iloc[-2]))/float(bars["close"].iloc[-2])
            r["spy_change"]=round(chg*100,2)
            if chg<SPY_DOWN_LIMIT: r["trade_today"]=False; r["skip_reason"]=f"SPY down {chg*100:.1f}%"; r["reasons"].append(f"SPY={chg*100:.1f}% defensive")
            else: r["reasons"].append(f"SPY={chg*100:.1f}% OK")
    except Exception as e: log.warning(f"SPY: {e}")

def _final_verdict(r):
    if not r["trade_today"]: return
    if r["confidence"]<40: r["trade_today"]=False; r["skip_reason"]=f"Confidence {r['confidence']}/100 too low"

def compute_rsi(df,p=14):
    d=df["close"].diff(); g=d.clip(lower=0).rolling(p).mean(); l=-d.clip(upper=0).rolling(p).mean()
    return float((100-(100/(1+g/l))).iloc[-1])

def compute_vwap(df):
    return float((df["close"]*df["volume"]).sum()/df["volume"].sum())

def compute_atr(df,p=14):
    try:
        h=df["high"]; lo=df["low"]; c=df["close"]
        tr=pd.concat([h-lo,(h-c.shift()).abs(),(lo-c.shift()).abs()],axis=1).max(axis=1)
        return float(tr.rolling(p).mean().iloc[-1])
    except: return None

def compute_macd(df):
    try:
        e12=df["close"].ewm(span=12,adjust=False).mean(); e26=df["close"].ewm(span=26,adjust=False).mean()
        macd=e12-e26; sig=macd.ewm(span=9,adjust=False).mean(); hist=macd-sig
        return float(macd.iloc[-1]),float(sig.iloc[-1]),float(hist.iloc[-1])
    except: return None,None,None

def compute_bollinger(df,p=20,s=2):
    try:
        sma=df["close"].rolling(p).mean(); std=df["close"].rolling(p).std()
        return float((sma+s*std).iloc[-1]),float(sma.iloc[-1]),float((sma-s*std).iloc[-1])
    except: return None,None,None

def compute_support_resistance(df,n=50):
    try:
        h=df["high"].tail(n); lo=df["low"].tail(n); price=float(df["close"].iloc[-1])
        res=[]; sup=[]
        for i in range(2,len(h)-2):
            if h.iloc[i]>h.iloc[i-1] and h.iloc[i]>h.iloc[i+1]: res.append(float(h.iloc[i]))
            if lo.iloc[i]<lo.iloc[i-1] and lo.iloc[i]<lo.iloc[i+1]: sup.append(float(lo.iloc[i]))
        return round(max([s for s in sup if s<price],default=price*0.98),2), round(min([r for r in res if r>price],default=price*1.02),2)
    except: return None,None

def get_indicators(symbol):
    b5=api.get_bars(symbol,"5Min",limit=250).df; bd=api.get_bars(symbol,"1Day",limit=210).df
    price=float(api.get_latest_trade(symbol).price)
    ml,ms,mh=compute_macd(b5); bu,bm,bl=compute_bollinger(b5); sup,res=compute_support_resistance(b5)
    return {
        "price":        price,
        "rsi":          round(compute_rsi(b5),2),
        "vwap":         round(compute_vwap(b5),2),
        "ema9":         round(float(b5["close"].ewm(span=9,adjust=False).mean().iloc[-1]),2),
        "ema21":        round(float(b5["close"].ewm(span=21,adjust=False).mean().iloc[-1]),2),
        "ema50":        round(float(b5["close"].ewm(span=50,adjust=False).mean().iloc[-1]),2),
        "ema200_daily": round(float(bd["close"].ewm(span=200,adjust=False).mean().iloc[-1]),2),
        "volume_ratio": round(float(b5["volume"].iloc[-1]/b5["volume"].iloc[:-1].mean()),2),
        "atr":          round(compute_atr(b5),4) if compute_atr(b5) else None,
        "macd_line":    round(ml,4) if ml else None,
        "macd_hist":    round(mh,4) if mh else None,
        "bb_upper":     round(bu,2) if bu else None,
        "bb_lower":     round(bl,2) if bl else None,
        "support":      sup, "resistance": res,
    }

def score_trade(data, regime, report):
    score=0; details=[]
    if 50<data["rsi"]<65:                    score+=20; details.append(f"RSI={data['rsi']} ideal +20")
    elif 45<data["rsi"]<=50:                 score+=10; details.append(f"RSI={data['rsi']} borderline +10")
    else:                                               details.append(f"RSI={data['rsi']} fail +0")
    if data["price"]>data["vwap"]:           score+=20; details.append("Above VWAP +20")
    else:                                               details.append("Below VWAP fail +0")
    if data["ema9"]>data["ema21"]:
        score+=15; details.append("EMA9>EMA21 +15")
        if data["ema21"]>data["ema50"]:      score+=5;  details.append("Full EMA align +5")
    else:                                               details.append("EMA9<EMA21 fail +0")
    if data["volume_ratio"]>VOLUME_MIN:      score+=15; details.append(f"Vol={data['volume_ratio']}x +15")
    else:                                               details.append(f"Vol={data['volume_ratio']}x fail +0")
    if data.get("macd_hist") and data["macd_hist"]>0: score+=10; details.append(f"MACD bull +10")
    else:                                               details.append("MACD fail +0")
    if data.get("bb_upper") and data.get("bb_lower"):
        rng=(data["bb_upper"]-data["bb_lower"]); pos=(data["price"]-data["bb_lower"])/rng if rng>0 else 0.5
        if 0.4<pos<0.7: score+=5; details.append(f"BB pos {pos:.2f} +5")
        elif pos>0.9:   score-=5; details.append(f"BB overbought {pos:.2f} -5")
    if EMA200_FILTER:
        if data["price"]>data["ema200_daily"]: score+=10; details.append("Above EMA200 +10")
        else:                                             details.append("Below EMA200 fail +0")
    if regime=="bull":   score+=10; details.append("Bull regime +10")
    elif regime=="bear": score+=5;  details.append("Bear regime +5")
    else:                            details.append("Chop +0")
    return score, details

def calc_sniper_bracket(data, action):
    price=data["price"]; atr=data.get("atr")
    if USE_ATR_STOPS and atr:
        if action=="BUY":
            stop=round(price-atr*ATR_STOP_MULT,2); tp1=round(price+atr*ATR_TP1_MULT,2); tp2=round(price+atr*ATR_TP2_MULT,2)
            if data.get("support") and data["support"]>stop and data["support"]>price*0.97: stop=round(data["support"]*0.998,2)
            if data.get("resistance") and data["resistance"]<tp2: tp1=round(data["resistance"]*0.999,2)
        else:
            stop=round(price+atr*ATR_STOP_MULT,2); tp1=round(price-atr*ATR_TP1_MULT,2); tp2=round(price-atr*ATR_TP2_MULT,2)
        trail=round(atr*1.0,2)
    else:
        if action=="BUY":
            stop=round(price*(1-STOP_LOSS_PCT),2); tp1=round(price*(1+TAKE_PROFIT_PCT),2); tp2=round(price*(1+TAKE_PROFIT_PCT*1.5),2)
        else:
            stop=round(price*(1+STOP_LOSS_PCT),2); tp1=round(price*(1-TAKE_PROFIT_PCT),2); tp2=round(price*(1-TAKE_PROFIT_PCT*1.5),2)
        trail=round(price*0.01,2)
    risk=abs(price-stop); reward=abs(tp1-price); rr=round(reward/risk,2) if risk>0 else 0
    return stop,tp1,tp2,trail,rr

def register_trailing_stop(oid, symbol, direction, price, offset):
    _trailing_stops[oid]={"symbol":symbol,"direction":direction,
        "stop":price-offset if direction=="buy" else price+offset,"best":price,"offset":offset}

def update_all_trailing_stops():
    for oid,ts in list(_trailing_stops.items()):
        try:
            price=float(api.get_latest_trade(ts["symbol"]).price)
            if ts["direction"]=="buy" and price>ts["best"]:
                ts["best"]=price; ts["stop"]=round(price-ts["offset"],2)
            elif ts["direction"]=="sell" and price<ts["best"]:
                ts["best"]=price; ts["stop"]=round(price+ts["offset"],2)
        except: pass

def check_trail_exits():
    exits=[]
    for oid,ts in list(_trailing_stops.items()):
        try:
            price=float(api.get_latest_trade(ts["symbol"]).price)
            hit=(ts["direction"]=="buy" and price<=ts["stop"]) or (ts["direction"]=="sell" and price>=ts["stop"])
            if hit:
                log.info(f"TRAIL EXIT: {ts['symbol']} price={price} stop={ts['stop']}")
                close_position(ts["symbol"]); del _trailing_stops[oid]; _open_snipes.pop(oid,None); exits.append(ts["symbol"])
        except: pass
    return exits

def kill_switch():
    try:
        acc=api.get_account(); pnl=float(acc.equity)-float(acc.last_equity)
        if pnl/float(acc.equity)<MAX_DAILY_LOSS: log.warning(f"KILL SWITCH P&L={pnl:.2f}"); return True,pnl
        return False,pnl
    except: return False,0

def position_size(price, equity):
    if equity>50000: risk=0.02
    elif equity>20000: risk=0.015
    else: risk=RISK_PCT
    return max(int((equity*risk)/price),1)

def execute_trade(symbol, signal_action):
    symbol=symbol.upper()
    killed,pnl=kill_switch()
    if killed: return {"status":"stopped","reason":f"Kill switch P&L={pnl:.2f}"}
    if not trading_allowed(): return {"status":"skipped","reason":f"Outside window 9:30-{STOP_HOUR}:{STOP_MIN:02d} PDT"}
    if too_many(symbol): return {"status":"skipped","reason":f"Max {MAX_TRADES} trades for {symbol}"}
    if too_soon(symbol): return {"status":"skipped","reason":"15min cooldown"}
    report=get_market_report()
    if not report["trade_today"]: return {"status":"skipped","reason":f"Market: {report['skip_reason']}"}
    regime=detect_regime(); action,reason=auto_direction(signal_action,regime)
    if action is None: return {"status":"skipped","reason":reason}
    try: data=get_indicators(symbol)
    except Exception as e: return {"status":"error","reason":f"Indicators failed: {e}"}
    score,details=score_trade(data,regime,report)
    if score<MIN_SCORE: return {"status":"skipped","reason":f"Score {score}/{MIN_SCORE}","score":score,"details":details}
    try:
        pos=api.get_position(symbol)
        if int(pos.qty)>0 and action=="BUY": return {"status":"skipped","reason":f"Already holding {pos.qty} shares"}
    except: pass
    update_all_trailing_stops(); check_trail_exits()
    equity=float(api.get_account().equity); price=data["price"]; qty=position_size(price,equity)
    stop,tp1,tp2,trail,rr=calc_sniper_bracket(data,action)
    if rr<MIN_RR: return {"status":"skipped","reason":f"R/R={rr:.2f} below min {MIN_RR}","rr":rr}
    side="buy" if action=="BUY" else "sell"
    try:
        order=api.submit_order(symbol=symbol,qty=qty,side=side,type="market",time_in_force="day",
            order_class="bracket",stop_loss={"stop_price":stop},take_profit={"limit_price":tp1})
        log_trade(symbol)
        register_trailing_stop(order.id,symbol,side,price,trail)
        _open_snipes[order.id]={"symbol":symbol,"qty":qty,"entry":price,"tp2":tp2,"trail_offset":trail,"score":score}
        log.info(f"SNIPER {action} {symbol} qty={qty} price={price} stop={stop} tp1={tp1} tp2={tp2} score={score} R/R={rr}")
        return {"status":"filled","type":f"SNIPER_{action}","symbol":symbol,"signal":signal_action,
                "regime":regime,"regime_reason":reason,"qty":qty,"price":price,"stop":stop,
                "take_profit_1":tp1,"take_profit_2":tp2,"trail_offset":trail,"risk_reward":rr,
                "score":f"{score}/100","score_details":details,"indicators":data,
                "market_bias":report["market_bias"],"confidence":report["confidence"],"order_id":order.id}
    except Exception as e: log.error(f"Order failed: {e}"); return {"status":"error","reason":str(e)}

def close_position(symbol):
    try: qty=int(api.get_position(symbol).qty)
    except: return {"status":"skipped","reason":f"No position in {symbol}"}
    for o in api.list_orders(status="open"):
        if o.symbol==symbol: api.cancel_order(o.id)
    order=api.submit_order(symbol=symbol,qty=qty,side="sell",type="market",time_in_force="day")
    for oid in [k for k,v in _trailing_stops.items() if v["symbol"]==symbol]: del _trailing_stops[oid]
    for oid in [k for k,v in _open_snipes.items() if v["symbol"]==symbol]: del _open_snipes[oid]
    return {"status":"submitted","symbol":symbol,"qty":qty,"order_id":order.id}

@app.route("/webhook", methods=["POST"])
def webhook():
    data=request.get_json(force=True); symbol=str(data.get("ticker","")).upper().strip(); action=str(data.get("action","")).upper().strip()
    if symbol not in ALLOWED_TICKERS: return jsonify({"status":"skipped","reason":f"{symbol} not allowed"})
    result=close_position(symbol) if action=="SELL" else execute_trade(symbol,action) if action=="BUY" else None
    if result is None: return jsonify({"error":f"Unknown action: {action}"}),400
    return jsonify(result)

@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    try: event=stripe.Webhook.construct_event(request.data,request.headers.get("Stripe-Signature",""),STRIPE_WEBHOOK_SECRET)
    except Exception as e: return jsonify({"error":str(e)}),400
    etype=event["type"]; data=event["data"]["object"]
    if etype=="customer.subscription.created": log.info(f"NEW SUB: {data.get('customer')}")
    elif etype=="invoice.payment_succeeded":   log.info(f"PAID: ${data.get('amount_paid',0)/100:.2f}")
    elif etype=="invoice.payment_failed":      log.warning(f"FAILED: {data.get('customer')}")
    elif etype=="customer.subscription.deleted": log.info(f"CANCELLED: {data.get('customer')}")
    return jsonify({"status":"ok"})

@app.route("/", methods=["GET"])
def home():
    report=get_market_report(); regime=detect_regime(); killed,pnl=kill_switch()
    update_all_trailing_stops(); check_trail_exits()
    positions=[]
    try:
        for p in api.list_positions():
            positions.append({"symbol":p.symbol,"qty":p.qty,"entry":p.avg_entry_price,"current":p.current_price,"pnl":p.unrealized_pl})
    except: pass
    return jsonify({"status":"ok","service":"ApexTrade Super Bot v7.0 — Full Sniper Edition",
        "time_pdt":pdt_now().strftime("%I:%M %p PDT"),"trading_allowed":trading_allowed() and not killed,
        "kill_switch":killed,"daily_pnl":round(pnl,2),"regime":regime,
        "market":{"trade_today":report["trade_today"],"skip_reason":report["skip_reason"],
            "bias":report["market_bias"],"confidence":report["confidence"],"vix":report["vix"],
            "spy_change":report.get("spy_change"),"best_window":report["best_entry_window"],"reasons":report["reasons"]},
        "settings":{"risk_pct":f"{RISK_PCT*100:.1f}%","stop_loss":f"{STOP_LOSS_PCT*100:.1f}%",
            "take_profit":f"{TAKE_PROFIT_PCT*100:.1f}%","max_trades":MAX_TRADES,"min_score":f"{MIN_SCORE}/100",
            "max_vix":MAX_VIX,"volume_min":f"{VOLUME_MIN}x","sniper_window":f"9:30-{STOP_HOUR}:{STOP_MIN:02d} PDT",
            "ema200_filter":EMA200_FILTER,"atr_stops":USE_ATR_STOPS,"atr_mults":f"{ATR_STOP_MULT}x stop / {ATR_TP1_MULT}x tp1 / {ATR_TP2_MULT}x tp2",
            "min_rr":MIN_RR,"auto_direction":"enabled","trailing_stops":"enabled","partial_profits":"50% TP1 / 50% TP2"},
        "active_snipes":len(_open_snipes),"trailing_stops":len(_trailing_stops),
        "trades_today":{k:len(v) for k,v in trade_log.items()},"open_positions":positions})

@app.route("/market",  methods=["GET"])
def market(): return jsonify(get_market_report(request.args.get("refresh","false").lower()=="true"))

@app.route("/regime",  methods=["GET"])
def regime_check(): return jsonify({"regime":detect_regime(),"time":pdt_now().strftime("%I:%M %p PDT")})

@app.route("/analyze/<symbol>", methods=["GET"])
def analyze(symbol):
    sym=symbol.upper(); report=get_market_report(); regime=detect_regime()
    try: data=get_indicators(sym)
    except Exception as e: return jsonify({"error":str(e)}),500
    score,details=score_trade(data,regime,report); action,_=auto_direction("BUY",regime)
    stop,tp1,tp2,trail,rr=calc_sniper_bracket(data,action or "BUY")
    return jsonify({"symbol":sym,"would_trade":score>=MIN_SCORE and report["trade_today"] and rr>=MIN_RR,
        "score":f"{score}/100","min_score":MIN_SCORE,"direction":action,"regime":regime,"risk_reward":rr,"min_rr":MIN_RR,
        "levels":{"stop":stop,"tp1":tp1,"tp2":tp2,"trail_offset":trail},
        "indicators":data,"details":details,"market_bias":report["market_bias"],"confidence":report["confidence"]})

@app.route("/snipes",  methods=["GET"])
def snipes():
    update_all_trailing_stops(); result=[]
    for oid,s in _open_snipes.items():
        try:
            price=float(api.get_latest_trade(s["symbol"]).price); trail=_trailing_stops.get(oid,{}).get("stop")
            result.append({**s,"current_price":price,"trail_stop":trail,"pnl":round((price-s["entry"])*s["qty"],2),"order_id":oid})
        except: result.append({"order_id":oid,"error":"fetch failed"})
    return jsonify({"active_snipes":result,"count":len(result)})

@app.route("/sentiment", methods=["GET"])
def sentiment():
    r=get_market_report()
    return jsonify({"sentiment":r["market_bias"],"confidence":r["confidence"],"trade_today":r["trade_today"],
        "vix":r["vix"],"spy_change":r.get("spy_change"),"headlines":r["top_headlines"][:5]})

@app.route("/close-all", methods=["POST"])
def close_all():
    api.cancel_all_orders(); api.close_all_positions(); _trailing_stops.clear(); _open_snipes.clear()
    return jsonify({"status":"ok","message":"All positions and snipes closed"})

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8080)),debug=False)
