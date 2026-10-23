# Bot fleet promotion

The **Bot fleet** button exists only in the primary iHarvester bot's private owner menu. It is intentionally absent from creator clones and cannot be reached through a command.

It adds the configured approved bot usernames to **one channel at a time**. The owner enters a numeric channel ID, iHarvester verifies that it is already a channel administrator, then asks for an explicit confirmation. This creates one compact MongoDB job containing the requested bot names and their final outcomes.

The Bot API cannot promote unrelated bots. A small private worker therefore runs on the recovery VPS using the three already-authorised human Telegram sessions. For each job it:

1. Resolves the exact stored channel ID for each authorised account, using the registered public username when available, without inspecting channel history.
2. Verifies that any resolved peer is the requested channel ID.
3. Uses only an account Telegram permits to grant administrators; this requires Telegram's **Add admins** permission.
4. Promotes only the approved fleet bots, sequentially and with a delay.
5. Requests **post messages**, **edit messages**, **delete messages**, and **Invite Users via Link** rights. Telegram only permits a human administrator to delegate rights it already holds, so a limited acting account grants its valid subset and the job records the omitted rights. It never grants channel-management or the ability to add other administrators.

The worker never logs in, exports, or deletes a human session. It does not need any bot token for the fleet bots.

## Configuration

`BOT_FLEET_USERNAMES` is an optional comma-separated override in the primary deployment. Names may include `@`; iHarvester normalizes and de-duplicates them. The repository's default is the approved fifteen-bot fleet. This setting has no effect in clones.

## VPS worker

Install the recovery-only dependencies in the existing recovery checkout, then run the job worker with all three preserved sessions:

```bash
cd /root/iharvester-recovery/repo
source /root/iharvester-recovery/venv/bin/activate
python scripts/promote_bot_fleet_mtproto.py \
  --session /root/iharvester-recovery/session/account-1 \
  --session /root/iharvester-recovery/session/account-2 \
  --session /root/iharvester-recovery/session/account-3 \
  --once
```

It reads `MONGODB_URI`, `MONGODB_DB_NAME`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH` from the VPS-only environment. Do not put those human-account credentials, session files, or API values into Koyeb.

Use `--dry-run` to list waiting fleet jobs without opening Telegram or writing data. A systemd timer may call the `--once` command every few minutes. The Telegram UI shows per-bot completion, already-admin, pending, failed, or cancelled state.

The repository includes ready-to-copy systemd units in [`deploy/iharvester-bot-fleet.service`](../deploy/iharvester-bot-fleet.service) and [`deploy/iharvester-bot-fleet.timer`](../deploy/iharvester-bot-fleet.timer). They use the existing VPS-only environment and preserved session paths, run one channel per invocation, and take a non-blocking lock so they never overlap.

If every authorised account lacks **Add admins** in a chosen channel, the job safely fails without changing the channel. For the complete fleet profile, at least one authorised account must itself hold **Add admins**, **Post messages**, **Edit messages**, **Delete messages**, and **Invite Users via Link**. If it has only **Add admins** and **Invite Users via Link**, the worker can still add the fleet and grant invite-link access, but it cannot legally grant the other three rights.
