#!/usr/bin/env python3
"""Health check for the Roderick on the Line feed.

Run by .github/workflows/health.yml every hour. Exits NON-ZERO when the live feed
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
  * Media: the newest 5 episodes plus the three tail items (episode zero and both
    bonuses) must serve byte-range audio whose total size matches the enclosure
    length. A playable episode published within the last 96 hours may await its
    automatic public-archive copy; older off-archive media fails. Pages must match the
    full committed enclosure catalog;
    FeedBurner must match after its documented 90-minute polling window. This rejects
    an HTML outage page posing as a 206 and a title-current proxy with stale audio URLs.
  * Updater-fired heartbeat (Actions only): query the GitHub API for the most recent
    successful main-branch run of update.yml. After 3h, the health workflow requests
    one refresh and waits up to 5 minutes for a new success before failing. Scheduled
    runs can arrive hours late. Local checks never request a refresh.

The original outage was exactly "FeedBurner serving truncated, invalid XML" -- this catches
that, plus any count regression, non-200, blank notes, or a stalled updater.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

FB = "https://feeds.feedburner.com/RoderickOnTheLine"
PAGES = "https://merlinmann.github.io/rotl-feed/feed.xml"
LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.xml")

NOTES_CHECK_N = 5            # check the newest N items for present notes
HEARTBEAT_MAX_AGE_H = 3      # request recovery after this long without an update
RECOVERY_TIMEOUT_S = 300
RECOVERY_POLL_S = 10
MEDIA_NEWEST_N = 5
MEDIA_TAIL_N = 3
RADIO_HOST = "radio.contiguous.me"
ARCHIVE_GRACE_H = 96
FEEDBURNER_GRACE_MIN = 90


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
    m = re.search(r"Ep\.?\s*(\d+)", title)
    if m:
        return f"e{m.group(1)}"
    return title[:20] or "?"


def _notes_text(item):
    """Description text with tags + whitespace stripped."""
    raw = item.findtext("description") or ""
    no_tags = re.sub(r"<[^>]+>", "", raw)
    return no_tags.strip()


def blank_notes(data):
    """Return labels of the newest N items whose notes are blank ([] if all present)."""
    root = ET.fromstring(data)
    items = root.findall(".//item")[:NOTES_CHECK_N]
    return [_item_label(it) for it in items if not _notes_text(it)]


def media_samples(data):
    """Return newest episodes plus episode zero and bonuses, without duplicates."""
    root = ET.fromstring(data)
    items = root.findall(".//item")
    chosen = items[:MEDIA_NEWEST_N] + items[-MEDIA_TAIL_N:]
    seen = set()
    result = []
    for item in chosen:
        guid = (item.findtext("guid") or _item_label(item)).strip()
        if guid not in seen:
            seen.add(guid)
            result.append(item)
    return result


def _published_age_hours(item, now=None):
    """Return an item's age in hours, or None when pubDate is missing/invalid."""
    try:
        published = parsedate_to_datetime(item.findtext("pubDate") or "")
    except (TypeError, ValueError):
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - published).total_seconds() / 3600


def check_media(data, fails, now=None):
    """Verify representative enclosures are real, exact byte-range audio."""
    for item in media_samples(data):
        label = _item_label(item)
        enc = item.find("enclosure")
        if enc is None or not enc.get("url"):
            fails.append(f"media[{label}]: missing enclosure")
            continue
        url = enc.get("url")
        off_archive = urllib.parse.urlsplit(url).hostname != RADIO_HOST
        age_h = _published_age_hours(item, now=now) if off_archive else None
        archive_pending = (
            off_archive
            and age_h is not None
            and 0 <= age_h < ARCHIVE_GRACE_H
        )
        declared = enc.get("length", "")
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "rotl-feed-health/1.0", "Range": "bytes=0-0"},
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                code = response.getcode()
                content_type = (
                    response.headers.get("Content-Type") or ""
                ).split(";", 1)[0].lower()
                content_range = response.headers.get("Content-Range") or ""
        except Exception as exc:
            fails.append(f"media[{label}]: fetch error: {exc}")
            continue
        match = re.fullmatch(r"bytes 0-0/(\d+)", content_range)
        actual = int(match.group(1)) if match else None
        problems = []
        if off_archive and not archive_pending:
            problems.append(f"not on {RADIO_HOST} after archive grace")
        if code != 206:
            problems.append(f"HTTP {code}, expected 206")
        if not content_type.startswith("audio/"):
            problems.append(f"Content-Type {content_type or '?'}")
        if actual is None:
            problems.append(f"bad Content-Range {content_range or '?'}")
        elif not declared.isdigit() or actual != int(declared):
            problems.append(f"size {actual}, feed says {declared or '?'}")
        if problems:
            fails.append(f"media[{label}]: " + "; ".join(problems))
            print(f"media[{label}]: " + "; ".join(problems) + " [BAD]")
        else:
            status = (
                f"ok; archive pending {age_h:.0f}h/{ARCHIVE_GRACE_H}h"
                if archive_pending
                else "ok"
            )
            print(f"media[{label}]: 206 {content_type}, {actual} bytes [{status}]")


def enclosure_manifest(data):
    """Return the ordered subscriber-facing identity and enclosure catalog."""
    root = ET.fromstring(data)
    result = []
    for item in root.findall(".//item"):
        enc = item.find("enclosure")
        result.append(
            (
                (item.findtext("guid") or "").strip(),
                (item.findtext("title") or "").strip(),
                None if enc is None else enc.get("url"),
                None if enc is None else enc.get("length"),
            )
        )
    return result


