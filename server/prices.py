"""
prices.py
Fetches live prices and index data from yfinance, writes to live_prices table.
Also fetches earnings/dividend metadata for held tickers.

Replaces the old refresh_prices.py + fetch_indices.py scripts.
Called every 5 minutes during market hours by the Pi-side cron,
or triggered via GET /prices/refresh on the server.
"""

from datetime import datetime, timedelta, date
import time

import yfinance as yf
from sqlalchemy.orm import Session

from models import LivePrice, Holding, Security, Account

INDICES = {
    "SPY": "SPY",
    "DOW": "^DJI",
    "NDQ": "^IXIC",
}


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

def fetch_with_retry(sym: str, retries: int = 3):
    for attempt in range(retries):
        try:
            return yf.Ticker(sym).fast_info
        except Exception as e:
            if "429" in str(e) and attempt < retries - 1:
                wait = 2 ** attempt * 2  # 2s, 4s, 8s
                print(f"  429 on {sym}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise

def refresh_prices(db: Session) -> dict:
    # Collect all unique non-cash tickers + indices
    held_tickers = set(INDICES.values())
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

    if not held_tickers:
        return {"updated": 0}

    tickers_list = list(held_tickers)
    updated = 0

    # ── Batch download for current + previous close ───────────────────────────
    try:
        raw = yf.download(
            tickers=" ".join(tickers_list),
            period="2d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )
    except Exception as e:
        print(f"  Batch download failed: {e}")
        raw = None

    # Build price lookup from batch download
    batch_prices: dict[str, dict] = {}
    if raw is not None and not raw.empty:
        for sym in tickers_list:
            try:
                if len(tickers_list) == 1:
                    closes = raw["Close"]
                else:
                    closes = raw["Close"][sym]

                closes = closes.dropna()
                if len(closes) >= 2:
                    price      = round(float(closes.iloc[-1]), 4)
                    prev_close = round(float(closes.iloc[-2]), 4)
                elif len(closes) == 1:
                    price      = round(float(closes.iloc[-1]), 4)
                    prev_close = price
                else:
                    continue

                daily_pct = round((price - prev_close) / prev_close * 100, 4) if prev_close else 0.0
                batch_prices[sym] = {
                    "price":      price,
                    "prev_close": prev_close,
                    "daily_pct":  daily_pct,
                }
            except Exception as e:
                print(f"  Batch parse failed for {sym}: {e}")

    # ── Extended data for indices (30d/YTD) with retry ────────────────────────
    for sym in tickers_list:
        pd = batch_prices.get(sym)
        if pd is None:
            # Fallback to fast_info with retry for missing tickers
            try:
                info       = fetch_with_retry(sym)
                price      = round(float(info.last_price), 4)
                prev_close = round(float(info.previous_close), 4)
                daily_pct  = round((price - prev_close) / prev_close * 100, 4) if prev_close else 0.0
                pd = {"price": price, "prev_close": prev_close, "daily_pct": daily_pct}
                batch_prices[sym] = pd
            except Exception as e:
                print(f"  Fallback fetch failed for {sym}: {e}")
                continue

        chg_30d = chg_ytd = None

        # Only fetch extended data for indices
        if sym in INDICES.values():
            try:
                hist = yf.Ticker(sym).history(period="ytd", interval="1d")
                if not hist.empty:
                    price     = pd["price"]
                    ytd_start = float(hist["Close"].iloc[0])
                    chg_ytd   = round((price - ytd_start) / ytd_start * 100, 2)
                    month_ago = hist.index[-1] - timedelta(days=30)
                    hist_30d  = hist[hist.index >= month_ago]
                    if not hist_30d.empty:
                        start_30d = float(hist_30d["Close"].iloc[0])
                        chg_30d   = round((price - start_30d) / start_30d * 100, 2)
            except Exception as e:
                print(f"  Extended data failed for {sym}: {e}")

        _upsert_price(db, sym, pd["price"], pd["prev_close"],
                      pd["daily_pct"], chg_30d, chg_ytd)
        updated += 1

    return {"updated": updated, "tickers": tickers_list}


def get_portfolio_value(db: Session, slug: str) -> dict:
    """
    Compute current total value, daily gain, and daily % for a portfolio slug
    using live_prices table × holdings quantities.

    Returns:
        {total_value, daily_gain, daily_pct, positions: [...]}
    """
    account = db.query(Account).filter_by(slug=slug, is_active=True).first()
    if not account:
        return {"total_value": None, "daily_gain": None, "daily_pct": None, "positions": []}

    total_value      = 0.0
    total_prev_value = 0.0
    positions        = []

    for holding in account.holdings:
        sec    = holding.security
        ticker = sec.ticker_symbol

        if sec.is_cash_equivalent or not ticker:
            # Cash — use institution_value as fixed
            cv = holding.institution_value or 0.0
            total_value      += cv
            total_prev_value += cv
            positions.append({
                "symbol":    ticker or "CASH",
                "quantity":  holding.quantity,
                "price":     None,
                "value":     cv,
                "daily_pct": 0.0,
                "daily_gain": 0.0,
                "is_cash":   True,
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
            "symbol":    ticker,
            "quantity":  holding.quantity,
            "price":     lp.price,
            "value":     value,
            "daily_pct": lp.daily_pct,
            "daily_gain": daily_gain,
            "is_cash":   False,
        })

    total_value      = round(total_value, 2)
    total_daily_gain = round(total_value - total_prev_value, 2)
    daily_pct        = round(
        (total_daily_gain / total_prev_value * 100) if total_prev_value else 0.0, 4
    )

    return {
        "total_value": total_value,
        "daily_gain":  total_daily_gain,
        "daily_pct":   daily_pct,
        "positions":   positions,
    }


def get_indices(db: Session) -> list[dict]:
    """Return current index data from live_prices table."""
    result = []
    for label, sym in INDICES.items():
        lp = db.query(LivePrice).filter_by(ticker=sym).first()
        result.append({
            "symbol":         label,
            "price":          lp.price          if lp else None,
            "change_pct":     lp.daily_pct      if lp else None,
            "change_pct_30d": lp.change_pct_30d if lp else None,
            "change_pct_ytd": lp.change_pct_ytd if lp else None,
        })
    return result


def get_upcoming_events(db: Session, slugs: list[str]) -> list[dict]:
    """
    Return upcoming earnings/dividend events for held tickers.
    Fetches from yfinance — results are not cached in DB (lightweight enough).
    """
    tickers = set()
    for slug in slugs:
        acct = db.query(Account).filter_by(slug=slug, is_active=True).first()
        if not acct:
            continue
        for h in acct.holdings:
            if h.security.ticker_symbol and not h.security.is_cash_equivalent:
                tickers.add(h.security.ticker_symbol)

    events  = []
    today   = date.today()

    for sym in tickers:
        try:
            info = yf.Ticker(sym).info

            # Earnings
            earn_ts = info.get("earningsTimestampEnd")
            if earn_ts:
                earn_date = date.fromtimestamp(earn_ts)
                if earn_date >= today:
                    events.append({
                        "symbol": sym,
                        "kind":   "ERN",
                        "date":   earn_date,
                        "detail": earn_date.strftime("%-m/%-d"),
                    })

            # Dividends
            div_date_ts = info.get("dividendDate")
            div_rate    = info.get("dividendRate")
            if div_date_ts and div_rate:
                div_date = date.fromtimestamp(div_date_ts)
                if div_date >= today:
                    events.append({
                        "symbol": sym,
                        "kind":   "DIV",
                        "date":   div_date,
                        "detail": f"${div_rate:.2f}",
                    })
        except Exception:
            continue

    events.sort(key=lambda e: e["date"])
    return events[:8]