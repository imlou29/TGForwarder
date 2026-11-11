"""Telegram admin-only private controller UI.

Buttons mutate configuration ONLY after allowlist checks.
Backup sends remain manually confirmed; topic creation and automatic forwarding
require explicit second-step confirmation.
"""
import logging
import sqlite3
import time
from datetime import datetime,timezone

from telethon import Button, events
from telethon.errors import MessageNotModifiedError

from tegridy_state import (SRC,DST,FILTERS,connect,mappings,selected,set_selected,
                          settings,set_setting,count_copies,history)
from tegridy_topics import (discover,topic_report,candidate_destinations,
                           link_existing,create_missing)
from tegridy_verify import verify_topic

log=logging.getLogger("tegridy-ui")


def menu(db):
    auto=settings(db,"auto_forwarding","off")
    return [
        [Button.inline("📊 Status",b"tg:status"),Button.inline("📋 Jobs",b"tg:jobs")],
        [Button.inline("▶️ Start / Resume",b"tg:confirm"),Button.inline("🛑 Stop",b"tg:stop")],
        [Button.inline("📂 Topics / Select",b"tg:topics"),Button.inline("🔄 Sync Topics",b"tg:scan")],
        [Button.inline("⚡ Auto Forwarding: "+auto.upper(),b"tg:auto")],
        [Button.inline("⚙️ Filters",b"tg:filters"),Button.inline("ℹ️ Help",b"tg:help")]
    ]


def status_text(db,jobs):
    rows=mappings(db)
    lines=["📦 Tegridy Backup — Status",jobs.info()]
    lines.append("⚡ Automatic forwarding: "+settings(db,"auto_forwarding","off").upper())
    for route in rows[:15]:
        name=route["name"] or "Topic "+str(route["source_topic"])
        state=db.execute("""SELECT initial_complete,last_complete FROM batch_state
          WHERE source_chat=? AND source_topic=? AND target_chat=? AND target_topic=?""",
          (route["source_chat"],route["source_topic"],route["target_chat"],route["target_topic"])).fetchone()
        count=count_copies(db,route)
        lines.append(f"\n📍 {name} · {route['filter']}\n✅ Copies: {count} · Initial: "+("complete" if state and state[0] else "pending"))
    if len(rows)>15:
        lines.append(f"\n…and {len(rows)-15} more topics")
    lines.append("\nCounts are stored receipts; verify destination via Topic Details.")
    return "\n".join(lines)


def topics_text(db,admin,page=0):
    rows=mappings(db)
    chosen=selected(db,admin)
    start=page*8
    lines=[f"📂 Topics — {len(chosen)} selected", "Select one, multiple, or all."]
    buttons=[]
    for r in rows[start:start+8]:
        key=(r["source_chat"],r["source_topic"])
        mark="✅" if key in chosen else "⬜"
        title=r["name"] or "Topic "+str(r["source_topic"])
        lines.append(f"{mark} {title} · {r['filter']}")
        buttons.append([
            Button.inline(mark+" "+title[:24],f"tg:s:{r['source_topic']}:{page}".encode()),
            Button.inline("⚙️",f"tg:d:{r['source_topic']}".encode())])
    nav=[]
    if page>0: nav.append(Button.inline("◀️",f"tg:t:{page-1}".encode()))
    if start+8<len(rows): nav.append(Button.inline("▶️",f"tg:t:{page+1}".encode()))
    if nav: buttons.append(nav)
    buttons.append([Button.inline("☑️ All linked",b"tg:all"),Button.inline("⬜ Clear",b"tg:clear")])
    buttons.append([Button.inline("⚙️ Bulk filters",b"tg:bulk"),Button.inline("🏠 Menu",b"tg:menu")])
    return "\n".join(lines),buttons


def topic_details(db,topic):
    row=db.execute("""SELECT source_name,content_filter,exclude_fun,target_topic
         FROM topic_routes WHERE source_chat=? AND source_topic=?""",(SRC,topic)).fetchone()
    if not row:
        return "Topic not linked",[[Button.inline("⬅️ Topics",b"tg:topics")]]
    name,mode,exclude,dest=row
    lines=[f"⚙️ Topic: {name or topic}",f"Source topic ID: {topic}",
           f"Destination topic ID: {dest}",f"Content filter: {mode}",
           "Stickers/GIFs: "+("excluded" if exclude else "included if supported"),
           "Changing a completed topic filter marks it for historical rescan on next manual run."]
    buttons=[]
    for key,label in (("text","📝 Text"),("photos","📷 Photos"),
                      ("photo_caption","📸 Photo + caption"),("all","📦 All supported")):
        buttons.append([Button.inline(("✅ " if mode==key else "")+label,f"tg:f:{topic}:{key}".encode())])
    buttons.append([Button.inline("🚫 Toggle GIF/sticker exclusion",f"tg:x:{topic}".encode())])
    buttons.append([Button.inline("🔎 Verify last 100",f"tg:v:{topic}".encode())])
    buttons.append([Button.inline("⬅️ Topics",b"tg:topics")])
    return "\n".join(lines),buttons


