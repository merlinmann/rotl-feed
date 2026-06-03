#!/usr/bin/env python3
"""Health check for the Roderick on the Line feed.

Run by .github/workflows/health.yml every 6 hours. Exits NON-ZERO when the live feed
looks unhealthy -- a failed scheduled Actions run makes GitHub email the repo owner, so
a break is caught automatically without any local machine involved.

Checks:
  * GitHub Pages (the source we control): HTTP 200, valid XML, item count == the
    committed feed.xml (Pages serves that file verbatim).
  * FeedBurner (what subscribers actually fetch): HTTP 200, valid XML, item count within
    10% of expected (tolerates FeedBurner's cache lag but catches truncation, which drops
    hundreds of items), and the newest episode title present.

The original outage was exactly "FeedBurner serving truncated, invalid XML" -- this catches
that, plus any count regression or non-200.
"""
import os
import sys
import urllib.request
from xml.etree import ElementTree as ET

FB = "https://feeds.feedburner.com/RoderickOnTheLine"
PAGES = "https://merlinmann.github.io/rotl-feed/feed.xml"
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.xml")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "rotl-feed-health/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.getcode(), r.read()


def count_and_newest(data):
    root = ET.fromstring(data)  # raises ParseError on truncated/invalid XML
    items = root.findall(".//item")
    newest = (items[0].findtext("title") or "") if items else ""
    return len(items), newest


def main():
    # Guard: no committed feed.xml (e.g. hook smoke sandbox) -> nothing to check.
    if not os.path.exists(LOCAL):
        print(f"no {LOCAL} -- nothing to check", file=sys.stderr)
        return 0

    expected, expected_newest = count_and_newest(open(LOCAL, "rb").read())
    print(f"expected: {expected} items, newest {expected_newest!r}")

    fails = []
    targets = [
        ("Pages", PAGES, expected),                  # serves our file verbatim -> exact
        ("FeedBurner", FB, int(expected * 0.90)),    # cached proxy -> allow lag
    ]
    for name, url, floor in targets:
        try:
            code, data = fetch(url)
        except Exception as e:
            fails.append(f"{name}: fetch error: {e}")
            continue
        if code != 200:
            fails.append(f"{name}: HTTP {code}")
            continue
        try:
            n, newest = count_and_newest(data)
        except ET.ParseError as e:
            fails.append(f"{name}: INVALID XML ({e})")
            continue
        status = "ok"
        if n < floor:
            fails.append(f"{name}: only {n} items (floor {floor})")
            status = "LOW"
        if not newest.strip():
            fails.append(f"{name}: no newest title")
            status = "EMPTY"
        print(f"{name}: 200, {n} items, newest={newest!r} [{status}]")

    if fails:
        print("\nUNHEALTHY:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nfeed healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
