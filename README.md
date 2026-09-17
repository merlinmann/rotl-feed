# rotl-feed

A clean, complete, spec-valid RSS feed for **[Roderick on the Line](https://www.merlinmann.com/roderick)**,
hosted on GitHub Pages and kept current by a GitHub Actions cron.

**Live feed:** <https://merlinmann.github.io/rotl-feed/feed.xml>

## Why this exists

Squarespace V5 renders the whole feed on each request and truncates it mid-`<item>`
once the catalog is large enough — invalid XML, frozen podcast directories. This repo
replaces that flaky generator with a static file we control, while the public
subscriber URL (`feeds.feedburner.com/RoderickOnTheLine`) stays unchanged: FeedBurner's
"Original Feed" simply points here instead of at Squarespace.

Full background, architecture diagram, and **rollback steps** live in the hub:
`rotl/reference/feed-migration-2026-06-02.md`.

## How it stays current

- `feed.xml` — the complete feed (all 631 episodes, every GUID preserved, real
  `<enclosure length>` on each). Built once; this file is the source of truth.
- `update.py` — hourly via `.github/workflows/update.yml`: fetches Squarespace's live
  feed, adds any episode whose GUID isn't already present, validates, commits. No new
  episodes → no change. **Runs on GitHub's servers; no local machine involved.**
- Posting a new episode to Squarespace is the only manual step, and it's the one you
  already do.

## Manual refresh

Actions tab → **Update ROTL feed** → **Run workflow**. Or locally: `python3 update.py`.

## Health checks and delayed scheduled runs

The health workflow checks the feed, show notes, and representative audio files.
GitHub sometimes delivers the scheduled updater runs hours apart, despite the
15-minute cron. When the last successful main-branch update is over three hours
old, the health check requests one update and waits up to five minutes for a new
successful completion. If an update is already queued or running, it waits for
that run. Failed recovery still fails the health workflow and sends the usual
GitHub notification. Feed and audio failures remain failures even after recovery.

The health workflow has Actions write permission for this dispatch; its contents
permission remains read-only. Local checks never dispatch updates. To exercise
recovery on demand, run **ROTL feed health** with **refresh_updater** enabled.
The workflow runs the regression tests before checking the live feed.
