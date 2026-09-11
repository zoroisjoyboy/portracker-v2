"""
main.py
FastAPI application for portracker-v2.

Routes:
    POST /plaid/link-token      — generate link token for browser Link flow
    POST /plaid/exchange        — exchange public token for access token
    POST /plaid/webhook         — receive Plaid webhook notifications
    GET  /plaid/sync            — manually trigger full Plaid sync
    GET  /prices/refresh        — manually trigger price refresh
    GET  /display/image         — returns rendered 800x480 PNG for the Pi
    GET  /display/image/daily   — force daily mode
    GET  /display/image/monthly — force monthly mode
    GET  /display/image/ytd     — force ytd mode
    POST /admin/import-csv      — one-time CSV import
"""

import io
import os
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

import plaid_client as pc
from db import get_db_dep, init_db
from models import PlaidItem, Account
from sync import sync_all, sync_item
from prices import refresh_prices, get_portfolio_value, get_indices, get_upcoming_events
from history import log_all_snapshots, load_history, import_from_csv
from renderer import render_display

app = FastAPI(title="Portracker v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten for production
    allow_methods=["*"],
    allow_headers=["*"],
)

DISPLAY_MODE_SCHEDULE = os.environ.get("DISPLAY_MODE", "daily")  # overridden per route
PORTFOLIOS = [
    slug.strip()
    for slug in os.getenv("PORTFOLIOS", "").split(",")
    if slug.strip()
]

@app.on_event("startup")
def startup():
    init_db()


# ── Plaid Link flow ───────────────────────────────────────────────────────────

class ExchangeRequest(BaseModel):
    public_token: str


@app.post("/plaid/link-token")
def create_link_token():
    """Step 1: browser requests a link token to open Plaid Link."""
    try:
        token = pc.create_link_token()
        return {"link_token": token}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/plaid/exchange")
def exchange_token(body: ExchangeRequest, db: Session = Depends(get_db_dep)):
    """
    Step 2: browser sends public_token after user completes Plaid Link.
    Exchanges it for a permanent access_token and stores in DB.
    """
    try:
        result = pc.exchange_public_token(body.public_token)
        info   = pc.get_item_info(result["access_token"])

        # Check if item already exists
        existing = db.query(PlaidItem).filter_by(item_id=info["item_id"]).first()
        if existing:
            existing.access_token = result["access_token"]
            db.commit()
            return {"status": "updated", "item_id": info["item_id"]}

        item = PlaidItem(
            item_id          =info["item_id"],
            access_token     =result["access_token"],
            institution_id   =info["institution_id"],
            institution_name =info["institution_name"],
        )
        db.add(item)
        db.commit()

        # Trigger initial sync in background
        return {"status": "linked", "item_id": info["item_id"],
                "institution": info["institution_name"]}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/plaid/webhook")
async def plaid_webhook(payload: dict, background: BackgroundTasks,
                        db: Session = Depends(get_db_dep)):
    """
    Plaid calls this when holdings change.
    Triggers a background sync so we don't make Plaid wait.
    """
    webhook_type = payload.get("webhook_type")
    webhook_code = payload.get("webhook_code")

    if webhook_type == "INVESTMENTS" and webhook_code in (
        "DEFAULT_UPDATE", "HOLDINGS_DEFAULT_UPDATE"
    ):
        item_id = payload.get("item_id")
        item    = db.query(PlaidItem).filter_by(item_id=item_id).first()
        if item:
            background.add_task(_background_sync, item.id)

    return {"status": "ok"}


async def _background_sync(item_db_id: int):
    from db import get_db
    with get_db() as db:
        item = db.query(PlaidItem).get(item_db_id)
        if item:
            sync_item(db, item)


# ── Manual sync + price refresh ───────────────────────────────────────────────

@app.get("/plaid/sync")
def manual_sync(db: Session = Depends(get_db_dep)):
    from sync import sync_all, sync_item
    from models import PlaidItem
    items = db.query(PlaidItem).all()
    results = []
    for item in items:
        try:
            summary = sync_item(db, item)
            db.commit()
            results.append({"item_id": item.item_id, "status": "ok", **summary})
        except Exception as e:
            import traceback
            results.append({
                "item_id": item.item_id,
                "status": "error",
                "error": str(e),
                "trace": traceback.format_exc()
            })
    return {"results": results}


