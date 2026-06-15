# notify-watcher idea backlog

Unbuilt feature ideas, salvaged from the retired TOMORROW-PROMPT.md before deleting it
(June 2026). The "two-way reply-button control" idea from that old list is already
shipped as the Discord Worker, so it is not repeated here. Some items below may already
be done; prune as you confirm.

- F4 visa wait estimator: keep each visa-bulletin value as history in state; on each
  alert (and a quarterly summary) add a line like "F4 advanced N days over the last M
  bulletins; at this pace, about X years to your priority date." Pure logic over data
  the visa topic already collects.
- "Now streaming" for watchlist movies: TMDb watch-providers endpoint (existing
  TMDB_API_KEY); push once when a watchlist film becomes streamable in the DO region,
  naming the service.
- Wikipedia picture of the day: the Wikimedia featured feed the learn topic already
  fetches includes the day's featured image; attach it to the learning push.
- Morning weather line on the digest: open the daily digest with "Today: 31C, rain 20%,
  UV 9" from Open-Meteo (already used by uv/marine/beach_day).
- Hurricane cone image on weather alerts: attach NHC's forecast-cone PNG to a
  watch/warning push. Confirm first that the per-storm graphic URL can be derived from
  the NHC ATOM feed entry.
- YouTube channel uploads: follow a configured channel list via each channel's free
  no-key RSS feed, one push per upload. Same pattern as the ios_release topic.
- Dominican baseball: MLB Stats API (free, no key) for a daily-digest line on a followed
  team's result, or milestone alerts for Dominican players in season.
- Word-of-the-day learn channel: a Wiktionary word-of-the-day feed, or a curated
  vocabulary data file, as a new rotation slot.
