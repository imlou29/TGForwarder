"""Read-only verification of saved Telegram copy receipts against actual destination messages."""
import fcntl
import os
from pathlib import Path

from tegridy_state import connect
from tegridy_topics import user_client, locked_user_session


async def verify_topic(source_chat, source_topic, limit=100):
    # No writes, and no competing scan against an active backup user session.
    handle=locked_user_session()
    try:
        db=connect()
        try:
            route=db.execute("""SELECT target_chat,target_topic FROM topic_routes
                                WHERE source_chat=? AND source_topic=? AND enabled=1""",
                             (source_chat,source_topic)).fetchone()
            if not route:
                raise ValueError("Topic has no configured destination")
            target_chat,target_topic=route
            rows=db.execute("""SELECT f.source_msg,f.target_msg FROM forwarded f
                WHERE f.source_chat=? AND f.target_chat=? AND f.target_topic=?
                AND f.target_msg IS NOT NULL ORDER BY f.source_msg DESC LIMIT ?""",
                (source_chat,target_chat,target_topic,limit)).fetchall()
        finally:
            db.close()
        if not rows:
            return "📋 No recorded copies to verify for this topic."
        client=user_client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Dedicated user session not authorized")
            target_ids=[int(row[1]) for row in rows]
            source_ids=[int(row[0]) for row in rows]
            missing=0; mismatch=0; verified=0
            for start in range(0,len(rows),100):
                slice_rows=rows[start:start+100]
                originals=await client.get_messages(source_chat,ids=[int(x[0]) for x in slice_rows])
                copies=await client.get_messages(target_chat,ids=[int(x[1]) for x in slice_rows])
                for original,copy in zip(originals,copies):
                    if not copy:
                        missing+=1
                    elif not original:
                        # Source removed or unavailable; destination existence still confirmed.
                        verified+=1
                    elif bool(original.photo)!=bool(copy.photo) or (original.message or "")!=(copy.message or ""):
                        mismatch+=1
                    else:
                        verified+=1
        finally:
            await client.disconnect()
        text=f"🔎 Topic verification (latest {len(rows)} recorded copies)\n✅ Present/content consistent: {verified}\n⚠️ Missing at destination: {missing}\n⚠️ Content mismatches: {mismatch}"
        if limit<len(rows):
            text+="\nThis is a sample, not a complete audit."
        text+="\nRead-only: no messages, receipts or checkpoints changed."
        return text
    finally:
        fcntl.flock(handle,fcntl.LOCK_UN)
        handle.close()
