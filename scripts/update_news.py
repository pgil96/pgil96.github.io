#!/usr/bin/env python3
"""Fetch oil-market headlines from public RSS feeds and write data/news.json.

Run by .github/workflows/update-prices.yml alongside update_prices.py; the
result is force-pushed to the `data` branch, which the Markets section on
index.html reads via raw.githubusercontent.com. Stdlib only — no pip installs.

Picks 4 headlines — 2 crude oil + 2 middle distillates — from Google News
(which aggregates Reuters, WSJ, Bloomberg, S&P Global…) and OilPrice.com,
scored by source quality and recency. To tune coverage, edit FEEDS,
SOURCE_WEIGHTS and the keyword sets below.
"""
import datetime
import email.utils
import json
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

FEEDS = [
    {"url": GNEWS.format(q=urllib.parse.quote('"crude oil" OR brent OR WTI OR OPEC when:2d')), "topic": "crude"},
    {"url": GNEWS.format(q=urllib.parse.quote('diesel OR gasoil OR "middle distillates" OR "jet fuel" market when:2d')), "topic": "distillates"},
    {"url": "https://oilprice.com/rss/main", "topic": None, "source": "OilPrice.com"},
]

# Higher = more authoritative; unknown sources get DEFAULT_WEIGHT. Recency adds
# up to ~7 points on top (see score()), so a fresh no-name can outrank a stale
# wire story but not a same-day one.
SOURCE_WEIGHTS = {
    "reuters": 10, "bloomberg": 10, "the wall street journal": 10, "wsj": 10,
    "financial times": 10, "s&p global": 9, "argus media": 9, "energy intelligence": 8,
    "cnbc": 8, "marketwatch": 8, "axios": 8, "oilprice.com": 7, "investing.com": 6,
    "yahoo finance": 6, "the new york times": 8, "fortune": 6, "tradingview": 4,
}
DEFAULT_WEIGHT = 3
MAX_AGE_HOURS = 48

DISTILLATE_WORDS = re.compile(
    r"\b(diesel|gasoil|gas oil|distillate|jet fuel|kerosene|heating oil|ulsd|refining margin|crack spread)s?\b", re.I)
CRUDE_WORDS = re.compile(
    r"\b(crude|brent|wti|opec|oil price|oil market|barrel|oil export|oil supply|oil demand|oil rises|oil falls|oil climbs|oil drops)s?\b", re.I)
# PR-wire noise, market-research spam and evergreen price trackers.
JUNK = re.compile(
    r"market (size|report|forecast)|to reach usd|cagr|stocks? to (buy|watch)|price prediction"
    r"|top \d+|how to invest|aaa fuel|gas prices today|press release", re.I)

USER_AGENT = "Mozilla/5.0 (horsanbrokers.com market panel; contact via site form)"
OUT_PATH = pathlib.Path(__file__).resolve().parent.parent / "data" / "news.json"


def fetch(url):
    last_error = None
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as err:  # noqa: BLE001 — retry any transport error
            last_error = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last_error}")


def parse_feed(spec):
    """Yield {title, url, source, published, topic} for each usable item."""
    try:
        root = ET.fromstring(fetch(spec["url"]))
    except Exception as err:  # noqa: BLE001 — one dead feed must not kill the rest
        print(f"warning: skipping feed ({err})", file=sys.stderr)
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source = (item.findtext("source") or spec.get("source") or "").strip()
        # Google News appends " - Source" to every title; strip it.
        if source and title.lower().endswith(" - " + source.lower()):
            title = title[: -len(source) - 3].strip()
        try:
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate", ""))
        except (TypeError, ValueError):
            continue
        if not title or not link or JUNK.search(title):
            continue
        age_h = (now - published).total_seconds() / 3600
        if not 0 <= age_h <= MAX_AGE_HOURS:
            continue
        topic = spec["topic"]
        if topic is None:  # topic-less feeds must self-classify or be dropped
            if DISTILLATE_WORDS.search(title):
                topic = "distillates"
            elif CRUDE_WORDS.search(title):
                topic = "crude"
            else:
                continue
        elif topic == "crude" and DISTILLATE_WORDS.search(title):
            topic = "distillates"  # the crude query also catches distillate stories
        yield {
            "topic": topic,
            "title": title,
            "url": link,
            "source": source or "News",
            "published": published.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "_age_h": age_h,
        }


def score(art):
    weight = SOURCE_WEIGHTS.get(art["source"].lower(), DEFAULT_WEIGHT)
    return weight + max(0.0, 24 - art["_age_h"]) * 0.3


def norm_title(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())[:60]


def pick(articles, topic, count, chosen):
    """Top `count` for `topic`, avoiding titles and sources already chosen."""
    seen_titles = {norm_title(a["title"]) for a in chosen}
    picked = []
    pool = sorted((a for a in articles if a["topic"] == topic), key=score, reverse=True)
    for prefer_new_source in (True, False):
        for art in pool:
            if len(picked) == count:
                return picked
            used_sources = {a["source"] for a in chosen + picked}
            if norm_title(art["title"]) in seen_titles:
                continue
            if prefer_new_source and art["source"] in used_sources:
                continue
            picked.append(art)
            seen_titles.add(norm_title(art["title"]))
    return picked


def main():
    articles = [a for spec in FEEDS for a in parse_feed(spec)]
    chosen = pick(articles, "crude", 2, [])
    chosen += pick(articles, "distillates", 2, chosen)
    # Backfill from the other bucket rather than publish an uneven panel.
    if len(chosen) < 4:
        chosen += pick(articles, "crude", 4 - len(chosen), chosen)
    if len(chosen) < 4:
        raise RuntimeError(f"only {len(chosen)} usable headlines found")

    for art in chosen:
        art.pop("_age_h")
    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Public RSS feeds (Google News, OilPrice.com)",
        "articles": chosen,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(f"wrote {OUT_PATH} — " + "; ".join(f"[{a['topic']}] {a['source']}: {a['title'][:60]}" for a in chosen))


if __name__ == "__main__":
    sys.exit(main())
