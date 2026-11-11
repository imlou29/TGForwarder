# Tegridy Backup Controller 2.0 — manual and optional automatic backups

The source and destination are:
- **Source:** group `-1004350769620`, Touchdowns topic `501` (source ID confirmed by a diagnostic incoming-message log).
- **Destination:** group `-1003900751944`, Touchdowns topic `3962` (the user confirmed successful copies on 2026-10-24).
- Additional topics are discovered and explicitly linked via the Telegram controller, with independent content filters.

## Current configuration

Railway service `TGForwarder` (project `adequate-respect`) runs `python tegridy_forwarder.py`. It provides a Telegram admin controller, manual backup subprocesses, and optional automatic forwarding. The **durable SQLite** setting `backup_settings.auto_forwarding` defaults to `off`; admins can explicitly turn it ON/OFF via Telegram with confirmation to enable. The old `TG_DIAGNOSTIC_ONLY` Railway variable is **legacy and not used by the new runtime switch**. Do not change it to enable automatic forwarding. **OpenClaw is a separate project and must not be touched.**

Railway volume `/data` holds SQLite state. API_ID, API_HASH and BOT_TOKEN are already stored as Railway service variables. `SOURCE_ID` / `TARGET_ID` are ignored by the topic-specific script.

## Manual workflow (October initial batch completed; future batches incremental)

1. Keep Telegram's **Restrict Saving Content** enabled during normal operations. The tool does not and cannot toggle Telegram group restrictions for you.
2. Open a **private Railway SSH session** into the dedicated TGForwarder service; run `python tegridy_auth.py` once, entering phone/code/optional 2FA **only into the SSH terminal**. It stores `/data/tgforwarder/user.session` on the existing persistent volume. Do not paste login credentials, codes or session files in ChatGPT, GitHub, variables, or deploy logs. Only a user account with legitimate access can enumerate old history.
3. On a backup day, admin temporarily disables Restrict Saving Content. **During that window, other members may also be able to save/forward messages.**
4. Run a read-only preview via a one-shot execution environment with access to the same `/data` SQLite volume, `TOPIC_ROUTES`, API credentials, and user session: `python tegridy_batch.py --phase initial`.
5. After reviewing preview counts, test a small batch: `python tegridy_batch.py --phase initial --execute --max-send 5`.
6. Verify the first five copied photos and their captions appear **in destination topic 3962**, not the general chat or another topic. Check logs for copy errors.
7. Rerun without the limit: `python tegridy_batch.py --phase initial --execute`. The persistent DB skips successfully copied messages. The initial scan is frozen to an upper message ID on first execute; an interrupted batch can resume. It marks initial complete only if scan reaches end without a send error or test limit.
8. Later sessions: `python tegridy_batch.py --phase incremental` (preview), then `python tegridy_batch.py --phase incremental --execute`. This includes ONLY new Telegram photos that contain a nonempty written caption after the initial cutoff, skipping already copied items.
9. Before turning protection back on, check log **SUMMARY** and review destination counts/content; if any failure occurred, fix and retry while content is accessible. Finally re-enable Telegram protection manually.

**Never switch the always-on Railway service start command to an executing batch without an explicit, separately controlled plan.** It should run once and exit, not loop/restart indefinitely. A Railway one-shot execution pathway still needs validation.

## Required environment variables

```text
TOPIC_ROUTES=[{"source_chat":-1004350769620,"source_topic":501,"target_chat":-1003900751944,"target_topic":3962}]
BACKUP_DB_PATH=/data/tgforwarder/forwarded.sqlite3
API_ID=<secret>
API_HASH=<secret>
USER_SESSION_PATH=/data/tgforwarder/user
```

For the monitoring service (not batch): `BOT_TOKEN`, `BOT_SESSION_PATH=/data/tgforwarder/bot`, `TG_DIAGNOSTIC_ONLY=true`.

## What the script does and does not guarantee

