# KBO English

A Bluesky bot posting English-language KBO League (Korean baseball) updates.

Posts to [@kbo-english.bsky.social](https://bsky.app/profile/kbo-english.bsky.social).

## Post types

- schedule — a pre-game thread: tonight's matchups and start times (KST), with
  the probable starting pitchers threaded underneath (morning).
- live — one box score per game, as soon as it goes final. Held for up to 45
  minutes for KBO to publish the crowd figure, then posted without it.
- results — a nightly final-scores digest, with a compact box score threaded per
  game, once every game on the slate is final.
- standings — a daily rank / W-L / games-back table, held until the night's
  results digest has gone out so the scores always lead the table.
- leaders — a weekly season-leaders thread, top 5 in each core hitting and
  pitching stat (Mondays, a league off-day).

Data comes from Naver Sports' public JSON API and the KBO English site. Team
names use a stable 2-letter code; pitcher and leader names are romanised from the
KBO English pages and cached in kbo_roster.json. Dedup is by (mode, date) in
kbo_history.json, so each card posts at most once per day.

## When it posts

The launchd plists in ~/Library/LaunchAgents are the authority; this is the
shape of them. The three polling modes share a run lock, so they are spaced five
minutes apart and never queue behind each other:

    live       15:00 - 01:00, every 15 minutes, on the hour
    results    15:05 - 01:05, every 15 minutes, at :05 past
    standings  15:10 - 01:10, every 15 minutes, at :10 past

The window opens at 15:00 because the earliest start seen this season is 13:00
(March) and a fast game runs about two and a half hours; it closes at 01:00 for
extra innings. A poll past midnight looks back a day, since a night's games
belong to the date they started on, and stops once that night's roundup has
gone out.

## Scripts

- kbo_post.py <mode> — post one card; mode is schedule, results, standings or
  leaders.
- kbo_roster_build.py — refresh kbo_roster.json, the pcode-to-English-name table.
- kbo_attendance_timing_check.py — a temporary verification harness (July 2026),
  to be removed once its question is answered.

## Setup

    pip install -r requirements.txt

    # Bluesky app password in the macOS Keychain:
    security add-generic-password -a "kbo-english.bsky.social" -s "kbobot-bluesky" -w

## Usage

    python3 kbo_post.py schedule            # or results / standings / leaders
    python3 kbo_post.py results --dry-run
    python3 kbo_roster_build.py             # refresh the roster name cache

## Notes

- kbo_history.json, kbo_results_history.json and kbo_roster.json are gitignored
  (rebuildable state/cache). The Bluesky credential lives in the macOS Keychain.
- No API keys: every data source is public and unauthenticated.

## License

MIT — see [LICENSE](LICENSE).
