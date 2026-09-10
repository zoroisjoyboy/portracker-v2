"""
prices.py
Fetches live prices, index data, and earnings/dividend events
exclusively via Finnhub API.

Finnhub free tier: 60 requests/minute

Environment variables required:
    FINNHUB_API_KEY
"""

import os
import time
from datetime import datetime, timedelta, date

import requests
from sqlalchemy.orm import Session

from models import LivePrice, Holding, Security, Account

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
FINNHUB_BASE    = "https://finnhub.io/api/v1"

INDICES = {
    "SPY": "SPY",   # S&P 500
    "DOW": "DIA",   # Dow Jones
    "NDQ": "QQQ",   # NASDAQ
}

REQUEST_DELAY = 1 # seconds between requests 


def _get(endpoint: str, params: dict = {}) -> dict:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY environment variable not set")
    params["token"] = FINNHUB_API_KEY
    url = f"{FINNHUB_BASE}/{endpoint}"
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 429:
                wait = 2 ** attempt * 5
                print(f"  Finnhub 429 on {endpoint}, waiting {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                time.sleep(2 ** attempt * 2)
            else:
                print(f"  Finnhub request failed: {endpoint} — {e}")
                return {}
    return {}


def fetch_quote(ticker: str) -> dict | None:
    data = _get("quote", {"symbol": ticker})
    if not data or data.get("c") is None or data.get("c") == 0:
        return None
    price      = round(float(data["c"]), 4)
    prev_close = round(float(data["pc"]), 4)
    daily_pct  = round((price - prev_close) / prev_close * 100, 4) if prev_close else 0.0
    return {"price": price, "prev_close": prev_close, "daily_pct": daily_pct}


def fetch_candles(ticker: str, days: int) -> list[dict]:
    now     = int(datetime.now().timestamp())
    from_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    data = _get("stock/candle", {
        "symbol": ticker, "resolution": "D",
        "from": from_ts, "to": now,
    })
    if not data or data.get("s") != "ok":
        return []
    return [
        {"date": date.fromtimestamp(t), "close": round(float(c), 4)}
        for t, c in zip(data.get("t", []), data.get("c", []))
    ]


def fetch_extended(ticker: str, price: float) -> tuple[float | None, float | None]:
    candles = fetch_candles(ticker, days=370)
    if not candles:
        return None, None
    today     = date.today()
    ytd_start = date(today.year, 1, 1)
    month_ago = today - timedelta(days=30)

    ytd_candles = [c for c in candles if c["date"] >= ytd_start]
    chg_ytd = None
    if ytd_candles:
        base = ytd_candles[0]["close"]
        chg_ytd = round((price - base) / base * 100, 2) if base else None

    candles_30d = [c for c in candles if c["date"] >= month_ago]
    chg_30d = None
    if candles_30d:
        base = candles_30d[0]["close"]
        chg_30d = round((price - base) / base * 100, 2) if base else None

    return chg_30d, chg_ytd


def _upsert_price(db: Session, ticker: str, price: float, prev_close: float,
                  daily_pct: float, chg_30d: float | None, chg_ytd: float | None):
    row = db.query(LivePrice).filter_by(ticker=ticker).first()
    if not row:
        row = LivePrice(ticker=ticker)
        db.add(row)
    row.price          = price
    row.prev_close     = prev_close
    row.daily_pct      = daily_pct
    row.change_pct_30d = chg_30d
    row.change_pct_ytd = chg_ytd
    row.fetched_at     = datetime.utcnow()


def refresh_prices(db: Session) -> dict:
    held_tickers: set[str] = set()
    rows = (
        db.query(Security.ticker_symbol)
        .join(Holding, Holding.security_id == Security.id)
        .filter(Security.is_cash_equivalent == False)
        .filter(Security.ticker_symbol.isnot(None))
        .distinct()
        .all()
    )
    for (ticker,) in rows:
        held_tickers.add(ticker)

    index_tickers = set(INDICES.values())
    all_tickers   = list(held_tickers | index_tickers)
    updated       = 0

    print(f"Refreshing {len(all_tickers)} tickers via Finnhub...")

    for ticker in all_tickers:
        quote = fetch_quote(ticker)
        if not quote:
            print(f"  No quote for {ticker} — skipping")
            continue
        chg_30d = chg_ytd = None
        if ticker in index_tickers:
            chg_30d, chg_ytd = fetch_extended(ticker, quote["price"])
        _upsert_price(db, ticker, quote["price"], quote["prev_close"],
                      quote["daily_pct"], chg_30d, chg_ytd)
        updated += 1

    print(f"Finnhub refresh complete — {updated}/{len(all_tickers)} tickers updated")
    return {"updated": updated, "tickers": all_tickers}


def get_portfolio_value(db: Session, slug: str) -> dict:
    account = db.query(Account).filter_by(slug=slug, is_active=True).first()
    if not account:
        return {"total_value": None, "daily_gain": None, "daily_pct": None, "positions": []}

    total_value = total_prev_value = 0.0
    positions = []

    for holding in account.holdings:
        sec    = holding.security
        ticker = sec.ticker_symbol
        if sec.is_cash_equivalent or not ticker:
            cv = holding.institution_value or 0.0
            total_value      += cv
            total_prev_value += cv
            positions.append({
                "symbol": ticker or "CASH", "quantity": holding.quantity,
                "price": None, "value": cv,
                "daily_pct": 0.0, "daily_gain": 0.0, "is_cash": True,
            })
            continue
        lp = db.query(LivePrice).filter_by(ticker=ticker).first()
        if not lp or lp.price is None:
            continue
        value      = round(lp.price * holding.quantity, 2)
        prev_value = round(lp.prev_close * holding.quantity, 2) if lp.prev_close else value
        daily_gain = round(value - prev_value, 2)
        total_value      += value
        total_prev_value += prev_value
        positions.append({
            "symbol": ticker, "quantity": holding.quantity, "price": lp.price,
            "value": value, "daily_pct": lp.daily_pct, "daily_gain": daily_gain,
            "is_cash": False,
        })

    total_value      = round(total_value, 2)
    total_daily_gain = round(total_value - total_prev_value, 2)
    daily_pct        = round(
        (total_daily_gain / total_prev_value * 100) if total_prev_value else 0.0, 4
    )
    return {
        "total_value": total_value, "daily_gain": total_daily_gain,
        "daily_pct": daily_pct, "positions": positions,
    }


def get_indices(db: Session) -> list[dict]:
    result = []
    for label, etf_ticker in INDICES.items():
        lp = db.query(LivePrice).filter_by(ticker=etf_ticker).first()
        result.append({
            "symbol":         label,
            "price":          lp.price          if lp else None,
            "change_pct":     lp.daily_pct      if lp else None,
            "change_pct_30d": lp.change_pct_30d if lp else None,
            "change_pct_ytd": lp.change_pct_ytd if lp else None,
        })
    return result


def get_upcoming_events(db: Session, slugs: list[str]) -> list[dict]:
    tickers: set[str] = set()
    for slug in slugs:
        acct = db.query(Account).filter_by(slug=slug, is_active=True).first()
        if not acct:
            continue
        for h in acct.holdings:
            if h.security.ticker_symbol and not h.security.is_cash_equivalent:
                tickers.add(h.security.ticker_symbol)

    events  = []
    today   = date.today()
    to_date = today + timedelta(days=90)

    for sym in tickers:
        try:
            data = _get("calendar/earnings", {
                "symbol": sym,
                "from":   today.strftime("%Y-%m-%d"),
                "to":     to_date.strftime("%Y-%m-%d"),
            })
            for e in data.get("earningsCalendar", []):
                earn_date = datetime.strptime(e["date"], "%Y-%m-%d").date()
                if earn_date >= today:
                    events.append({
                        "symbol": sym, "kind": "ERN",
                        "date": earn_date,
                        "detail": earn_date.strftime("%-m/%-d"),
                    })
        except Exception:
            pass

        try:
            data = _get("stock/dividend2", {"symbol": sym})
            for d in data.get("data", []):
                ex_date = d.get("exDate")
                amount  = d.get("amount")
                if not ex_date or not amount:
                    continue
                div_date = datetime.strptime(ex_date, "%Y-%m-%d").date()
                if div_date >= today:
                    events.append({
                        "symbol": sym, "kind": "DIV",
                        "date": div_date,
                        "detail": f"${float(amount):.2f}",
                    })
        except Exception:
            pass

    events.sort(key=lambda e: e["date"])
    return events[:8]