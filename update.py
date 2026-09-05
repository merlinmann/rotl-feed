#!/usr/bin/env python3
"""Incrementally refresh feed.xml for Roderick on the Line.

feed.xml is the complete, valid source of truth (631+ episodes, all GUIDs preserved).
This script adds episodes that aren't in it yet, and moves an enclosure to the public
radio archive once an exact-size audio copy is available there. It fetches Squarespace's
live rss.xml (whose newest items stay valid even when the old tail truncates), finds any
<item> whose GUID isn't already present, fills a real enclosure length= via a ranged
GET, inserts it at the top, validates, and rewrites feed.xml. Exits 0 with no change
when nothing is new and no enclosure is ready to migrate.

Designed to run on GitHub Actions cron -- no Merlin hardware involved. See
rotl/reference/feed-migration-2026-06-02.md in the hub for the full picture.
"""
import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from xml.etree import ElementTree as ET

HERE = Path(__file__).resolve().parent
FEED = HERE / "feed.xml"
CACHE = HERE / ".mp3-length-cache.json"
LIVE_URL = "http://www.merlinmann.com/roderick/rss.xml"
RADIO_BASE = "https://radio.contiguous.me/rotl/episodes/"
SQUARESPACE_FALLBACK_EPISODES = {
    77, 80, 88, 89, 143, 144, 145, 146, 147, 148, 152,
}

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
ARCHIVE_MP3_RE = re.compile(r"rotl_(?:e)?0*(\d+)\.mp3", re.I)
TAG_RE = re.compile(r"<[^>]+>")
LEGACY_ARCHIVE_ALIASES = {
    "rotl_0580a.mp3": "rotl_0580.mp3",
    "rotl_0047_esquivalience.mp3": "rotl_0047.mp3",
    "b2w-031.mp3": "rotl_0046.mp3",
    "rotl-0018.mp3": "rotl_0018.mp3",
}


