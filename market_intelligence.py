ApexTrade Market Intelligence Module
Searches the web every morning to determine if it's safe to trade today.
Checks: futures, VIX, news sentiment, sector strength, economic calendar.
"""
import requests, logging, os
from datetime import datetime, date
from bs4 import BeautifulSoup
import pytz

log = logging.getLogger(__name__)

# ── Cache so we don't hammer news sites on every signal ──────────────────────
_cache = {"date": None, "report": None}

def get_market_report(force=False) -> dict:
    """
    Master function — returns a full market intelligence report.
    Cached once per day. Pass force=True to refresh.
    """
    pdt   = pytz.timezone("America/Los_Angeles")
    today = datetime.now(pdt).date()

    if not force and _cache["date"] == today and _cache["report"]:
        return _cache["report"]

    log.info("🔍 Running daily market intelligence scan...")

    report = {
        "date":             today.isoformat(),
        "trade_today":      True,   # default allow, filters will block
        "skip_reason":      None,
        "market_bias":      "neutral",
        "vix":              None,
        "futures_sp500":    None,
        "futures_nasdaq":   None,
        "oil_change_pct":   None,
        "top_headlines":    [],
        "sector_sentiment": {},
        "best_entry_window": "09:45-11:00 AM PDT",
        "avoid_tickers":    [],
        "confidence":       0,      # 0-100
        "reasons":          [],
    }

    # Run all checks
    _check_futures(report)
    _check_vix(report)
    _check_oil(report)
    _check_news_headlines(report)
    _check_economic_calendar(report)
    _determine_verdict(report)

    _cache["date"]   = today
    _cache["report"] = report

    log.info(f"📊 Market report: trade={report['trade_today']} bias={report['market_bias']} confidence={report['confidence']}")
    return report


def _check_futures(report):
    """Fetch S&P 500 and Nasdaq futures from Yahoo Finance."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get("https://finance.yahoo.com/", headers=headers, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")

        text = soup.get_text()
        import re

        # Look for S&P futures change
        sp_match = re.search(r'S&P.*?([+-]?\d+\.\d+)%', text)
        nq_match = re.search(r'Nasdaq.*?([+-]?\d+\.\d+)%', text)

        if sp_match:
            report["futures_sp500"] = float(sp_match.group(1))
        if nq_match:
            report["futures_nasdaq"] = float(nq_match.group(1))

    except Exception as e:
        log.warning(f"Futures fetch failed: {e}")

    # Apply rules
    sp  = report.get("futures_sp500")
    nq  = report.get("futures_nasdaq")

    if sp and sp < -1.5:
        report["reasons"].append(f"S&P futures down {sp:.1f}% — major selloff")
        report["market_bias"] = "bearish"
        report["confidence"]  = max(report["confidence"] - 30, 0)
    elif sp and sp < -0.5:
        report["reasons"].append(f"S&P futures down {sp:.1f}% — caution")
        report["confidence"] = max(report["confidence"] - 15, 0)
    elif sp and sp > 0.5:
        report["reasons"].append(f"S&P futures up {sp:.1f}% — bullish open expected")
        report["confidence"] = min(report["confidence"] + 20, 100)

    if nq and nq < -1.5:
        report["reasons"].append(f"Nasdaq futures down {nq:.1f}% — tech selloff")
        report["avoid_tickers"].extend(["NVDA", "AAPL", "AMZN"])


def _check_vix(report):
    """Fetch VIX from Yahoo Finance — high VIX = high fear = don't trade."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get("https://finance.yahoo.com/quote/%5EVIX/", headers=headers, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        # Look for VIX price
        import re
        text = soup.get_text()
        match = re.search(r'VIX.*?(\d{2,3}\.\d{2})', text)
        if match:
            report["vix"] = float(match.group(1))
    except Exception as e:
        log.warning(f"VIX fetch failed: {e}")

    vix = report.get("vix")
    if vix:
        if vix > 35:
            report["reasons"].append(f"VIX={vix:.1f} — EXTREME FEAR, no trades")
            report["trade_today"]  = False
            report["skip_reason"]  = f"VIX={vix:.1f} extreme fear — all trading halted"
            report["market_bias"]  = "bearish"
        elif vix > 25:
            report["reasons"].append(f"VIX={vix:.1f} — elevated fear, reduce size")
            report["confidence"]   = max(report["confidence"] - 25, 0)
            report["market_bias"]  = "bearish"
        elif vix > 20:
            report["reasons"].append(f"VIX={vix:.1f} — moderate fear, be cautious")
            report["confidence"]   = max(report["confidence"] - 10, 0)
        elif vix < 15:
            report["reasons"].append(f"VIX={vix:.1f} — low fear, good conditions")
            report["confidence"]   = min(report["confidence"] + 15, 100)


def _check_oil(report):
    """Oil surges hurt tech stocks — check crude oil price change."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get("https://finance.yahoo.com/quote/CL=F/", headers=headers, timeout=8)
        text = BeautifulSoup(r.text, "html.parser").get_text()
        import re
        match = re.search(r'([+-]?\d+\.\d+)%', text)
        if match:
            report["oil_change_pct"] = float(match.group(1))
    except Exception as e:
        log.warning(f"Oil fetch failed: {e}")

    oil = report.get("oil_change_pct")
    if oil:
        if oil > 5:
            report["reasons"].append(f"Oil surging +{oil:.1f}% — hurts tech, inflation fears")
            report["avoid_tickers"].extend(["TSLA", "AMZN"])
            report["confidence"] = max(report["confidence"] - 20, 0)
        elif oil > 3:
            report["reasons"].append(f"Oil up +{oil:.1f}% — mild headwind for tech")
            report["confidence"] = max(report["confidence"] - 10, 0)
        elif oil < -3:
            report["reasons"].append(f"Oil down {oil:.1f}% — bullish for tech/consumer")
            report["confidence"] = min(report["confidence"] + 10, 100)