@app.get("/prices/refresh")
def manual_price_refresh(db: Session = Depends(get_db_dep)):
    """Manually trigger yfinance price refresh."""
    result = refresh_prices(db)

    slugs     = PORTFOLIOS
    pf_values = {slug: get_portfolio_value(db, slug) for slug in slugs}
    log_all_snapshots(db, pf_values)

    return {"prices": result, "portfolios": {
        slug: {"total_value": pf["total_value"], "daily_pct": pf["daily_pct"]}
        for slug, pf in pf_values.items()
    }, "slugs_used": slugs}


# ── Display image ─────────────────────────────────────────────────────────────

def _render_mode(mode: str, db: Session) -> bytes:
    slugs      = PORTFOLIOS
    portfolios = {slug: get_portfolio_value(db, slug) for slug in slugs}
    indices    = get_indices(mode)
    history    = load_history(db, mode)
    events     = get_upcoming_events(db, slugs)  # now a dict

    img = render_display(portfolios, indices, history, events, mode)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


@app.get("/display/image")
def display_image(mode: str = Query(default="daily"), db: Session = Depends(get_db_dep)):
    """
    Returns the rendered 800x480 PNG for the Pi to push to e-paper.
    Pi calls this: GET /display/image?mode=daily
    """
    buf = _render_mode(mode, db)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/display/image/{mode}")
def display_image_mode(mode: str, db: Session = Depends(get_db_dep)):
    if mode not in ("daily", "monthly", "ytd"):
        raise HTTPException(status_code=400, detail="mode must be daily, monthly, or ytd")
    try:
        buf = _render_mode(mode, db)
        return StreamingResponse(buf, media_type="image/png")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Admin ──────────────────────────────────────────────────────────────────────

class CsvImportRequest(BaseModel):
    csv_path: str


