# How to roll back / recover the Roderick on the Line feed

This repo hosts the show's RSS feed (`feed.xml`) on GitHub Pages. FeedBurner's
"Original Feed" points here; subscribers use the unchanged
`feeds.feedburner.com/RoderickOnTheLine`. In every scenario below **the public URL
never changes and no episode GUID ever changes**, so no subscriber loses the show or
re-downloads the catalog.

## The one setting that changed

| FeedBurner → Edit Feed Details | Before | Now |
|---|---|---|
| **Original Feed** | `http://www.merlinmann.com/roderick/rss.xml` | `https://merlinmann.github.io/rotl-feed/feed.xml` |

Squarespace settings were never touched. A full rollback = restore that one value.

## A. Put it back exactly as it was

1. <https://feedburner.google.com> (merlinmann@gmail.com)
2. **Roderick on the Line** → **Edit Feed Details**
3. **Original Feed** → `http://www.merlinmann.com/roderick/rss.xml`
4. **Save.** Within one poll cycle FeedBurner reads Squarespace again. Not broken — just
   back to the prior (occasionally truncating) setup. The static feed stays hosted, so you
   can re-point forward later with no rebuild.

## B. New episodes stop appearing (cron failed)

The feed keeps serving everything it has; it just goes stale at the top.
- **Actions** tab → *Update ROTL feed* → **Run workflow**, or locally
  `python3 update.py && git commit -am refresh && git push`.
- Stopgap: do (A); Squarespace shows the newest episode at the top.

## C. GitHub Pages down / unreachable

FeedBurner keeps serving its last cached copy. If it stays down: do (A), **or** re-host
`feed.xml` (this repo, or its git history) on any static host (S3, Netlify, etc.) and set
that URL as Original Feed.

## D. This repo / account lost

`feed.xml` lives here, in this repo's git history, and a copy is kept in Merlin's private
hub (`rotl/reference/rotl-feed-complete.xml`). Re-host any copy and point Original Feed at
it. Rebuild from scratch with the hub's `rotl/scripts/build-rotl-feed.py`.

## E. FeedBurner shuts down

Independent of this setup (would hit the old one too). The feed is a portable file you
own — stand it up at a URL you control and migrate with (F).

## F. Leave FeedBurner on purpose

Add `<itunes:new-feed-url>https://your-url/feed.xml</itunes:new-feed-url>` to the channel.
Apple and compliant apps move subscribers over a few weeks. Don't remove the FeedBurner
feed until migration settles — some apps ignore `new-feed-url`.

---
Full background and diagnosis: see Merlin's hub, `rotl/reference/feed-migration-2026-06-02.md`.
