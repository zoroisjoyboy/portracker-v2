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
from models import PlaidItem
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
    """Manually trigger full Plaid sync for all linked items."""
    results = sync_all(db)
    return {"results": results}


@app.get("/prices/refresh")
def manual_price_refresh(db: Session = Depends(get_db_dep)):
    """Manually trigger yfinance price refresh."""
    result = refresh_prices(db)

    # Log portfolio snapshots to history
    slugs = ["individual", "roth_ira"]
    pf_values = {slug: get_portfolio_value(db, slug) for slug in slugs}
    log_all_snapshots(db, pf_values)

    return {"prices": result, "portfolios": {
        slug: {"total_value": pf["total_value"], "daily_pct": pf["daily_pct"]}
        for slug, pf in pf_values.items()
    }}


# ── Display image ─────────────────────────────────────────────────────────────

def _render_mode(mode: str, db: Session) -> bytes:
    slugs      = ["individual", "roth_ira"]
    portfolios = {slug: get_portfolio_value(db, slug) for slug in slugs}
    indices    = get_indices(db)
    history    = load_history(db, mode)
    events     = get_upcoming_events(db, slugs)

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
    """Convenience routes: /display/image/daily, /display/image/ytd, etc."""
    if mode not in ("daily", "monthly", "ytd"):
        raise HTTPException(status_code=400, detail="mode must be daily, monthly, or ytd")
    buf = _render_mode(mode, db)
    return StreamingResponse(buf, media_type="image/png")


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