@app.post("/admin/import-csv")
def import_csv(body: CsvImportRequest, db: Session = Depends(get_db_dep)):
    """
    One-time import of existing portfolio_history.csv into PostgreSQL.
    Run once after deploying, then never again.
    """
    try:
        count = import_from_csv(db, body.csv_path)
        return {"imported": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/debug/fonts")
def debug_fonts():
    import subprocess
    result = subprocess.run(
        ["find", "/usr", "-name", "DejaVuSansMono.ttf"],
        capture_output=True, text=True
    )
    return {"found": result.stdout.strip().split("\n")}


@app.post("/admin/backfill")
def backfill_history(db: Session = Depends(get_db_dep)):
    """
    Backfill portfolio_history from Jan 1 of current year to yesterday
    using current Plaid holdings × yfinance historical daily closes.
 
    Only processes investment accounts (type=investment).
    Uses current quantities — positions bought/sold during the year
    will not be perfectly accurate but close enough for the chart.
 
    Safe to re-run — skips dates already in portfolio_history.
    """
    import yfinance as yf
    from datetime import date, timedelta, datetime
    from models import PortfolioHistory
 
    START_DATE = date(date.today().year, 1, 1)
    END_DATE   = date.today() - timedelta(days=1)
    CASH_TYPES = {"cash", "money market"}
 
    # Get all investment accounts
    accounts = (
        db.query(Account)
        .filter_by(is_active=True)
        .filter(Account.account_type == "investment")
        .all()
    )
 
    if not accounts:
        return {"error": "No investment accounts found — run /plaid/sync first"}
 
    # Collect all unique non-cash tickers across all investment accounts
    all_tickers: set[str] = set()
    account_holdings: dict[str, list[dict]] = {}
 
    for acct in accounts:
        positions = []
        for h in acct.holdings:
            sec = h.security
            if sec.is_cash_equivalent or not sec.ticker_symbol:
                positions.append({
                    "ticker":     None,
                    "quantity":   h.quantity,
                    "is_cash":    True,
                    "cash_value": h.institution_value or 0.0,
                })
            else:
                positions.append({
                    "ticker":   sec.ticker_symbol,
                    "quantity": h.quantity,
                    "is_cash":  False,
                })
                all_tickers.add(sec.ticker_symbol)
        account_holdings[acct.slug] = positions
 
    if not all_tickers:
        return {"error": "No non-cash tickers found in holdings"}
 
    # Fetch historical daily closes for all tickers in one batch

    print(
        f"Fetching historical prices for {len(all_tickers)} tickers "
        f"({START_DATE} → {END_DATE})..."
    )

    YAHOO_TICKER_MAP = {
        "BRK.B": "BRK-B",
    }

    # Keep original tickers for database/portfolio use
    original_tickers = list(all_tickers)

    # Convert only for Yahoo Finance
    yahoo_tickers = [
        YAHOO_TICKER_MAP.get(ticker, ticker)
        for ticker in original_tickers
    ]

    try:
        raw = yf.download(
            tickers=" ".join(yahoo_tickers),
            start=START_DATE.strftime("%Y-%m-%d"),
            end=(END_DATE + timedelta(days=1)).strftime("%Y-%m-%d"),
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"yfinance download failed: {e}"
        )

    # Build price lookup: {original_ticker: {date: close_price}}
    price_map = {ticker: {} for ticker in original_tickers}

    if not raw.empty:

        for original_ticker, yahoo_ticker in zip(
            original_tickers,
            yahoo_tickers
        ):
            try:
                # With group_by="ticker", each ticker is a top-level column
                series = raw[yahoo_ticker]["Close"]

                for ts, val in series.items():
                    if val == val:  # skip NaN
                        price_map[original_ticker][ts.date()] = float(val)

            except Exception as e:
                print(
                    f"Could not process {original_ticker} "
                    f"(Yahoo: {yahoo_ticker}): {e}"
                )
 
    # Compute daily portfolio values and write to portfolio_history
    total_written = 0
    results       = {}
 
    for slug, positions in account_holdings.items():
        print(f"Computing history for {slug}...")
 
        # Get all dates where we have prices
        all_dates: set[date] = set()
        for pos in positions:
            if not pos["is_cash"] and pos["ticker"]:
                all_dates.update(price_map.get(pos["ticker"], {}).keys())
 
        sorted_dates = sorted(d for d in all_dates if START_DATE <= d <= END_DATE)
        if not sorted_dates:
            results[slug] = "no price data"
            continue
 
        # Get existing dates to skip
        existing = set(
            row[0].date()
            for row in db.query(PortfolioHistory.snapshot_at)
            .filter_by(slug=slug)
            .filter(PortfolioHistory.snapshot_at >= datetime.combine(START_DATE, datetime.min.time()))
            .all()
        )
 
        base_value = None
        written    = 0
 
        for d in sorted_dates:
            if d in existing:
                continue
 
            # Compute total portfolio value for this date
            total = 0.0
            skip  = False
            for pos in positions:
                if pos["is_cash"]:
                    total += pos["cash_value"]
                    continue
                price = price_map.get(pos["ticker"], {}).get(d)
                if price is None:
                    # Use most recent prior price
                    prior_dates = [pd for pd in price_map.get(pos["ticker"], {}) if pd < d]
                    price = price_map[pos["ticker"]][max(prior_dates)] if prior_dates else None
                if price is None:
                    skip = True
                    break
                total += price * pos["quantity"]
 
            if skip:
                continue
 
            total = round(total, 2)
            if base_value is None:
                base_value = total
 
            pct_gain = round((total - base_value) / base_value * 100, 4) if base_value else 0.0
 
            db.add(PortfolioHistory(
                snapshot_at  =datetime.combine(d, datetime.min.time()),
                slug         =slug,
                pct_gain     =pct_gain,
                dollar_value =total,
                source       ="backfill",
            ))
            written += 1
 
            if written % 100 == 0:
                db.flush()
 
        db.commit()
        total_written += written
        results[slug]  = f"{written} rows written ({sorted_dates[0]} → {sorted_dates[-1]})"
        print(f"  {slug}: {written} rows")
 
    return {
        "total_written": total_written,
        "start_date":    START_DATE.isoformat(),
        "end_date":      END_DATE.isoformat(),
        "accounts":      results,
    }


@app.get("/prices/refresh-dividends")
def refresh_dividends_endpoint(db: Session = Depends(get_db_dep)):
    """Refresh dividend data — call once daily, not every 5 min."""
    from prices import refresh_dividends
    events = refresh_dividends(db)
    return {"dividend_events": len(events), "events": [
        {"symbol": e["symbol"], "date": str(e["date"]), "detail": e["detail"]}
        for e in events
    ]}

@app.get("/debug/plaid-accounts")
def debug_plaid_accounts(db: Session = Depends(get_db_dep)):
    from models import PlaidItem
    import plaid_client as pc
    item = db.query(PlaidItem).first()
    if not item:
        return {"error": "no plaid item found"}
    data = pc.get_holdings(item.access_token)
    return {
        "accounts": [
            {
                "account_id": a["account_id"],
                "name": a.get("name"),
                "type": a.get("type"),
                "subtype": a.get("subtype"),
            }
            for a in data["accounts"]
        ]
    }