- **Every Touchdowns run, both initial and incremental**, selects only Telegram photos with a nonempty written caption. Pure text, captionless photos, video and files are excluded.
- Each **subsequent run** selects newer eligible photo-and-caption messages only. Shared `copy_message()` enforces this policy even if a caller makes a mistake.
- A durable SQLite copy table avoids most duplicates across interrupted runs. A crash between Telegram accepting a send and SQLite committing the receipt can still produce a duplicate.
- The scanned set is based on Telegram's `iter_messages(chat, reply_to=topic)` behavior. Confirm that first-level and nested forum posts, albums, and protected content work as expected **before a full batch**. The topic root post may not be included in thread enumerations.
- Failed or unsupported copies stop the batch rather than falsely advancing its completed checkpoint. Flood waits are not bypassed. Manual execution defaults to a **15-second pause before each outgoing photo**; `--delay` cannot be lower than 10 seconds in execute mode. Flood waits are surfaced rather than silently slept through, and the batch stops with the requested wait duration. These precautions do not guarantee the absence of Telegram limits.
- Message copies receive new timestamps / authors; exact original chronology metadata, reactions, edits, deleted messages, reply topology, polls, and grouped albums are not reliably preserved.
- Telegram's content protection may block some operations even during the requested window; do not use the program to evade content restrictions.
- Never run two batch jobs against the same DB at the same time. Keep continuous forwarding disabled.
- GitHub pushes to this branch may trigger Railway deployments; the existing start command remains diagnostic only.

## Security

Use a dedicated Telegram account with legitimate access. Authorize via `python tegridy_auth.py` in a PRIVATE Railway SSH terminal. Its volume-backed session is a sensitive authentication credential. Anyone with access to the volume or shell can potentially use this account. If compromised, terminate the active session in Telegram. If exposed, terminate the session in Telegram's active sessions.

## Adding topics

Add an explicit source/destination mapping to `TOPIC_ROUTES`, review topic IDs and backfill policy separately, and extend the batch selection before enabling. The Touchdowns-only historical rule must not silently apply to other topics.

## Resuming safely after a FloodWait

The initial batch encountered a Telegram FloodWait of 1654 seconds on 2026-10-24 after about 300 copies; the user later confirmed that the backup for that day completed successfully. The current saved copy count should be checked from SQLite before any later batch. Respect the Telegram wait fully before attempting another send. The SQLite `forwarded` table already records successful sends, while `batch_state` freezes the initial upper bound. **Do not delete either table or reinitialize the persistent volume.** Use `/app/.venv/bin/python /app/tegridy_batch.py --phase initial --execute` to resume after the wait; the new default pacing applies. Do not invoke parallel backups. `PROGRESS` is logged every 10 sends, while `SUMMARY` is printed on termination. Telegram `Restrict Saving Content` must be temporarily disabled by group admins during manual copy windows.

## Telegram admin controller (manual operation)

The private `@TegridyBackup_bot` panel authorizes ONLY Telegram IDs in `TG_CONTROLLER_IDS` (comma-separated, configured on the TGForwarder Railway service): `6570407118`, `8837667454`, `8454891650`. Buttons: **Status**, **Jobs**, **Start / Resume**, **Stop**, **Topics / Select**, **Sync Topics**, **Auto Forwarding ON/OFF**, **Filters**, and **Help**. Start requires **CONFIRM START** within 90 seconds; forwarding ON also requires confirmation. There is no schedule/cron. New installations start with auto forwarding OFF, and manual backups cannot start while auto forwarding is ON.

The controller starts a single subprocess using the existing volume-backed Telegram **user** session, photo-with-written-caption eligibility, and **15-second pre-send spacing**. After the first historical batch completes, the controller selects `incremental` automatically. On interruption it can restart against the original frozen cutoff, skipping SQLite receipts. The manual script and controller both acquire the same persistent `.batch.lock` advisory lock. A Telegram FloodWait halts the job and must expire before a new attempt; it is not bypassed.