def unlinked(db,page=0):
    report=topic_report(db)
    missing=report["missing"]
    lines=[f"🔄 Topic inventory: {report['matched']} linked / {report['total']} sources",
           f"Unlinked topics: {len(missing)}","Unmatched names are never guessed or created without confirmation."]
    buttons=[]
    for tid,name in missing[page*7:page*7+7]:
        buttons.append([Button.inline("🔗 "+name[:30],f"tg:u:{tid}".encode())])
    nav=[]
    if page: nav.append(Button.inline("◀️",f"tg:un:{page-1}".encode()))
    if (page+1)*7<len(missing): nav.append(Button.inline("▶️",f"tg:un:{page+1}".encode()))
    if nav: buttons.append(nav)
    buttons.append([Button.inline("🔄 Rescan",b"tg:scan"),Button.inline("🏠 Menu",b"tg:menu")])
    return "\n".join(lines),buttons


def link_choices(db,topic,page=0):
    source=db.execute("SELECT title FROM topic_inventory WHERE chat=? AND topic=?",(SRC,topic)).fetchone()
    if not source:
        return "Topic not found; rescan first.",[[Button.inline("↩️ Sync",b"tg:un:0")]]
    options=candidate_destinations(db,topic)
    lines=[f"🔗 Link: {source[0]}", "Select an existing destination, or create a new topic.",
           "Matching is only automatic when names are unique and exact."]
    buttons=[]
    for tid,title in options[page*6:page*6+6]:
        buttons.append([Button.inline(title[:32],f"tg:lkq:{topic}:{tid}".encode())])
    nav=[]
    if page: nav.append(Button.inline("◀️",f"tg:lc:{topic}:{page-1}".encode()))
    if (page+1)*6<len(options):nav.append(Button.inline("▶️",f"tg:lc:{topic}:{page+1}".encode()))
    if nav:buttons.append(nav)
    buttons.append([Button.inline("➕ Create missing topic",f"tg:crq:{topic}".encode())])
    buttons.append([Button.inline("↩️ Unlinked",b"tg:un:0")])
    return "\n".join(lines),buttons


