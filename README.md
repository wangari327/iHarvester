# iHarvester

iHarvester is a Telegram-native campaign orchestrator for an owner-operated network of channels. It registers channels when the bot becomes an administrator, stores durable state in MongoDB, and runs rate-limited, restart-safe campaign cycles with real Telegram buttons.

It deliberately remains one Python service plus MongoDB: no Redis, Celery, dashboard, redirect tracker, user-account login, or separate worker deployment.

If a past campaign's cleanup is blocked by Telegram's Bot API 48-hour limit, use the workstation-only [MTProto recovery guide](docs/mtproto-recovery.md). It authenticates the existing bot identity—not a channel owner's personal account—and deletes only iHarvester's exact tracked message IDs after a small pilot succeeds.

## The quickest production path: Koyeb

1. Create a MongoDB Atlas database and copy its connection URI.
2. Push this repository to GitHub, then create a **Web Service** in the Koyeb UI from the repository. Koyeb will detect the Dockerfile.
3. Under **Exposed ports**, select `8000` with protocol `HTTP`; Koyeb supplies that value as `PORT`. Choose an always-on paid instance with one minimum instance. Koyeb Free scales to zero after idle time, which is unsuitable for a campaign scheduler.
4. Add `BOT_TOKEN`, `OWNER_USER_IDS`, `MONGODB_URI`, `WEBHOOK_PATH_SECRET`, and `WEBHOOK_SECRET_TOKEN` as Koyeb Secrets. Set `RUN_MODE=webhook` and `MONGODB_DB_NAME=telegram_campaign_orchestrator`.
5. Koyeb supplies `KOYEB_PUBLIC_DOMAIN`; iHarvester derives its public webhook base URL from it. Deploy and check `/readyz`.
6. In `@BotFather`, run `/setinline`, select the iHarvester bot, and set a placeholder such as `Enter an iHarvester share code`. This enables owner-only manual variant sharing; no additional environment variable is needed.
7. Open the bot in Telegram, press **Start**, and promote it to administrator in the source channels.

Koyeb's default TCP health check is sufficient; an HTTP `/healthz` check is optional if the port settings expose the customization control. The service sets its Telegram webhook on startup. Keep `WEB_CONCURRENCY=1` / one application worker; the MongoDB scheduler lease remains a safety net during deployment overlaps.

If you choose a Free Instance, Koyeb scales it to zero after one hour without **public** traffic. Its internal health probes do not prevent this. Configure an external monitor to request `https://<your-koyeb-domain>/` every 5–10 minutes, or use an always-on instance for reliable scheduling.

## Local or VPS polling mode

```text
cp .env.example .env
# fill BOT_TOKEN, OWNER_USER_IDS, MONGODB_URI
docker compose up -d
```

With `RUN_MODE=polling`, no domain or TLS certificate is required. The container exposes `/healthz` and `/readyz` at port 8000.

## Owner workflow

Everything is in the bot's private chat with an allowed owner ID:

1. Press **Create Campaign**, name the draft, and capture **Variant 1** as formatted text or a supported media post.
2. Choose **Text**, **Photo**, **Photo + caption**, **Video**, **Video + caption**, **Album**, or **Forward ready post**. Forwarded content retains its Telegram formatting and media IDs.
3. Add a CTA by entering its label and URL in separate prompts. Use **Add beside last** for a horizontal button or **Add new row** for a vertical one; labels are never rewritten or truncated by iHarvester.
4. Use **+ Add variant** for Variant 2, Variant 3, and so on. With two or more variants, **Mix + Rotate** becomes the default: the frozen audience is divided into count-balanced cohorts and every cycle advances each cohort to the next variant. **Manage variants** previews, replaces, adds CTA buttons to, or removes each variant independently. Standard mode deliberately uses Variant 1 only.
5. Add destinations, source targets (all active, tags, audience-size range/minimum, or manual IDs), mode, and schedule through their own guided controls. The Network screen shows active, unavailable, paused, and attention-required channel counts, accepts a forwarded channel post for manual repair/registration, and provides separate paginated rankings for every public and private channel. **Refresh all network stats** queues a durable, rate-limited background recheck of every channel's access and subscriber count; it preserves manual pauses and reports progress when Network is opened again.
6. Choose **Send campaign**, then pick a duration preset or enter a custom duration such as `45m`, `2h`, `3d`, or `1mo` (30 days). Choose an even repost interval, 1-20 **Specific times after launch** such as `1d, 4d, 6d`, or 1-20 **custom repost gaps** such as `1d, 3d, 2d`. Each repost replaces the previous campaign post. If a rotating plan is too short or too tightly packed to complete every variant safely, iHarvester re-spaces the cadence or extends the end and shows the exact adjustment before launch.
7. **Plan for later** records start and end times in the displayed owner timezone, then ends on a clear **Schedule for …** confirmation. That confirmation freezes the target snapshot and turns the draft into `SCHEDULED`; simply saving dates does not leave an invisible launch step. If a sleeping host wakes after an untouched schedule's whole window has passed, iHarvester gives that never-started campaign an equivalent fresh window and records the delay rather than silently ending it.
8. **Launch** freezes the active target snapshot and automatically excludes all known destination channel IDs.

