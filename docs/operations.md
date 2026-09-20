# Operations

`/healthz` confirms that the process is alive. `/readyz` additionally verifies MongoDB and startup initialization; it does not require an active campaign.

The scheduler uses one short MongoDB lease. A deployment overlap may run two processes briefly, but only the current lease holder creates campaign cycles or begins ending transitions. Delivery claims also have MongoDB leases; a crash simply makes expired work claimable again.

Delivery statuses distinguish permanent failures, retry waits, and `UNKNOWN_SEND_STATE`. The last is deliberately not auto-retried because a Telegram request timeout has no idempotency key. Correct permission problems by re-promoting the bot, then wait for the next campaign cycle or use the owner retry control where eligible.

Keep normal broadcast rates at or below the conservative free defaults. Koyeb Free reaches scale-to-zero after one hour without public Internet traffic; its health checks are not an uptime mechanism. An external request to `/` every 5–10 minutes can keep a hobby deployment awake, but an always-on instance is the reliable scheduler option.

For future campaigns, the owner must complete the visible **Schedule for …** confirmation after entering the timing. The timestamp is interpreted in the timezone shown by the wizard and saved as UTC. The scheduler catches a process that wakes late while the campaign is still in its window. If the entire window elapsed while an otherwise untouched scheduled campaign had no first cycle, it is rebased to an equivalent fresh window rather than silently archived. This is recovery, not an exact-time guarantee: use an always-on service (or keep the instance awake) when the start must be punctual.

Client progress pages are bearer links: send them only to the intended promoter. They show aggregate campaign statistics only and poll every 10 seconds; they deliberately do not expose channel titles, IDs, source counts, or failure details. Revoke all links from the campaign when the client relationship ends. Client rerun and new-promotion requests remain approval-gated; the owner must confirm payment and approve from **Client requests** before a copy is scheduled or a submitted draft can be launched.

Manual variant sharing requires inline mode to be enabled once through `@BotFather` with `/setinline`. The application logs a warning at startup when this setting is absent, and the Share manually screen gives the same corrective instruction instead of generating an unusable code. Inline queries are subscribed automatically in polling and webhook modes.

Each code resolves an immutable creative snapshot and is accepted only from an ID in `OWNER_USER_IDS`. Telegram caching is disabled for these results. Revocation takes effect on the next lookup; revoked records receive a 30-day MongoDB TTL. Campaign deletion removes all associated share snapshots. The downstream broadcast bot must deliberately preserve the incoming `reply_markup` when copying the post—ordinary forwarding or a bot that discards markup will lose the buttons, so iHarvester also exposes a row-preserving CTA manifest.

The Network **Top channels by subscribers** view ranks the 15 or 30 largest stored numeric `member_count` values. Counts are updated when a channel is registered or refreshed; opening the ranking does not call Telegram for every channel. Channels without a verified count are omitted and reported separately so unknown values are never presented as zero.