def _check_news_headlines(report):
    """
    Scan multiple news sources for market-moving keywords.
    Sources: Finviz, MarketWatch, Reuters headlines.
    """
    bull_words = ["rally","surge","gain","rise","bull","strong","beats","record",
                  "growth","upgrade","breakout","recovery","optimism","ceasefire","peace"]
    bear_words = ["fall","drop","decline","crash","bear","weak","miss","recession",
                  "tariff","war","inflation","layoff","bankruptcy","downgrade","escalation","sanctions"]
    headlines  = []

    # Finviz
    try:
        r = requests.get("https://finviz.com/news.ashx",
                         headers={"User-Agent":"Mozilla/5.0"}, timeout=5)
        soup = BeautifulSoup(r.text, "html.parser")
        headlines += [a.text for a in soup.find_all("a", class_="tab-link")][:20]
    except Exception as e:
        log.warning(f"Finviz failed: {e}")

    # MarketWatch
    try:
        r = requests.get("https://www.marketwatch.com/latest-news",
                         headers={"User-Agent":"Mozilla/5.0"}, timeout=5)
        soup = BeautifulSoup(r.text, "html.parser")
        headlines += [a.text.strip() for a in soup.find_all("a", class_="link") if len(a.text.strip()) > 20][:10]
    except Exception as e:
        log.warning(f"MarketWatch failed: {e}")

    report["top_headlines"] = headlines[:10]

    lower = [h.lower() for h in headlines]
    bull  = sum(1 for h in lower for w in bull_words if w in h)
    bear  = sum(1 for h in lower for w in bear_words if w in h)

    log.info(f"Headlines: bull={bull} bear={bear} total={len(headlines)}")

    if bear > bull + 5:
        report["market_bias"] = "bearish"
        report["reasons"].append(f"News heavily bearish ({bear} bear vs {bull} bull signals)")
        report["confidence"]  = max(report["confidence"] - 20, 0)
    elif bull > bear + 5:
        report["market_bias"] = "bullish"
        report["reasons"].append(f"News strongly bullish ({bull} bull vs {bear} bear signals)")
        report["confidence"]  = min(report["confidence"] + 15, 100)
    else:
        report["reasons"].append(f"News mixed (bull={bull} bear={bear})")

    # Check for specific high-risk keywords
    crisis_words = ["war escalat","iran attack","market crash","circuit breaker",
                    "trading halt","emergency","nuclear","major attack"]
    for h in lower:
        for w in crisis_words:
            if w in h:
                report["trade_today"] = False
                report["skip_reason"] = f"Crisis headline detected: '{w}'"
                report["market_bias"] = "bearish"
                report["reasons"].append(f"⚠️ Crisis keyword: '{w}'")
                break


def _check_economic_calendar(report):
    """
    Flag high-impact economic events that cause volatility.
    FOMC, CPI, NFP, etc. — avoid trading on these days.
    """
    try:
        r = requests.get("https://finance.yahoo.com/calendar/economic/",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        text = BeautifulSoup(r.text, "html.parser").get_text().lower()
        high_impact = ["federal reserve","fomc","interest rate decision",
                       "cpi","consumer price","nonfarm payroll","gdp"]
        for event in high_impact:
            if event in text:
                report["reasons"].append(f"⚠️ High-impact event today: {event}")
                report["confidence"] = max(report["confidence"] - 15, 0)
                break
    except Exception as e:
        log.warning(f"Economic calendar failed: {e}")


def _determine_verdict(report):
    """Final decision — trade or skip today based on all signals."""
    # Start with base confidence of 50
    if report["confidence"] == 0:
        report["confidence"] = 50

    bias = report["market_bias"]

    # Hard stops
    if not report["trade_today"]:
        return

    # Confidence-based decision
    if report["confidence"] < 25:
        report["trade_today"] = False
        report["skip_reason"] = f"Confidence too low ({report['confidence']}/100) — too risky"
    elif report["confidence"] < 40:
        report["market_bias"] = "bearish"
        report["reasons"].append("Low confidence — only best setups")
    elif report["confidence"] >= 60:
        report["market_bias"] = "bullish"

    # Best entry window based on market conditions
    if bias == "bullish":
        report["best_entry_window"] = "09:35-10:30 AM PDT (momentum open)"
    elif bias == "bearish":
        report["best_entry_window"] = "10:30-11:30 AM PDT (wait for stabilization)"
    else:
        report["best_entry_window"] = "09:45-11:00 AM PDT (standard window)"

    # Deduplicate avoid_tickers
    report["avoid_tickers"] = list(set(report["avoid_tickers"]))