**Important limitations:** The bot cannot change Telegram's `Restrict Saving Content` setting. Only start after an administrator manually disables that setting for the backup window. `Stop` interrupts its own subprocess; a Telegram request already underway may finish before the stop takes effect. The panel's progress counts come from SQLite; an estimated count of 407 was measured in an earlier dry run and is not continuously recalculated. The bot's current in-process job state is lost during Railway redeploy/restart. Do not redeploy while a backup is active. The service restart could interrupt a job; existing SQLite receipts remain. Telegram message fidelity and destination placement should be visually verified.


## Version 2.0 feature guide (2026-11-11)

1. **Live progress:** the button used for confirming a manual backup becomes the one progress message; the bot edits that same message every **15 seconds only when the content changes**, with no repeated notification stream. Shows per-job copies, dry-run planned total, % where known, recent 20-send speed, approximate ETA, FloodWait deadline and final result. Telegram may throttle edits; this does not affect copying.
2. **Multiple topics:** `Topics / Select` enables individual toggles, **All linked** and **Clear**. Each administrator's selection is stored in SQLite, but jobs freeze their selected routes at start. New/other topics must be linked first.
3. **Independent content filters:** `text` (plain chat text, including messages with link previews), `photos` (with or without caption), `photo_caption` (photo with nonempty written caption), and `all` (text, photos, videos, documents where supported). Stickers/GIFs/service messages are excluded by default; the exclusion can be toggled for other topics. **Touchdowns topic 501 is locked to photo_caption**, always excluding GIFs/stickers.
4. **Filter change after completion:** a changed topic filter marks `topic_rescan`; on next confirmed manual run, the controller scans the historical topic again **without deleting copy receipts**, skipping already-recorded messages. Topic-level checkpoints are separate in `batch_state`.
5. **Sync Topics:** scans both configured Telegram forum groups using the dedicated user session. Finds and automatically links only **unique exact normalized-name matches**; existing saved mappings always win. Ambiguous or unmatched topics are shown for an admin to explicitly link. Discovery never copies posts.
6. **Create missing:** admin chooses an unlinked source topic and clicks **Create missing topic** then **CONFIRM CREATE**. The controller rescans both groups to avoid duplicates and requires the dedicated user to have Telegram Manage Topics rights in the destination. If the outcome is uncertain, it does NOT blindly retry; rescan first.
7. **Automatic forwarding:** separate SQLite `auto_forwarding=off/on` runtime switch, toggled in the Telegram private menu. ON copies only newly observed eligible posts in linked topics, at 15-second pre-send spacing; it does NOT start a historical backup. OFF stops new transfers (a Telegram request already in progress may finish). Telegram's content-protection rules are never bypassed. Do not manually backfill while automatic forwarding is ON.
8. **History, recovery and verification:** `backup_runs`, `copy_events` and per-topic checkpoints persist on `/data`. A service restart marks an in-progress controller job `interrupted` rather than claiming completion; the next run resumes by receipts/checkpoints. `Topic Details → Verify last 100` compares recent source/destination messages by availability, media type and caption/text **read-only**. This is a sample integrity check, **not** a cryptographic check of photo bytes or a complete proof of all historical posts. A send accepted by Telegram just before SQLite writes its receipt may still duplicate after an unexpected crash.

**Manual starting precautions:** Telegram group `Restrict Saving Content` must be temporarily switched off by an authorized group administrator if it otherwise blocks copying; the bot does not manage this setting. Verify destination rights and topic mappings in the menu, then start a small authorized test batch. The original user session and volume must remain private. **No automatic job or any copy is started by deploying these code changes.**

**Scope limitation:** The configured source/destination *groups* are fixed to Tegridy and Tegridy Backup 3.0; topic discovery and creation operate within this pair. Arbitrary group-pair selection, exact preservation of original message authors/time/reactions/albums, independent auto-forward retry queues, and full-media cryptographic verification are not implemented.