def install(client,jobs,authorized):
    confirmations={}
    async def render(event,text,buttons):
        try:
            await event.edit(text[:3900],buttons=buttons)
        except MessageNotModifiedError:
            pass

    @client.on(events.NewMessage(incoming=True))
    async def dm(event):
        if not event.is_private:
            return
        cmd=(event.raw_text or "").strip().split(maxsplit=1)
        command=cmd[0].split("@")[0].lower() if cmd else ""
        if command=="/myid":
            await event.respond("Your Telegram user ID: "+str(event.sender_id)+
                               "\nPanel access requires explicit admin authorization.")
            return
        if event.sender_id not in authorized:
            return
        if command in ("/start","/menu","/status","/help"):
            db=connect()
            try:
                text=("🤖 Tegridy Backup Controller\nManual backups and optional automatic forwarding." if command in ("/start","/menu")
                      else status_text(db,jobs) if command=="/status"
                      else help_text())
                await event.respond(text,buttons=menu(db))
            finally:
                db.close()

    @client.on(events.CallbackQuery())
    async def callbacks(event):
        if not event.is_private or event.sender_id not in authorized:
            await event.answer("Not authorized",alert=True)
            return
        data=(event.data or b"").decode("utf-8",errors="replace")
        if not data.startswith("tg:"):
            await event.answer("Unknown action",alert=True)
            return
        parts=data.split(":")
        action=parts[1]
        # Fast acknowledgement for slow Telegram topic enumeration.
        await event.answer()
        db=connect()
        try:
            if jobs.running() and action in ("f","x","bf","lk","cr","auto_on","scan"):
                raise ValueError("Stop the active manual backup before editing topic mappings, filters or forwarding mode")
            if action=="menu":
                await render(event,"🤖 Tegridy Backup Controller",menu(db))
            elif action=="status":
                await render(event,status_text(db,jobs),menu(db))
            elif action=="jobs":
                await render(event,jobs.info()+"\n\n"+jobs.job_history()+"\n\n"+jobs.log_tail(500),menu(db))
            elif action=="help":
                await render(event,help_text(),menu(db))
            elif action=="topics":
                await render(event,*topics_text(db,event.sender_id))
            elif action=="t":
                await render(event,*topics_text(db,event.sender_id,int(parts[2])))
            elif action=="s":
                topic=int(parts[2]);page=int(parts[3])
                rows=mappings(db)
                wanted=selected(db,event.sender_id)
                pair=(SRC,topic)
                if pair not in [(r["source_chat"],r["source_topic"]) for r in rows]:
                    raise ValueError("Topic is not linked")
                if pair in wanted: wanted.remove(pair)
                else: wanted.add(pair)
                set_selected(db,event.sender_id,wanted)
                await render(event,*topics_text(db,event.sender_id,page))
            elif action=="all":
                set_selected(db,event.sender_id,{(r["source_chat"],r["source_topic"]) for r in mappings(db)})
                await render(event,*topics_text(db,event.sender_id))
            elif action=="clear":
                set_selected(db,event.sender_id,set())
                await render(event,*topics_text(db,event.sender_id))
            elif action=="d":
                await render(event,*topic_details(db,int(parts[2])))
            elif action=="f":
                topic=int(parts[2]);mode=parts[3]
                if mode not in FILTERS:raise ValueError("Unsupported content filter")
                if topic==501 and mode!="photo_caption":
                    raise ValueError("Touchdowns is locked to photos with written captions")
                with db:
                    db.execute("""UPDATE topic_routes SET content_filter=? WHERE source_chat=? AND source_topic=?""",
                               (mode,SRC,topic))
                    db.execute("""INSERT OR IGNORE INTO topic_rescan VALUES (?,?)""",(SRC,topic))
                await render(event,*topic_details(db,topic))
            elif action=="x":
                topic=int(parts[2])
                if topic==501:
                    raise ValueError("Touchdowns excludes GIFs and stickers")
                with db:
                    db.execute("UPDATE topic_routes SET exclude_fun=1-exclude_fun WHERE source_chat=? AND source_topic=?",(SRC,topic))
                    db.execute("INSERT OR IGNORE INTO topic_rescan VALUES (?,?)",(SRC,topic))
                await render(event,*topic_details(db,topic))
            elif action=="bulk":
                await render(event,"⚙️ Apply filter to selected topics (Touchdowns remains photo+caption).",
                    [[Button.inline(label,f"tg:bf:{mode}".encode())] for mode,label in
                    (("text","📝 Text"),("photos","📷 Photos"),("photo_caption","📸 Photo + caption"),("all","📦 All supported"))]+
                    [[Button.inline("↩️ Topics",b"tg:topics")]])
            elif action=="bf":
                mode=parts[2]
                if mode not in FILTERS:raise ValueError("Unsupported filter")
                wanted=selected(db,event.sender_id)
                with db:
                    for sc,st in wanted:
                        if st==501:continue
                        db.execute("UPDATE topic_routes SET content_filter=? WHERE source_chat=? AND source_topic=?",(mode,sc,st))
                        db.execute("INSERT OR IGNORE INTO topic_rescan VALUES (?,?)",(sc,st))
                await render(event,f"✅ Applied {mode} to {max(0,len(wanted)-int((SRC,501) in wanted))} topics. Touchdowns unchanged.",menu(db))
            elif action=="auto":
                mode=settings(db,"auto_forwarding","off")
                if mode=="on":
                    set_setting(db,"auto_forwarding","off")
                    await render(event,"🔴 Automatic forwarding OFF. Manual backups are still available.",menu(db))
                elif jobs.running():
                    raise ValueError("Stop manual backup before enabling automatic forwarding")
                else:
                    await render(event,"⚠️ TURN AUTOMATIC FORWARDING ON?\n\nNew eligible messages from linked topics will be copied as they arrive. No historical backup is started. Telegram's Restrict Saving Content protection is respected. Stop forwarding before manual batches.",
                        [[Button.inline("✅ CONFIRM ON",b"tg:auto_on"),Button.inline("↩️ Cancel",b"tg:menu")]])
            elif action=="auto_on":
                if jobs.running():raise ValueError("Manual backup active")
                set_setting(db,"auto_forwarding","on")
                await render(event,"🟢 Automatic forwarding ON for linked topics. Only new eligible messages are copied.",menu(db))
            elif action=="confirm":
                if jobs.running():raise ValueError("A backup is already active")
                if settings(db,"auto_forwarding","off")=="on":
                    raise ValueError("Turn automatic forwarding OFF first")
                chosen=[r for r in mappings(db) if (r["source_chat"],r["source_topic"]) in selected(db,event.sender_id)]
                if not chosen:raise ValueError("Select at least one linked topic in Topics")
                confirmations[event.sender_id]=time.monotonic()+90
                labels=", ".join((r["name"] or str(r["source_topic"])) for r in chosen[:7])
                await render(event,"⚠️ CONFIRM MANUAL BACKUP\n\nTopics: "+labels+
                             "\nFilters apply per topic.\nTelegram content protection must be disabled for authorized backup windows.\nA read-only preview runs first.\nConfirm within 90 seconds.",
                             [[Button.inline("✅ CONFIRM START",b"tg:launch"),Button.inline("↩️ Cancel",b"tg:menu")]])
            elif action=="launch":
                until=confirmations.pop(event.sender_id,0)
                if time.monotonic()>until:raise ValueError("Confirmation expired")
                outcome=await jobs.start(client,event.sender_id,event.message_id)
                await render(event,outcome,[[Button.inline("📊 Progress",b"tg:status"),Button.inline("🛑 Stop",b"tg:stop")]])
            elif action=="stop":
                answer=await jobs.stop()
                await render(event,answer,menu(db))
            elif action=="scan":
                if jobs.running():raise ValueError("Stop the manual backup before topic discovery")
                await render(event,"🔎 Scanning topic lists from both groups; no posts will be sent.",[[Button.inline("🏠 Menu",b"tg:menu")]])
                db.close()
                report=await discover()
                db=connect()
                await render(event,*unlinked(db))
            elif action=="un":
                await render(event,*unlinked(db,int(parts[2])))
            elif action=="u":
                await render(event,*link_choices(db,int(parts[2])))
            elif action=="lc":
                await render(event,*link_choices(db,int(parts[2]),int(parts[3])))
            elif action=="lkq":
                st=int(parts[2]);dt=int(parts[3])
                await render(event,f"⚠️ Confirm linking source topic {st} to existing destination topic {dt}. No messages will be copied.",
                    [[Button.inline("✅ Confirm link",f"tg:lk:{st}:{dt}".encode()),Button.inline("↩️ Cancel",f"tg:u:{st}".encode())]])
            elif action=="lk":
                st=int(parts[2]);dt=int(parts[3])
                name,target=link_existing(db,st,dt)
                await render(event,f"✅ Linked {name} → {target}. Default backup filter is text only.",menu(db))
            elif action=="crq":
                st=int(parts[2])
                await render(event,f"⚠️ CREATE A NEW DESTINATION TOPIC for source topic {st}?\nRequires Manage Topics rights. The bot will rescan to prevent duplicates. No messages will be copied.",
                    [[Button.inline("✅ CONFIRM CREATE",f"tg:cr:{st}".encode()),Button.inline("↩️ Cancel",f"tg:u:{st}".encode())]])
            elif action=="cr":
                st=int(parts[2])
                if jobs.running():raise ValueError("Stop the manual backup first")
                await render(event,"Creating one confirmed missing topic. Do not press Create again.",[[Button.inline("🏠 Menu",b"tg:menu")]])
                db.close()
                name,tid=await create_missing(st)
                db=connect()
                await render(event,f"✅ Created and linked {name} (destination topic {tid}). Default filter: text.",menu(db))
            elif action=="v":
                st=int(parts[2])
                if jobs.running():raise ValueError("Stop the manual backup before verification")
                await render(event,"🔎 Verifying 100 latest saved receipts (read-only)…",[[Button.inline("🏠 Menu",b"tg:menu")]])
                db.close()
                answer=await verify_topic(SRC,st,100)
                db=connect()
                await render(event,answer,menu(db))
            else:
                await render(event,"Unknown command",menu(db))
        except Exception as exc:
            log.exception("Admin controller action failed: %s",action)
            try:
                await render(event,"⚠️ "+str(exc)[:300],[[Button.inline("🏠 Menu",b"tg:menu")]])
            except Exception:
                pass
        finally:
            db.close()


def help_text():
    return ("ℹ️ Backup Controller\n\n"
            "Topics: select one/multiple/all and choose each filter.\n"
            "Sync Topics: discover and safely link topics, or explicitly create missing ones.\n"
            "Start / Resume: requires confirmation; only runs when automatic forwarding is OFF.\n"
            "Auto Forwarding ON/OFF: only new eligible messages; always OFF after initial setup.\n"
            "Status, Jobs: saved copies and job history.\n"
            "Verify: read-only check of recent destination receipts.\n"
            "Touchdowns always backs up photos with written captions; stickers/GIFs excluded.\n"
            "Manual backups use a 15-second pre-send delay and stop for Telegram FloodWait.\n"
            "Do not disable Telegram Restrict Saving Content outside authorized backup windows.\n"
            "All three allowed admins can operate these controls.")
