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
  * Notes-present: the newest 5 items on Pages AND in the committed feed.xml must each have
    non-empty show notes (<description>). This catches the exact bug where e628/e629 shipped
    with blank notes -- a silent failure the count/title checks miss. FeedBurner is a cached
    proxy and lags, so notes are checked on Pages + local only.
  * Updater-fired heartbeat (Actions only): query the GitHub API for the most recent
    successful run of update.yml; fail if it's older than 3h (the updater polls every 15 min,
    so 3h == ~12 missed runs == a real stall). Skipped silently when run locally (no token).

The original outage was exactly "FeedBurner serving truncated, invalid XML" -- this catches
that, plus any count regression, non-200, blank notes, or a stalled updater.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

FB = "https://feeds.feedburner.com/RoderickOnTheLine"
PAGES = "https://merlinmann.github.io/rotl-feed/feed.xml"
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.xml")

NOTES_CHECK_N = 5            # check the newest N items for present notes
HEARTBEAT_MAX_AGE_H = 3      # updater polls every 15 min; 3h == ~12 missed runs


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "rotl-feed-health/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.getcode(), r.read()


def count_and_newest(data):
    root = ET.fromstring(data)  # raises ParseError on truncated/invalid XML
    items = root.findall(".//item")
    newest = (items[0].findtext("title") or "") if items else ""
    return len(items), newest


def _item_label(item):
    """Short label for reporting, e.g. 'e629' or a trimmed title."""
    title = (item.findtext("title") or "").strip()
    import re
    m = re.search(r"Ep\.?\s*(\d+)", title)
    if m:
        return f"e{m.group(1)}"
    return title[:20] or "?"


def _notes_text(item):
    """Description text with tags + whitespace stripped."""
    raw = item.findtext("description") or ""
    import re
    no_tags = re.sub(r"<[^>]+>", "", raw)
    return no_tags.strip()


def blank_notes(data):
    """Return labels of the newest N items whose notes are blank ([] if all present)."""
    root = ET.fromstring(data)
    items = root.findall(".//item")[:NOTES_CHECK_N]
    return [_item_label(it) for it in items if not _notes_text(it)]


def check_updater_heartbeat(fails):
    """Updater-fired heartbeat -- Actions only.

    Confirms update.yml is actually running. Skips silently when run locally (no token),
    so local runs never fail on this. On any GitHub API hiccup it warns but does NOT fail
    -- a token/API blip must not become a false alarm.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return  # local run -> skip silently

    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"update.yml/runs?per_page=10"
    )
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "rotl-feed-health/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read())
    except Exception as e:
        print(f"updater: heartbeat check skipped (GitHub API error: {e})")
        return

    runs = payload.get("workflow_runs", [])
    success = [run for run in runs if run.get("conclusion") == "success"]
    if not success:
        print("updater: heartbeat check skipped (no recent successful update.yml run found)")
        return

    # Newest successful run by updated_at.
    def parse_ts(run):
        return datetime.strptime(run["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    newest = max(success, key=parse_ts)
    age = datetime.now(timezone.utc) - parse_ts(newest)
    age_min = age.total_seconds() / 60
    if age_min > HEARTBEAT_MAX_AGE_H * 60:
        fails.append(f"updater: STALE (last success {age_min/60:.1f}h ago)")
        print(f"updater: STALE (last success {age_min/60:.1f}h ago) [STALE]")
    else:
        print(f"updater: last success {age_min:.0f}m ago [ok]")


def main():
    # Guard: no committed feed.xml (e.g. hook smoke sandbox) -> nothing to check.
    if not os.path.exists(LOCAL):
        print(f"no {LOCAL} -- nothing to check", file=sys.stderr)
        return 0

    local_bytes = open(LOCAL, "rb").read()
    expected, expected_newest = count_and_newest(local_bytes)
    print(f"expected: {expected} items, newest {expected_newest!r}")

    fails = []
    targets = [
        ("Pages", PAGES, expected),                  # serves our file verbatim -> exact
        ("FeedBurner", FB, int(expected * 0.90)),    # cached proxy -> allow lag
    ]
    page_data = {}  # capture fetched bytes for the notes check below
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
        page_data[name] = data
        status = "ok"
        if n < floor:
            fails.append(f"{name}: only {n} items (floor {floor})")
            status = "LOW"
        if not newest.strip():
            fails.append(f"{name}: no newest title")
            status = "EMPTY"
        print(f"{name}: 200, {n} items, newest={newest!r} [{status}]")

    # Notes-present test -- the check that would have caught the e628/e629 blank bug.
    # Pages (live, what we control) + local committed feed. FeedBurner lags, so skip it.
    notes_sources = [("local", local_bytes)]
    if "Pages" in page_data:
        notes_sources.append(("Pages", page_data["Pages"]))
    for src_name, src_data in notes_sources:
        try:
            blanks = blank_notes(src_data)
        except ET.ParseError as e:
            fails.append(f"notes[{src_name}]: INVALID XML ({e})")
            continue
        if blanks:
            fails.append(f"notes[{src_name}]: BLANK on {', '.join(blanks)}")
            print(f"notes[{src_name}]: BLANK on {', '.join(blanks)} [BLANK]")
        else:
            print(f"notes[{src_name}]: newest {NOTES_CHECK_N} all present [ok]")

    # Updater-fired heartbeat (Actions only; silent skip locally).
    check_updater_heartbeat(fails)

    if fails:
        print("\nUNHEALTHY:")
        for f in fails:
            print("  -", f)
        return 1
    print("\nfeed healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