Drafts show a compact setup checklist; running campaigns show delivery, timeline, and variant-coverage progress, latest-cycle reachability, failures, cleanup, live-post, and tracked-join counts. Content, CTA buttons, destinations, targets, timing, and end behavior remain editable while a campaign is a draft. During an active or paused run, an individual variant can be replaced for future rotation cycles. Text variants also offer **Repair live posts now**: after a preview and explicit confirmation, iHarvester queues durable, rate-limited in-place edits of only that variant's currently tracked messages. The dashboard reports edited, waiting, skipped, and failed repairs; a post that a newer repost has already replaced is skipped safely. Each planned cycle keeps its frozen content revision, and the window is extended automatically when the replacement needs more cycles to reach every frozen target. Scheduled campaigns can return to draft before they start.

Archived campaigns offer two separate reuse paths. **Run again now** preserves the prior content, CTA layout, destinations, target rules, mode, duration, exact cadence, and end behavior, then asks for one launch confirmation. **Edit a copy** opens the same prefilled configuration as a normal editable draft.

For client reporting, open an active, scheduled, ending, paused, or archived campaign and choose **Share client progress page**. Each generated private link exposes only that campaign's aggregate delivery, time, cycle, cleanup, and tracked-join progress; it never lists channels or network identifiers. The page polls for fresh progress every 10 seconds while it is open. A client can request the same promotion again at a preferred time or request a new promotion. New-promotion material is handed to the bot through a one-time Telegram link, then appears under **Client requests** for payment review. A paid rerun can be approved directly into a scheduled/active copy that retains the prior campaign definition; a paid new promotion becomes a reviewable draft with the submitted variants and the original campaign's audience rules. Client requests never launch or bill anything automatically.

Every supported variant also has **Share manually**. It creates an immutable, owner-only code tied to that exact content revision. Use the supplied chat chooser or type `@YourIHarvesterBot HV-XXXX-XXXX` in another broadcast bot's chat, then select the result to insert the frozen text/photo/video/document with its original entities and URL keyboard. Codes are reusable until revoked; editing a campaign creates a different snapshot instead of silently changing an old code. If the receiving bot strips keyboards when it republishes, use **Copy CTA fallback** to transfer the preserved row, label, and URL layout manually. Telegram inline mode cannot represent an album or video note as one selectable result, so save a single album item as its own photo/video variant when it needs manual inline sharing.

For fallback channel registration, forward a post from a source channel to the bot and select **Register/Refresh**. `/backup` creates a compressed core export; `/restore` validates an attached export and asks for confirmation before upserting it.

## Safety behavior worth knowing

- Every cycle gets a new deterministic HMAC dispatch order. A crash resumes the same cycle order; registration order is never broadcast order.
- Mix + Rotate freezes count-balanced cohorts, rotates variants by cohort, and independently reshuffles physical delivery order each cycle. Launch requires enough dispatch-safe cycles for every variant to visit every channel.
- Every delivery stores the selected variant revision. Replacing a live variant cannot mix old and new payloads inside one already-planned cycle.
- Reposts delete the known previous campaign message for that channel immediately before sending a replacement.
- End time/early end prevents future sends, cleans known live posts, then archives immutable results. An archive is repeated only by creating a new draft.
- End behavior can delete the final post at campaign end, retain it until a later campaign successfully replaces it in that channel, or retain it until manual cleanup. Overlapping active campaigns never delete one another's posts.
- A message-send timeout is recorded as `UNKNOWN_SEND_STATE`, not blindly retried, because Telegram can have accepted the send before a timeout reached the bot.
- Deletion cleanup uses a conservative 47-hour validation rule. A long campaign must repost within 47 hours if it promises cleanup.
- Automatic core backups coalesce the configured channel-growth and time triggers. Their enablement, threshold, and interval are editable in **Settings**.
- Manual-share lookups are restricted to `OWNER_USER_IDS`, returned with owner-personal zero-cache inline results, and remain frozen even if the source campaign is edited. Revoked records are purged automatically after 30 days; deleting the campaign removes its share records immediately.
- Generic HTTP access logging is disabled in production commands so the secret webhook path is not written to platform logs.

## Configuration

See [.env.example](.env.example). Secrets stay in the environment and are never placed into MongoDB or backups. Default delivery rates are 20 sends/sec and 25 combined API mutations/sec, below the normal free-broadcast ceiling.

## Development

```text
uv sync --locked --all-groups
uv run ruff check .
uv run pytest
uv run uvicorn app.main:app --reload
```

CI runs linting, tests, and a Docker image build. See [docs/operations.md](docs/operations.md) for operational behavior and the deployment guides for platform-specific steps.

## Deployment guides

- [Koyeb](docs/deploy-koyeb.md) (primary)
- [Render](docs/deploy-render.md)
- [Heroku](docs/deploy-heroku.md)
- [Railway](docs/deploy-railway.md)
- [VPS / Docker](docs/deploy-vps.md)
- [Backup and restore](docs/backup-restore.md)
