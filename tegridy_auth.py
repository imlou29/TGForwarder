"""One-time interactive Telegram user authorization. Run ONLY via private Railway SSH terminal.

Never paste Telegram verification codes, 2FA passwords, or session files into
ChatGPT, GitHub, Railway variables, or application logs.
"""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

async def main():
    root = Path(os.environ.get("USER_SESSION_PATH", "/data/tgforwarder/user"))
    root.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(root.parent, 0o700)
    client = TelegramClient(str(root), int(os.environ["API_ID"]), os.environ["API_HASH"])
    try:
        await client.start()  # Secure interactive phone, code and optional 2FA prompts.
        me = await client.get_me()
        print("AUTHORIZED Telegram user id:", me.id)
        print("Telegram username:", me.username or "(none)")
        print("Session saved on persistent volume; no session secret printed.")
    finally:
        await client.disconnect()
        session_file = Path(str(root) + ".session")
        if session_file.exists():
            os.chmod(session_file, 0o600)

if __name__ == "__main__":
    asyncio.run(main())
