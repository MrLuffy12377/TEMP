"""
Instagram -> Telegram forwarder
--------------------------------
Watches SOURCE_CHANNEL for messages containing Instagram links, downloads the
video at the highest available quality (yt-dlp), then re-uploads it to
DEST_CHANNEL with a caption (original IG caption + credit + source link).

Uses a local SQLite database to:
  - remember which Telegram messages have already been processed (so re-runs
    never re-download/re-upload the same post)
  - remember the last message id seen, so on startup it can "catch up" on
    anything posted while the script was offline

Run once to log in (creates a .session file), then leave it running
(systemd / screen / tmux / pm2 / Docker - your choice) for live forwarding.
"""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import Message
import yt_dlp

# ============================================================
# CONFIG  -- fill these in (see README.md for how to get them)
# ============================================================

API_ID = 39113911             # from https://my.telegram.org -> API development tools
API_HASH = "90201f2dd456b40efd7b5265225dc2a6"
SESSION_NAME = "insta_forwarder"  # local session file name, no need to change

SOURCE_CHANNEL = -1004485238197  # Channel A (where IG links are posted)
DEST_CHANNEL = -1003906146900     # Channel B (where videos get uploaded)

DB_PATH = "insta_forwarder.db"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Optional: point this at a cookies.txt (Netscape format, exported from your
# logged-in IG session) if you hit "login required" / rate-limit errors on
# private posts or after heavy use. Leave as None to try without login.
IG_COOKIES_FROM_BROWSER = None   # or ("edge",), ("firefox",), etc — whichever browser you're logged into Instagram with
IG_COOKIES_FILE = r"C:\Users\danis\Downloads\all from python\video download and upload\video download\files (1)\cookies.txt" # e.g. "cookies.txt"

CAPTION_MAX_LEN = 1000  # Telegram caption hard limit is 1024 chars

INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reel|p|tv)/[A-Za-z0-9_\-]+/?[^\s]*",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("insta_forwarder")


# ============================================================
# DATABASE
# ============================================================

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id       INTEGER PRIMARY KEY,
            url              TEXT NOT NULL,
            status           TEXT NOT NULL,     -- 'done' | 'failed'
            file_path        TEXT,
            dest_message_id  INTEGER,
            processed_at     TEXT NOT NULL,
            error            TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, message_id: int) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM processed_messages WHERE message_id = ? AND status = 'done'",
        (message_id,),
    )
    return cur.fetchone() is not None


def mark_processed(conn, message_id, url, status, file_path=None, dest_message_id=None, error=None):
    conn.execute(
        """
        INSERT INTO processed_messages
            (message_id, url, status, file_path, dest_message_id, processed_at, error)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            status          = excluded.status,
            file_path       = excluded.file_path,
            dest_message_id = excluded.dest_message_id,
            processed_at    = excluded.processed_at,
            error           = excluded.error
        """,
        (message_id, url, status, file_path, dest_message_id,
         datetime.now(timezone.utc).isoformat(), error),
    )
    conn.commit()


def get_last_seen_id(conn) -> int:
    row = conn.execute("SELECT value FROM state WHERE key = 'last_seen_id'").fetchone()
    return int(row[0]) if row else 0


def set_last_seen_id(conn, message_id: int):
    conn.execute(
        """
        INSERT INTO state (key, value) VALUES ('last_seen_id', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(message_id),),
    )
    conn.commit()


# ============================================================
# DOWNLOAD (yt-dlp, best available video+audio quality)
# ============================================================

def download_instagram(url: str, out_dir: Path):
    """
    Downloads the highest quality video+audio available and merges to mp4.
    Returns (file_path, caption_text, uploader_handle).
    Runs synchronously - call via asyncio.to_thread from async code.
    """
    outtmpl = str(out_dir / "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
    }
    if IG_COOKIES_FROM_BROWSER:
        ydl_opts["cookiesfrombrowser"] = IG_COOKIES_FROM_BROWSER
    if IG_COOKIES_FILE:
        ydl_opts["cookiefile"] = IG_COOKIES_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = ydl.prepare_filename(info)

        # If merge produced a different extension than the initial guess, fix it up
        if not file_path.endswith(".mp4"):
            base, _ = os.path.splitext(file_path)
            if os.path.exists(base + ".mp4"):
                file_path = base + ".mp4"

        caption = (info.get("description") or info.get("title") or "").strip()
        uploader = info.get("uploader") or info.get("uploader_id") or ""

    return file_path, caption, uploader


# ============================================================
# CORE PROCESSING
# ============================================================

async def process_message(client: TelegramClient, conn: sqlite3.Connection, message: Message):
    if not message.text:
        return

    urls = INSTAGRAM_URL_RE.findall(message.text)
    if not urls:
        return

    for url in urls:
        if is_processed(conn, message.id):
            log.info(f"[skip] message {message.id} already processed")
            continue

        log.info(f"[new] message {message.id} -> {url}")
        file_path = None
        try:
            file_path, ig_caption, uploader = await asyncio.to_thread(
                download_instagram, url, DOWNLOAD_DIR
            )

            parts = []
            if uploader:
                parts.append(f"👤 @{uploader}")
            if ig_caption:
                parts.append(ig_caption[:CAPTION_MAX_LEN])
            parts.append(f"🔗 {url}")
            final_caption = "\n\n".join(parts)

            sent = await client.send_file(
                DEST_CHANNEL,
                file_path,
                caption=final_caption,
                supports_streaming=True,
            )

            mark_processed(conn, message.id, url, "done",
                            file_path=file_path, dest_message_id=sent.id)
            log.info(f"[done] message {message.id} -> uploaded as {sent.id}")

        except Exception as e:
            log.exception(f"[fail] message {message.id}: {e}")
            mark_processed(conn, message.id, url, "failed", error=str(e))

        finally:
            # clean up local file whether it succeeded or failed to upload
            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass


# ============================================================
# CATCH-UP: scan history for anything missed while offline
# ============================================================

async def catch_up(client: TelegramClient, conn: sqlite3.Connection):
    last_id = get_last_seen_id(conn)
    log.info(f"Catching up on messages after id {last_id} ...")

    newest_id = last_id
    async for msg in client.iter_messages(SOURCE_CHANNEL, min_id=last_id, reverse=True):
        await process_message(client, conn, msg)
        if msg.id > newest_id:
            newest_id = msg.id

    if newest_id > last_id:
        set_last_seen_id(conn, newest_id)
    log.info("Catch-up complete.")


# ============================================================
# LIVE LISTENER: handle new messages as they arrive
# ============================================================

def register_handlers(client: TelegramClient, conn: sqlite3.Connection):
    @client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def handler(event):
        await process_message(client, conn, event.message)
        set_last_seen_id(conn, event.message.id)


# ============================================================
# MAIN
# ============================================================

async def main():
    conn = init_db()
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    await client.start()
    log.info("Telegram client connected.")

    await catch_up(client, conn)
    register_handlers(client, conn)

    log.info("Listening for new messages... (Ctrl+C to stop)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