def fetch_notes(link_url):
    """Pull show notes from an episode's HTML page when Squarespace's rss.xml ships
    a blank <description> (its XML Syndication is set to titles-only). The notes live
    in <div class="body"> ... <p class="enclosureWrapper">. Returns the inner HTML
    (relative storage/roderick URLs absolutised) or None on ANY fetch/parse failure --
    callers must treat None as 'leave the existing description untouched'."""
    if not link_url:
        return None
    try:
        req = urllib.request.Request(link_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            page_html = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    m = re.search(r'<div class="body">(.*?)<p class="enclosureWrapper">', page_html, re.S)
    if not m:
        m = re.search(r'<div class="body">(.*?)</div>', page_html, re.S)
        if not m:
            return None
    body = m.group(1)
    body = body.replace('src="/storage/', 'src="http://www.merlinmann.com/storage/')
    body = body.replace('href="/roderick/', 'href="http://www.merlinmann.com/roderick/')
    body = body.strip()
    return body or None


def description_is_blank(item):
    """True when the item has no <description> or its text is empty after stripping
    HTML tags and whitespace."""
    d = item.find("description")
    if d is None or not d.text:
        return True
    return TAG_RE.sub("", d.text).strip() == ""


def backfill_blank_notes(channel, limit=30):
    """For the newest `limit` items (by episode number) whose <description> is blank,
    fetch the notes from the episode's HTML page and fill it in. Only ever fills an
    empty description -- it never blanks or overwrites existing notes, so it fires once
    per blank episode and then never again (no churn). A later edit to the HTML page
    does NOT propagate; that is intended. Returns the list of episode numbers filled."""
    items = sorted(channel.findall("item"), key=ep_num, reverse=True)[:limit]
    filled = []
    for it in items:
        if not description_is_blank(it):
            continue
        notes = fetch_notes(it.findtext("link"))
        if not notes:
            continue
        d = it.find("description")
        if d is None:
            d = ET.Element("description")
            title = it.find("title")
            idx = list(it).index(title) + 1 if title is not None else 0
            it.insert(idx, d)
        d.text = notes
        n = ep_num(it)
        filled.append(n if n >= 0 else "?")
    return filled


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


def refresh_build_date(channel):
    """Set <lastBuildDate> to the newest episode's pubDate. It was frozen at the
    one-shot build date (2026-02-02) because this incremental updater never touched
    it -- cosmetic, but a stale lastBuildDate trips strict feed validators. Returns
    True only when the value actually changed, so a no-op run produces no diff (and
    so no hourly commit). 2026-06-15."""
    items = channel.findall("item")
    if not items:
        return False
    pub = max(items, key=ep_num).findtext("pubDate")
    if not pub:
        return False
    lbd = channel.find("lastBuildDate")
    if lbd is None:
        lbd = ET.Element("lastBuildDate")
        channel.insert(0, lbd)
    if (lbd.text or "").strip() == pub.strip():
        return False
    lbd.text = pub
    return True


def repair_live(raw):
    end = raw.rfind("</item>")
    if end == -1:
        # ValueError, not SystemExit: a truncated/empty upstream body is a transient
        # Squarespace hiccup that main() catches and turns into a no-op green run.
        raise ValueError("live feed has no complete <item> -- cannot repair")
    return raw[: end + len("</item>")] + "</channel></rss>"


def archive_filename(url):
    """Return the canonical filename used by the public RotL media archive."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    name = Path(path).name
    if name.lower() in LEGACY_ARCHIVE_ALIASES:
        return LEGACY_ARCHIVE_ALIASES[name.lower()]
    match = ARCHIVE_MP3_RE.fullmatch(name)
    if match:
        return f"rotl_{int(match.group(1)):04d}.mp3"
    if name in {"ballew-rotl-song.mp3", "rotl_bonus_verdi.mp3"}:
        return name
    return None


def radio_url(url):
    filename = archive_filename(url)
    return RADIO_BASE + filename if filename else None


def mp3_length(url, cache):
    """Ranged GET -> 206 + Content-Range: bytes 0-0/TOTAL. HEAD 403s because the MP3
    URL redirects to a GET-signed S3 URL. Only accept audio responses: Squarespace's
    V5 outage page returns 206 + Content-Length too, but it is HTML, not an MP3.
    Cache only successes so misses retry."""
    if cache.get(url):
        return cache[url]
    val = None
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "rotl-feed-updater/1.0", "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            content_type = (r.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            if not content_type.startswith("audio/"):
                return None
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                total = cr.rsplit("/", 1)[-1]
                if total.isdigit():
                    val = int(total)
            if val is None and r.getcode() == 200:
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit():
                    val = int(cl)
    except (urllib.error.URLError, OSError, ValueError):
        val = None
    if val:
        cache[url] = val
    return val


def migrate_ready_enclosures(channel, cache):
    """Move non-radio enclosures to the public archive once their exact file is ready.

    The committed feed is already migrated, so routine runs only probe new/unmigrated
    items. A candidate must serve audio and match the enclosure's declared byte length;
    otherwise the existing URL stays untouched and the next run retries.
    """
    migrated = []
    for item in channel.findall("item"):
        if ep_num(item) in SQUARESPACE_FALLBACK_EPISODES:
            continue
        enc = item.find("enclosure")
        if enc is None:
            continue
        current = enc.get("url", "")
        if current.startswith(RADIO_BASE):
            continue
        candidate = radio_url(current)
        if not candidate:
            continue
        actual = mp3_length(candidate, cache)
        declared = enc.get("length", "")
        if actual and (not declared.isdigit() or actual == int(declared)):
            enc.set("url", candidate)
            enc.set("length", str(actual))
            migrated.append(_item_label(item))
        else:
            # A missing or mismatched archive copy must be retried next run.
            cache.pop(candidate, None)
            if actual:
                print(f"WARNING: archive length mismatch for {_item_label(item)}: "
                      f"feed={declared or '?'} archive={actual}")
    return migrated


def _item_label(item):
    n = ep_num(item)
    return f"e{n}" if n >= 0 else (item.findtext("title") or "?").strip()


def main():
    if not FEED.exists():
        print("feed.xml missing -- run the one-shot builder first", file=sys.stderr)
        return 1

    tree = ET.parse(FEED)
    channel = tree.getroot().find("channel")
    have = {guid_of(it) for it in channel.findall("item")}
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    print(f"feed.xml: {len(have)} existing items")

    # Retire the FeedBurner hop: advertise the canonical GitHub Pages URL so Apple
    # and compliant apps migrate subscribers off FeedBurner over the coming weeks.
    # Idempotent + self-healing -- added once, then preserved on every later run.
    ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
    NEW_FEED_URL = "https://merlinmann.github.io/rotl-feed/feed.xml"
    nf_added = False
    if channel.find(f"{{{ITUNES_NS}}}new-feed-url") is None:
        nf = ET.Element(f"{{{ITUNES_NS}}}new-feed-url")
        nf.text = NEW_FEED_URL
        first_item = channel.find("item")
        idx = list(channel).index(first_item) if first_item is not None else len(list(channel))
        channel.insert(idx, nf)
        nf_added = True
        print(f"added itunes:new-feed-url -> {NEW_FEED_URL}")

    # feed.xml is the source of truth. Squarespace's live rss.xml is only consulted to
    # discover NEW episodes, and it intermittently serves a 5xx, a timeout, or a
    # truncated body. A momentary upstream hiccup must NOT fail the job (which emails
    # Merlin) -- it's a no-op: skip the new-episode check, still run local maintenance
    # (new-feed-url / lastBuildDate / note backfill), and exit 0. A genuine sustained
    # outage is caught separately by the every-6-hours ROTL feed health workflow.
    live_items = []
    try:
        with urllib.request.urlopen(
            urllib.request.Request(LIVE_URL, headers={"User-Agent": "rotl-feed-updater/1.0"}),
            timeout=30,
        ) as r:
            raw = r.read().decode("utf-8", "replace")
        live = ET.fromstring(repair_live(raw))
        live_channel = live.find("channel")
        if live_channel is None:
            raise ValueError("live feed has no <channel>")
        live_items = live_channel.findall("item")
    except (urllib.error.URLError, OSError, ValueError, ET.ParseError) as e:
        print(f"WARNING: live feed unavailable this run ({e}); "
              "keeping feed.xml as-is and skipping new-episode check")

    new = [it for it in live_items if guid_of(it) and guid_of(it) not in have]
    if not new:
        bd_changed = refresh_build_date(channel)
        filled = backfill_blank_notes(channel)
        migrated = migrate_ready_enclosures(channel, cache)
        if nf_added or bd_changed or filled or migrated:
            tree.write(FEED, encoding="UTF-8", xml_declaration=True)
            ET.parse(FEED)  # validate; raises on malformed
            if migrated:
                CACHE.write_text(json.dumps(cache))
            reasons = [r for r, on in (("itunes:new-feed-url", nf_added),
                                       ("lastBuildDate", bd_changed)) if on]
            if filled:
                reasons.append("backfilled notes: " + ", ".join(f"e{n}" for n in filled))
            if migrated:
                reasons.append("migrated audio: " + ", ".join(migrated))
            print(f"feed.xml updated ({'; '.join(reasons)}); no new episodes")
            return 0
        print("no new episodes -- feed.xml unchanged")
        return 0

    for it in new:
        enc = it.find("enclosure")
        if enc is not None and enc.get("url"):
            ln = mp3_length(enc.get("url"), cache)
            if ln:
                enc.set("length", str(ln))
    # Insert new items, then re-sort the whole channel newest-first by episode number.
    for it in new:
        channel.append(it)
    items = channel.findall("item")
    items.sort(key=ep_num, reverse=True)
    for it in channel.findall("item"):
        channel.remove(it)
    for it in items:
        channel.append(it)

    migrated = migrate_ready_enclosures(channel, cache)
    CACHE.write_text(json.dumps(cache))
    refresh_build_date(channel)
    filled = backfill_blank_notes(channel)
    tree.write(FEED, encoding="UTF-8", xml_declaration=True)
    ET.parse(FEED)  # validate; raises on malformed
    titles = ", ".join((it.findtext("title") or "?").strip() for it in new)
    print(f"added {len(new)} episode(s): {titles}")
    if filled:
        print("backfilled notes: " + ", ".join(f"e{n}" for n in filled))
    if migrated:
        print("migrated audio: " + ", ".join(migrated))
    print(f"feed.xml now {len(items)} items, validated OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
