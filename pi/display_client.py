"""
display_client.py
Polls the portracker server for a pre-rendered PNG and pushes it
to the Waveshare 4.26" e-paper display.

Replaces render_display.py, refresh_prices.py, history_logger.py.
All data fetching and rendering now happens server-side.

Environment variables:
    SERVER_URL   — e.g. https://your-app.up.railway.app
    DISPLAY_MODE — daily | monthly | ytd (set by cron per time of day)
"""

import os
import sys
import io
from datetime import datetime

import requests
from PIL import Image

SERVER_URL   = os.environ.get("SERVER_URL", "http://localhost:8000")
DISPLAY_MODE = os.environ.get("DISPLAY_MODE", "daily")
TIMEOUT      = 30  # seconds


def fetch_image() -> Image.Image:
    url = f"{SERVER_URL}/display/image/{DISPLAY_MODE}"
    r   = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("1")


def push_to_display(img: Image.Image):
    try:
        sys.path.insert(0, "/home/asaakov/portracker/lib")
        from waveshare_epd import epd4in26
        epd = epd4in26.EPD()
        epd.init()
        epd.display(epd.getbuffer(img))
        epd.sleep()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Display updated ({DISPLAY_MODE})")
    except ImportError:
        out = "/home/asaakov/portracker/last_render.png"
        img.save(out)
        print(f"[PREVIEW] Driver not found — saved to {out}")
    except Exception as e:
        print(f"[ERROR] Display push failed: {e}")


def main():
    try:
        img = fetch_image()
        push_to_display(img)
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Could not reach server: {e}")


if __name__ == "__main__":
    main()