def check_pages_enclosures(local_data, pages_data, fails):
    """Require Pages to expose the committed enclosure catalog."""
    local = enclosure_manifest(local_data)
    pages = enclosure_manifest(pages_data)
    if local != pages:
        fails.append("media[Pages]: enclosure URLs/lengths do not match committed feed")
        print("media[Pages]: enclosure URLs/lengths do not match committed feed [STALE]")
    else:
        print("media[Pages]: enclosure catalog matches committed feed [ok]")


def commit_age_minutes():
    """Return checkout HEAD age so FeedBurner's documented poll lag gets a grace period."""
    try:
        timestamp = int(
            subprocess.check_output(
                ["git", "show", "-s", "--format=%ct", "HEAD"],
                text=True,
                timeout=10,
            ).strip()
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return (datetime.now(timezone.utc).timestamp() - timestamp) / 60


def check_feedburner_enclosures(local_data, feedburner_data, fails, age_min=None):
    """Fail a stale FeedBurner enclosure catalog after its normal polling window."""
    if enclosure_manifest(local_data) == enclosure_manifest(feedburner_data):
        print("media[FeedBurner]: enclosure catalog matches committed feed [ok]")
        return
    age_min = commit_age_minutes() if age_min is None else age_min
    if age_min is not None and age_min < FEEDBURNER_GRACE_MIN:
        print(
            f"media[FeedBurner]: waiting for cache refresh "
            f"({age_min:.0f}m/{FEEDBURNER_GRACE_MIN}m) [LAG]"
        )
        return
    fails.append("media[FeedBurner]: enclosure catalog is stale")
    print("media[FeedBurner]: enclosure catalog is stale [STALE]")


def actions_api(token, repo, path, payload=None):
    """Read Actions state or request one workflow dispatch."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "rotl-feed-health/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def updater_runs(token, repo):
    return actions_api(
        token, repo, "workflows/update.yml/runs?branch=main&per_page=10"
    ).get("workflow_runs", [])


def recover_updater(token, repo, runs):
    """Request one refresh and require a new successful completion within five minutes."""
    # A successful run already observed cannot prove this recovery worked.
    previous_successes = {
        run["id"] for run in runs if run.get("conclusion") == "success"
    }
    # An existing queued/running update can satisfy recovery without another dispatch.
    active = any(run.get("status") in {"queued", "in_progress", "waiting", "pending"}
                 for run in runs)
    if active:
        print("updater: waiting for the update already in progress", flush=True)
    else:
        actions_api(token, repo, "workflows/update.yml/dispatches", {"ref": "main"})
        print("updater: requested a refresh to verify the updater", flush=True)
    deadline = time.monotonic() + RECOVERY_TIMEOUT_S
    last_runs = runs
    while time.monotonic() < deadline:
        last_runs = updater_runs(token, repo)
        for run in last_runs:
            if run.get("conclusion") == "success" and run["id"] not in previous_successes:
                # Also reject older successes entering the response as other runs disappear.
                completed = datetime.strptime(run["updated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                if 0 <= (datetime.now(timezone.utc) - completed).total_seconds() < RECOVERY_TIMEOUT_S:
                    print(f"updater: refresh succeeded ({run['id']}) [recovered]", flush=True)
                    return True
        time.sleep(min(RECOVERY_POLL_S, max(0, deadline - time.monotonic())))
    state = (last_runs[0].get("conclusion") or last_runs[0].get("status")) if last_runs else "no runs"
    print(f"updater: no successful refresh within {RECOVERY_TIMEOUT_S}s ({state})", flush=True)
    return False


def check_updater_heartbeat(fails):
    """Check freshness; the production health workflow may recover a delayed updater."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            fails.append("updater: heartbeat unavailable (missing Actions token or repository)")
        return

    may_recover = (
        os.environ.get("ROTL_RECOVER_UPDATER") == "true"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
    )
    requested_refresh = may_recover and os.environ.get("ROTL_REFRESH_UPDATER") == "true"

    try:
        runs = updater_runs(token, repo)
    except Exception as exc:
        if requested_refresh:
            fails.append(f"updater: requested refresh unavailable (GitHub API error: {exc})")
        else:
            print(f"updater: heartbeat check skipped (GitHub API error: {exc})")
        return

    success = [run for run in runs if run.get("conclusion") == "success"]
    if success:
        newest = max(run["updated_at"] for run in success)
        completed = datetime.strptime(newest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - completed).total_seconds() / 3600
        if age_h <= HEARTBEAT_MAX_AGE_H and not requested_refresh:
            print(f"updater: last success {age_h * 60:.0f}m ago [ok]")
            return
        problem = f"updater: STALE (last success {age_h:.1f}h ago)"
    else:
        problem = "updater: no successful update.yml run among the last 10 runs"

    if requested_refresh:
        problem = "updater: requested refresh has not succeeded"
    if may_recover:
        try:
            if recover_updater(token, repo, runs):
                return
        except Exception as exc:
            problem += f"; recovery failed: {exc}"
        else:
            problem += "; recovery timed out"
    fails.append(problem)
    print(f"{problem} [STALE]")


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

    # Representative media checks against the committed source of truth. Pages serves
    # this file verbatim; checking its enclosure targets catches host/content failures.
    try:
        check_media(local_bytes, fails)
    except ET.ParseError as e:
        fails.append(f"media[local]: INVALID XML ({e})")
    if "Pages" in page_data:
        try:
            check_pages_enclosures(local_bytes, page_data["Pages"], fails)
        except ET.ParseError as e:
            fails.append(f"media[Pages]: INVALID XML ({e})")
    if "FeedBurner" in page_data:
        try:
            check_feedburner_enclosures(local_bytes, page_data["FeedBurner"], fails)
        except ET.ParseError as e:
            fails.append(f"media[FeedBurner]: INVALID XML ({e})")

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
