"""Manual Telegram backup controller job manager.

Starts only from a confirmed and allowlisted private chat callback.
No background scheduling. No bypass of Telegram rate limits.
"""
import asyncio
import os
import signal
import sys
from pathlib import Path


class BackupJobs:
    def __init__(self):
        self.process = None
        self.watch_task = None
        self.last_result = "No backup started from Telegram yet."
        self.last_log_path = Path("/data/tgforwarder/controller_last.log")
        self._guard = asyncio.Lock()

    def running(self):
        return self.process is not None and self.process.returncode is None

    def info(self):
        if self.running():
            return "🟢 Backup running (Telegram controller). Check Status for saved copy count."
        return "⚪ No active controller backup.\nLast controller job: " + self.last_result

    async def start(self, client, chat_id):
        async with self._guard:
            if self.running():
                return "A controller backup is already running."
            # Never delete/replace the persistent DB or Telegram session.
            folder = self.last_log_path.parent
            folder.mkdir(parents=True, exist_ok=True)
            log_handle = open(self.last_log_path, "ab", buffering=0)
            try:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", "/app/tegridy_batch.py",
                    "--phase", "initial", "--execute", "--delay", "15",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log_handle, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                log_handle.close()
            self.last_result = "Started manual initial batch"
            self.watch_task = asyncio.create_task(self._watch(client, chat_id, self.process))
            return "✅ Manual backup started. Only photos with captions will be copied, paced at 15 seconds per send."

    async def _watch(self, client, chat_id, process):
        code = await process.wait()
        result = "Completed successfully" if code == 0 else "Stopped or failed (exit " + str(code) + ")"
        self.last_result = result
        # Never assume exit 0 equals copied content verified; DB status is authoritative.
        try:
            text = self.info() + "\n\n" + self.log_tail(1600)
            await client.send_message(chat_id, "📦 Backup job finished\n" + text)
        except Exception:
            pass

    async def stop(self):
        async with self._guard:
            if not self.running():
                return "No active controller backup."
            # Signal only the child process started by this bot instance.
            self.process.send_signal(signal.SIGINT)
            return "🛑 Stop requested. In-flight Telegram request may finish; check status shortly."

    def log_tail(self, cap=1600):
        try:
            with open(self.last_log_path, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 8192))
                data = fh.read().decode("utf-8", errors="replace")
            lines = [line for line in data.splitlines() if
                     "PROGRESS" in line or "SUMMARY" in line or "Flood wait" in line or
                     "FLOOD WAIT" in line or "COPY FAILED" in line or "MODE=" in line]
            return ("\n".join(lines[-8:]))[-cap:] if lines else "No progress log entries yet."
        except OSError:
            return "No controller job log available."
