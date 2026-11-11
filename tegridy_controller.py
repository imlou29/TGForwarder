"""Admin-confirmed, one-at-a-time Telegram backup jobs and 15-second live edits."""
import asyncio
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import Button
from telethon.errors import FloodWaitError, MessageNotModifiedError
from tegridy_state import (connect, selected, mappings, settings, register_run,
                          finish_run, recent_events, history)


class BackupJobs:
    def __init__(self):
        self.process = None
        self.task = None
        self.progress_task = None
        self.stop_requested = False
        self.chat_id = None
        self.message_id = None
        self.started = 0
        self.last_result = "No controller backup started since service restart."
        self.label = ""
        self.total_expected = None
        self.done_prior = 0
        self.phase = "Idle"
        self.last_log_path = Path("/data/tgforwarder/controller_last.log")
        self._guard = asyncio.Lock()

    def running(self):
        return self.task is not None and not self.task.done()

    def info(self):
        if self.running():
            return "🟢 " + self.phase + " — " + self.label
        return "⚪ No active controller backup.\nLast job: " + self.last_result

    def next_phase(self, db, route):
        row=db.execute("""SELECT initial_complete FROM batch_state
             WHERE source_chat=? AND source_topic=? AND target_chat=? AND target_topic=?""",
             (route["source_chat"],route["source_topic"],route["target_chat"],route["target_topic"])).fetchone()
        return "incremental" if row and row[0] else "initial"

    def selected_routes(self, admin_id):
        db=connect()
        try:
            wanted=selected(db,admin_id)
            return [r for r in mappings(db) if (r["source_chat"],r["source_topic"]) in wanted]
        finally:
            db.close()

    async def start(self, client, admin_id, message_id):
        async with self._guard:
            if self.running():
                return "⚠️ Another controller job is already running."
            db=connect()
            try:
                if settings(db,"auto_forwarding","off") == "on":
                    return "⚠️ Turn automatic forwarding OFF before running a manual batch."
                routes=[r for r in mappings(db) if (r["source_chat"],r["source_topic"]) in selected(db,admin_id)]
            finally:
                db.close()
            if not routes:
                return "⚠️ No linked topics selected. Open Topics and select at least one."
            self.stop_requested=False
            self.chat_id=admin_id
            self.message_id=message_id
            self.started=time.time()
            self.last_result="Job started"
            self.label=", ".join((r["name"] or "Topic "+str(r["source_topic"])) for r in routes[:4])
            if len(routes)>4:
                self.label+=" +"+str(len(routes)-4)
            self.phase="Preparing"
            self.total_expected=None
            self.done_prior=0
            self.task=asyncio.create_task(self._execute(client, routes, admin_id))
            self.progress_task=asyncio.create_task(self._progress_loop(client))
            return "✅ Backup accepted; preparing a read-only count before sending.\n" + self.label

    async def _subprocess(self, route, phase, execute, run_id=None):
        args=[sys.executable,"-u","/app/tegridy_batch.py",
              "--phase",phase,"--source-chat",str(route["source_chat"]),
              "--source-topic",str(route["source_topic"])]
        if execute:
            args.extend(["--execute","--delay","15"])
        env=os.environ.copy()
        if run_id is not None:
            env["BACKUP_RUN_ID"]=str(run_id)
        self.last_log_path.parent.mkdir(parents=True,exist_ok=True)
        log_handle=open(self.last_log_path,"ab",buffering=0)
        try:
            self.process=await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,
                env=env,start_new_session=True)
            chunks=[]
            while True:
                data=await self.process.stdout.readline()
                if not data:
                    break
                log_handle.write(data)
                if len(chunks)<2500:
                    chunks.append(data.decode("utf-8",errors="replace"))
            code=await self.process.wait()
            self.process=None
            return code,"".join(chunks)
        finally:
            log_handle.close()

    @staticmethod
    def _expected(log):
        match=re.search(r"SUMMARY .*matched=(\d+).*already_recorded=(\d+)",log)
        if not match:
            raise RuntimeError("Read-only preview did not return an eligibility summary")
        return max(0,int(match.group(1))-int(match.group(2)))

    async def _execute(self, client, routes, admin_id):
        result="Completed"
        try:
            expected=[]
            for route in routes:
                if self.stop_requested:
                    result="Stopped";break
                phase=self.next_phase_db(route)
                self.phase="Scanning "+(route["name"] or str(route["source_topic"]))
                code,output=await self._subprocess(route,phase,False)
                if code!=0:
                    raise RuntimeError("Preview failed for topic "+str(route["source_topic"])+": "+output[-450:])
                expected.append((route,phase,self._expected(output)))
            if result!="Stopped":
                self.total_expected=sum(x[2] for x in expected)
                self.done_prior=0
                for route,phase,count in expected:
                    if self.stop_requested:
                        result="Stopped";break
                    if count == 0:
                        self.done_prior+=0
                        continue
                    self.phase="Copying "+(route["name"] or str(route["source_topic"]))
                    db=connect()
                    try:
                        run_id=register_run(db,admin_id,route,phase)
                    finally:
                        db.close()
                    code,output=await self._subprocess(route,phase,True,run_id)
                    db=connect()
                    try:
                        sent=db.execute("SELECT copied FROM backup_runs WHERE id=?", (run_id,)).fetchone()[0]
                        finish_run(db,run_id,"completed" if code==0 else ("stopped" if self.stop_requested else "failed"),
                                   "" if code==0 else output[-300:])
                    finally:
                        db.close()
                    self.done_prior+=sent
                    if code!=0:
                        if self.stop_requested:
                            result="Stopped"
                        else:
                            flood=re.search(r"FLOOD WAIT (\d+)s",output)
                            result=("FloodWait "+flood.group(1)+"s — stopped" if flood else "Failed; see Jobs")
                        break
            self.last_result=result
        except Exception as exc:
            self.last_result="Failed: "+str(exc)[:180]
        finally:
            self.phase="Finished"
            await self._edit_progress(client,final=True)

    def next_phase_db(self,route):
        db=connect()
        try:
            return self.next_phase(db,route)
        finally:
            db.close()

    def current_copied(self):
        db=connect()
        try:
            # count events for all selected work in this job; old successful copies
            # are excluded by timestamp. The selected topic scope is enforced.
            routes=self.selected_routes(self.chat_id) if self.chat_id else []
            return sum(len(db.execute("""SELECT source_msg FROM copy_events
                  WHERE source_chat=? AND target_chat=? AND target_topic=? AND sent_at>=?""",
                 (r["source_chat"],r["target_chat"],r["target_topic"],self.started)).fetchall())
                       for r in routes)
        finally:
            db.close()

    def speed(self):
        db=connect()
        try:
            routes=self.selected_routes(self.chat_id) if self.chat_id else []
            times=[]
            for r in routes:
                times.extend(recent_events(db,r,self.started,20))
            times=sorted(times,reverse=True)[:20]
            if len(times)<2:
                return None
            elapsed=max(1.0,times[0]-times[-1])
            return 60.0*(len(times)-1)/elapsed
        finally:
            db.close()

    def progress_text(self, final=False):
        copied=self.current_copied() if self.started else 0
        speed=self.speed() if self.started else None
        lines=["📦 Tegridy Backup — Live Progress",
               "📍 "+self.label,
               "⚙️ "+(self.last_result if final else self.phase),
               "✅ Copied in this job: "+str(copied)]
        if self.total_expected is not None:
            remaining=max(0,self.total_expected-copied)
            lines.append("📋 Planned eligible copies: "+str(self.total_expected))
            lines.append("📈 Progress: "+f"{100*min(copied,self.total_expected)/max(1,self.total_expected):.1f}%")
            lines.append("📷 Remaining: "+str(remaining))
            if speed is not None and speed>0 and remaining>0 and not final:
                minutes=remaining/speed
                lines.append("⏳ ETA: approximately "+(f"{minutes:.0f} min" if minutes<180 else f"{minutes/60:.1f} h"))
        else:
            lines.append("📋 Eligible total: scanning…")
        lines.append("🚀 Recent rate: "+(f"{speed:.2f} copies/min" if speed is not None else "Calculating…"))
        if final:
            lines.append("🏁 Final job state; refresh Jobs for history.")
        else:
            lines.append("🔄 Updates every 15 seconds when changed.")
        return "\n".join(lines)

    async def _edit_progress(self,client,final=False):
        if not self.chat_id or not self.message_id:
            return
        text=self.progress_text(final)
        if getattr(self,"_last_text",None)==text:
            return
        try:
            buttons=[[Button.inline("🛑 Stop",b"tg:stop"),Button.inline("📋 Jobs",b"tg:jobs")]] if not final else [[Button.inline("📋 Jobs",b"tg:jobs"),Button.inline("🏠 Menu",b"tg:menu")]]
            await client.edit_message(self.chat_id,self.message_id,text,buttons=buttons)
            self._last_text=text
        except MessageNotModifiedError:
            self._last_text=text
        except FloodWaitError:
            # Never bypass a Telegram wait for cosmetic progress edits.
            pass
        except Exception:
            # A deleted message or lost edit permission must not stop the backup.
            pass

    async def _progress_loop(self,client):
        while self.running():
            await asyncio.sleep(15)
            await self._edit_progress(client)
        await self._edit_progress(client,final=True)

    async def stop(self):
        async with self._guard:
            if not self.running():
                return "No active controller backup."
            self.stop_requested=True
            if self.process and self.process.returncode is None:
                self.process.send_signal(signal.SIGINT)
            return "🛑 Stop requested; the current Telegram operation may finish first."

    def log_tail(self,cap=1100):
        try:
            with open(self.last_log_path,"rb") as f:
                f.seek(0,2);f.seek(max(0,f.tell()-20000))
                lines=f.read().decode("utf-8",errors="replace").splitlines()
            keep=[s for s in lines if any(x in s for x in ("COPY_EVENT","PROGRESS","SUMMARY","FLOOD WAIT","COPY FAILED"))]
            return "\n".join(keep[-6:])[-cap:] or "No recent events"
        except OSError:
            return "No controller logs recorded"

    def job_history(self):
        db=connect()
        try:
            rows=history(db,6)
            if not rows:
                return "📋 No controller job history yet."
            lines=["📋 Recent jobs"]
            for job,topic,phase,started,status,copied,error in rows:
                date=datetime.fromtimestamp(started,timezone.utc).strftime("%d %b %H:%M UTC")
                lines.append(f"#{job} · Topic {topic} · {phase} · {status}\n{date} · {copied} copied")
                if error:
                    lines.append("⚠️ "+str(error)[-130:])
            return "\n".join(lines)
        finally:
            db.close()
