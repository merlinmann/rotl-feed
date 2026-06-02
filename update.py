#!/usr/bin/env python3
"""Incrementally refresh feed.xml for Roderick on the Line.

feed.xml is the complete, valid source of truth (631+ episodes, all GUIDs preserved).
This script ONLY adds episodes that aren't in it yet: it fetches Squarespace's live
rss.xml (whose newest items stay valid even when the old tail truncates), finds any
<item> whose GUID isn't already present, fills a real enclosure length= via a ranged
GET, inserts it at the top, validates, and rewrites feed.xml. Exits 0 with no change
when nothing is new.

Designed to run on GitHub Actions cron -- no Merlin hardware involved. See
rotl/reference/feed-migration-2026-06-02.md in the hub for the full picture.
"""
import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
FEED = HERE / "feed.xml"
CACHE = HERE / ".mp3-length-cache.json"
LIVE_URL = "http://www.merlinmann.com/roderick/rss.xml"

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "wfw": "http://wellformedweb.org/CommentAPI/",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "dc": "http://purl.org/dc/elements/1.1/",
}
for p, u in NS.items():
    ET.register_namespace(p, u)

EP_RE = re.compile(r"Ep\.?\s*0*(\d+)", re.I)
MP3_RE = re.compile(r"rotl_0*(\d+)\.mp3", re.I)


def ep_num(item):
    m = EP_RE.search(item.findtext("title") or "")
    if m:
        return int(m.group(1))
    enc = item.find("enclosure")
    if enc is not None:
        m = MP3_RE.search(enc.get("url", ""))
        if m:
            return int(m.group(1))
    return -1


def guid_of(item):
    g = item.find("guid")
    return (g.text or "").strip() if g is not None else None


def repair_live(raw):
    end = raw.rfind("</item>")
    if end == -1:
        raise SystemExit("live feed has no complete <item> -- cannot repair")
    return raw[: end + len("</item>")] + "</channel></rss>"


def mp3_length(url, cache):
    """Ranged GET -> 206 + Content-Range: bytes 0-0/TOTAL. HEAD 403s because the MP3
    URL redirects to a GET-signed S3 URL. Cache only successes so misses retry."""
    if cache.get(url):
        return cache[url]
    val = None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "rotl-feed-updater/1.0", "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                total = cr.rsplit("/", 1)[-1]
                if total.isdigit():
                    val = int(total)
            if val is None:
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit():
                    val = int(cl)
    except (urllib.error.URLError, OSError, ValueError):
        val = None
    if val:
        cache[url] = val
    return val


def main():
    if not FEED.exists():
        print("feed.xml missing -- run the one-shot builder first", file=sys.stderr)
        return 1

    tree = ET.parse(FEED)
    channel = tree.getroot().find("channel")
    have = {guid_of(it) for it in channel.findall("item")}
    print(f"feed.xml: {len(have)} existing items")

    with urllib.request.urlopen(
        urllib.request.Request(LIVE_URL, headers={"User-Agent": "rotl-feed-updater/1.0"}),
        timeout=30,
    ) as r:
        raw = r.read().decode("utf-8", "replace")
    live = ET.fromstring(repair_live(raw))
    live_items = live.find("channel").findall("item")

    new = [it for it in live_items if guid_of(it) and guid_of(it) not in have]
    if not new:
        print("no new episodes -- feed.xml unchanged")
        return 0

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    for it in new:
        enc = it.find("enclosure")
        if enc is not None and enc.get("url"):
            ln = mp3_length(enc.get("url"), cache)
            if ln:
                enc.set("length", str(ln))
    CACHE.write_text(json.dumps(cache))

    # Insert new items, then re-sort the whole channel newest-first by episode number.
    for it in new:
        channel.append(it)
    items = channel.findall("item")
    items.sort(key=ep_num, reverse=True)
    for it in channel.findall("item"):
        channel.remove(it)
    for it in items:
        channel.append(it)

    tree.write(FEED, encoding="UTF-8", xml_declaration=True)
    ET.parse(FEED)  # validate; raises on malformed
    titles = ", ".join((it.findtext("title") or "?").strip() for it in new)
    print(f"added {len(new)} episode(s): {titles}")
    print(f"feed.xml now {len(items)} items, validated OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
