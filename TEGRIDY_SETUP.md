# Tegridy backup — manual batches (experimental)

The source and destination are:
- **Source:** group `-1004350769620`, Touchdowns topic `501` (source ID confirmed by a diagnostic incoming-message log).
- **Destination:** group `-1003900751944`, Touchdowns topic `3962` (destination **not yet validated by a successful send**).
- Additional topics require individually approved routing and eligibility policies.

## Current production-safe configuration

The Railway service `TGForwarder` (project `adequate-respect`) runs `python tegridy_forwarder.py` with `TG_DIAGNOSTIC_ONLY=true`. It is **NOT a backup worker** in this state; it only authenticates and logs incoming event metadata. **Do not turn this off for manual batches.** OpenClaw is a separate Railway project and is not involved.

Railway volume `/data` holds SQLite state. API_ID, API_HASH and BOT_TOKEN are already stored as Railway service variables. `SOURCE_ID` / `TARGET_ID` are ignored by the topic-specific script.

## Manual workflow (first batch in progress / resumable)

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

The initial batch stopped on 2026-10-24 after a Telegram FloodWait of 1654 seconds, having copied 3 photos earlier and 297 in the next run (approximately 300 registered). Respect the Telegram wait fully before attempting another send. The SQLite `forwarded` table already records successful sends, while `batch_state` freezes the initial upper bound. **Do not delete either table or reinitialize the persistent volume.** Use `/app/.venv/bin/python /app/tegridy_batch.py --phase initial --execute` to resume after the wait; the new default pacing applies. Do not invoke parallel backups. `PROGRESS` is logged every 10 sends, while `SUMMARY` is printed on termination. Telegram `Restrict Saving Content` must be temporarily disabled by group admins during manual copy windows.
