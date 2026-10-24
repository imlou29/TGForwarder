# Tegridy topic backup (experimental, NOT deployed)

This branch adds `tegridy_forwarder.py` for live bot forwarding and `tegridy_backfill.py` for a separate one-time authorized-user historical import. It does not modify the upstream `telegram_forwarder.py` used by existing deployments.

## Scope

- Source: group `-1004350769620`, Touchdowns thread `501`
- Destination: group `-1003900751944`, Touchdowns thread `3962`
- Live: copy new messages from **only mapped topics**, regardless of content type where Telegram supports copying that media.
- Historical: import **only photos with nonempty captions** from Touchdowns; skip text-only and captionless photos.
- Other topics: no processing unless explicitly added to `TOPIC_ROUTES`.

## Railway variables (for future deployment, not yet ready to switch on)

```text
API_ID=<Telegram application API ID>
API_HASH=<Telegram application API hash>
BOT_TOKEN=<new forwarding bot token>
TOPIC_ROUTES=[{"source_chat":-1004350769620,"source_topic":501,"target_chat":-1003900751944,"target_topic":3962}]
BACKUP_DB_PATH=/data/tgforwarder/forwarded.sqlite3
BOT_SESSION_PATH=/data/tgforwarder/bot
```

Set the Railway start command to `python tegridy_forwarder.py` **only after testing topic IDs**. Attach persistent Railway storage to the new forwarder service at `/data`. `SOURCE_ID` and `TARGET_ID` are deliberately ignored by the new script, preventing accidental whole-group forwarding. Do not put any credentials in GitHub.

## Historical import (separate action)

`tegridy_backfill.py` requires `USER_SESSION_STRING`, obtained by a legitimate authorized Telegram user account through Telethon interactive login. A bot token alone cannot retrieve messages sent before the bot was added. Never commit/share a session string. Use the same persistent DB as the live process for deduplication. The backfill defaults to a **dry run**; only `BACKFILL_EXECUTE=yes` permits writes. `BACKFILL_LIMIT=5` caps a first test. Import chronology and Telegram media restrictions are subject to API behavior.

## Before deployment

1. Confirm `501` and `3962` refer to correct Telegram forum topic IDs with a live test.
2. Review Telegram permissions for both the bot and historical user account.
3. Check test messages: first post, direct reply, nested reply, image+caption, photo-only, plain text, media and album.
4. Verify destination thread routing, formatting/media fidelity, and handling of protected content.
5. Verify SQLite persistence and deduplication across service restarts and imports. Crash after Telegram accepts a message but before DB commit can still result in a duplicate on retry.
6. Check changes and restrictions on Telegram forum topics / Telethon pagination for `iter_messages(..., reply_to=...)`.
7. Do not run concurrent historical importer and live client until ordering/race behavior is tested.

**Limitations:** The backup is not an exact archive. Telegram assigns new timestamps/senders to copied messages. Message edits/deletes, pinned state, reactions, polls, some service events, media groups, and reply structure are not faithfully replicated by this first version. Users should not rely on this as the sole disaster-recovery copy.

## Adding new topics

Add another explicit entry to `TOPIC_ROUTES`. Historical import rules for a new topic must be specified separately; Touchdowns-only photo+caption policy is not a default for other topics.
