#!/usr/bin/env python3
"""Fetch delayed market quotes from Yahoo Finance and write data/prices.json.

Run by .github/workflows/update-prices.yml on a schedule; the result is
force-pushed to the single-file `data` branch, which the Markets section on
index.html reads via raw.githubusercontent.com. Stdlib only — no pip installs.

ICE-licensed products (LS Gasoil, API2/Newcastle coal) are unavailable from
every free channel — APIs and TradingView embeds alike ("This symbol is only
available on TradingView") — hence the NYMEX-only panel.
"""
import datetime
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

INSTRUMENTS = [
    {"id": "brent", "symbol": "BZ=F", "name": "Brent Crude", "exchange": "NYMEX", "unit": "USD/bbl"},
    {"id": "wti", "symbol": "CL=F", "name": "WTI Crude", "exchange": "NYMEX", "unit": "USD/bbl"},
    {"id": "diesel", "symbol": "HO=F", "name": "Diesel ULSD", "exchange": "NYMEX", "unit": "USD/gal"},
    {"id": "gasoline", "symbol": "RB=F", "name": "Gasoline RBOB", "exchange": "NYMEX", "unit": "USD/gal"},
]

CHART_URL = "https://query{host}.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d"
USER_AGENT = "Mozilla/5.0 (horsanbrokers.com market panel; contact via site form)"
OUT_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "prices.json"


def fetch_chart(symbol):
    last_error = None
    for attempt in range(3):
        host = 1 if attempt % 2 == 0 else 2
        url = CHART_URL.format(host=host, symbol=urllib.parse.quote(symbol))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.load(resp)
            result = payload["chart"]["result"][0]
            if result["meta"].get("regularMarketPrice") is None:
                raise ValueError("no regularMarketPrice in response")
            return result
        except Exception as err:  # noqa: BLE001 — retry any transport/shape error
            last_error = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{symbol}: {last_error}")


def build_quote(spec):
    result = fetch_chart(spec["symbol"])
    meta = result["meta"]
    timestamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    points = [(t, c) for t, c in zip(timestamps, closes) if c is not None]

    price = meta["regularMarketPrice"]
    market_time = meta.get("regularMarketTime")
    market_day = (
        datetime.datetime.fromtimestamp(market_time, datetime.timezone.utc).date()
        if market_time
        else None
    )

    # Previous close = last daily bar strictly before the current session; the
    # final bar of the range is the (partial) session that regularMarketPrice
    # belongs to, so it must not be compared against itself.
    prev_close = None
    for ts, close in reversed(points):
        bar_day = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()
        if market_day is None or bar_day < market_day:
            prev_close = close
            break

    change = round(price - prev_close, 4) if prev_close else None
    change_pct = round((price - prev_close) / prev_close * 100, 2) if prev_close else None

    # Sub-$10 contracts (ULSD/RBOB in USD/gal) quote in 4 decimals; 2 is enough
    # above that. The front-end applies the same threshold when formatting.
    decimals = 2 if price >= 10 else 4

    spark = [
        [datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat(), round(close, decimals)]
        for ts, close in points
    ]

    return {
        "id": spec["id"],
        "symbol": spec["symbol"],
        "name": spec["name"],
        "exchange": spec["exchange"],
        "unit": spec["unit"],
        "currency": meta.get("currency", "USD"),
        "price": round(price, decimals),
        "prevClose": round(prev_close, decimals) if prev_close else None,
        "change": change,
        "changePct": change_pct,
        "marketTime": market_time,
        "spark": spark,
    }


def main():
    quotes = [build_quote(spec) for spec in INSTRUMENTS]
    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "NYMEX delayed quotes via Yahoo Finance",
        "quotes": quotes,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(f"wrote {OUT_PATH} — " + ", ".join(f"{q['name']} {q['price']}" for q in quotes))


if __name__ == "__main__":
    sys.exit(main())
