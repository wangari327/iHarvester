# MTProto recovery for old campaign posts

Use this only when iHarvester's normal Bot API cleanup reports `DELETE_NOT_ALLOWED` for many otherwise-accessible channels. The Bot API refuses messages after 48 hours, but this one-off recovery invokes Telegram's MTProto channel-delete method with **the existing iHarvester bot identity**.

It does **not** authenticate any human channel-owner account. That matters for a network with different channel owners: the one identity that already has access to all target channels is the iHarvester bot itself. It also does not search channel history or compare content. It reads iHarvester's saved `campaign_channel_state` and submits only those exact `(channel ID, message ID)` pairs.

Telegram documents `channels.deleteMessages` as available to both users and bots, with channel-admin errors rather than the Bot API's 48-hour deletion rule. This is therefore a controlled fallback, not an assumption that every channel can be reached. Start with ten channels and inspect the result before the full run. [Telegram MTProto method reference](https://core.telegram.org/method/channels.deleteMessages)

## One-time setup on your computer

1. Pull the latest `main` branch.
2. Create a Telegram API application at [my.telegram.org/apps](https://my.telegram.org/apps) and keep its `api_id` and `api_hash` private. These identify the API application; they do not need to be the owner of any campaign channel.
3. Create a separate local environment for the recovery. It is intentionally separate from Koyeb and from the application virtual environment.
4. In PowerShell, set the values from your existing Koyeb configuration. Do not commit them or paste them into chat.

```powershell
py -m venv .mtproto-recovery-venv
& .\.mtproto-recovery-venv\Scripts\python.exe -m pip install --upgrade pip
& .\.mtproto-recovery-venv\Scripts\python.exe -m pip install -e . -r requirements-mtproto-recovery.txt

$env:MONGODB_URI = "mongodb+srv://..."
$env:MONGODB_DB_NAME = "telegram_campaign_orchestrator"
$env:BOT_TOKEN = "123456:..."
$env:TELEGRAM_API_ID = "12345678"
$env:TELEGRAM_API_HASH = "..."
```

The Telethon session file is local-only under `work/` and is ignored by Git. Treat it like a password, then delete it when the recovery is finished.

## Safe recovery sequence

Use the campaign ID shown on the first line of **View cleanup issues** as `Recovery campaign ID` (after the latest bot deployment).

```powershell
# 1. Read-only: lists the exact first ten channel/message pairs.
& .\.mtproto-recovery-venv\Scripts\python.exe scripts\cleanup_campaign_mtproto.py --campaign cmp_YOUR_ID --limit 10

# 2. Delete only that ten-channel pilot and inspect those channels in Telegram.
& .\.mtproto-recovery-venv\Scripts\python.exe scripts\cleanup_campaign_mtproto.py --campaign cmp_YOUR_ID --limit 10 --confirm

# 3. If the pilot succeeds, process every remaining tracked post.
& .\.mtproto-recovery-venv\Scripts\python.exe scripts\cleanup_campaign_mtproto.py --campaign cmp_YOUR_ID --confirm
```

The tool uses a conservative four requests per second and obeys Telegram flood waits. Successful deletes are reconciled directly in Mongo as `CLEANED`; failed rows remain intact with an `mtproto_last_error`. When it completes, open the campaign and tap **Refresh dashboard**. Normal iHarvester cleanup will archive it once no tracked posts remain.

If the ten-channel pilot reports `CHAT_ADMIN_REQUIRED` or `CHANNEL_PRIVATE`, those are real access exceptions for that channel. Do not run the full recovery until the sample shows that the common bot identity can delete the old posts.
