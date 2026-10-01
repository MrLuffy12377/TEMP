import os
import re
import subprocess
import sqlite3
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

import yt_dlp
from telethon import TelegramClient, events
from telethon.tl.custom.message import Message
from telethon.tl.types import DocumentAttributeVideo
from telethon.errors import MessageTooLongError

# ============================================================
# TELEGRAM CONFIG  -- fill these in with YOUR existing values
# ============================================================
API_ID = 39113911               # <-- your api_id
API_HASH = "90201f2dd456b40efd7b5265225dc2a6"                # <-- your api_hash
SESSION_NAME = "insta_forwarder"
SOURCE_CHANNEL = -1004485238197          # <-- channel you're monitoring for Instagram links
DEST_CHANNEL = -1003906146900         # <-- channel where videos get uploaded

# NEW: a channel/chat where the sqlite database file gets backed up as a
# document after every run (and periodically while the script is live).
# Set to None to disable backups entirely.
BACKUP_CHANNEL = None         # <-- e.g. "me" (Saved Messages) or a channel username/id
BACKUP_INTERVAL_MINUTES = 30  # how often to re-upload the backup while running live

# ============================================================
# GENERAL CONFIG
# ============================================================
DB_PATH = "insta_forwarder.db"
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Instagram is now frequently requiring a logged-in session to serve video
# data (you'll see "Instagram sent an empty media response" otherwise).
# Use ONE of the two options below:
IG_COOKIES_FROM_BROWSER = None  # e.g. ("chrome",) or ("edge",) or ("firefox",)
IG_COOKIES_FILE = r"C:\Users\danis\Downloads\all from python\video download and upload\video download\files (1)\cookies.txt"

MANUAL_FFMPEG_PATH = r"C:\Users\danis\AppData\Local\Programs\Python\Python312\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"

CAPTION_MAX_LEN = 1000  # Telegram caption hard limit is 1024 chars

# NEW: how many times to retry a link that keeps erroring before giving up
# and permanently marking it "broken" instead of retrying forever.
MAX_ATTEMPTS = 3

# NEW: whether to automatically retry links already marked "broken" on a
# later run. Usually you want this off (broken = permanently unavailable
# post), but flip to True if you think Instagram access was the problem,
# not the post itself.
RETRY_BROKEN = False

# NEW: full history reconciliation. Because you run this on/off instead of
# 24/7, the incremental "since last_seen_id" scan can miss messages if a
# previous run crashed before saving state. With this True, every run walks
# the ENTIRE channel history and checks each message against the database
# (cheap - already-completed links are skipped instantly), guaranteeing
# nothing is ever permanently missed. Set to False only if your channel is
# huge and you're confident incremental tracking is reliable.
FULL_RESCAN_EVERY_RUN = True

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
# Schema note: the old schema used message_id as the PRIMARY KEY, which
# silently overwrote earlier links whenever a single Telegram message
# contained more than one Instagram URL. The new schema keys on
# (message_id, url) so every link is tracked independently, and adds an
# "attempts" counter + "pending" status so a link is recorded in the DB
# the moment it's spotted, not just after it succeeds or fails.

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_links (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id        INTEGER NOT NULL,
            url               TEXT NOT NULL,
            status            TEXT NOT NULL,     -- 'pending' | 'completed' | 'error' | 'broken'
            file_path         TEXT,
            dest_message_id   INTEGER,
            message_date      TEXT,
            attempts          INTEGER NOT NULL DEFAULT 0,
            processed_at      TEXT NOT NULL,
            error             TEXT,
            UNIQUE(message_id, url)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    _migrate_old_schema(conn)
    return conn


def _migrate_old_schema(conn: sqlite3.Connection):
    """One-time migration: if an old `processed_messages` table (message_id
    as PK, one row per message rather than per link) exists, copy its rows
    into the new processed_links table so history isn't lost."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='processed_messages'"
    )
    if not cur.fetchone():
        return
    log.info("Old 'processed_messages' table found - migrating rows into 'processed_links'...")
    old_rows = conn.execute(
        "SELECT message_id, url, status, file_path, dest_message_id, processed_at, error "
        "FROM processed_messages"
    ).fetchall()
    migrated = 0
    for (message_id, url, status, file_path, dest_message_id, processed_at, error) in old_rows:
        new_status = "completed" if status == "done" else ("error" if status == "failed" else status)
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_links
                    (message_id, url, status, file_path, dest_message_id, attempts, processed_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, url, new_status, file_path, dest_message_id,
                 1 if new_status in ("completed", "error", "broken") else 0,
                 processed_at, error),
            )
            migrated += 1
        except sqlite3.Error:
            pass
    conn.commit()
    log.info(f"Migration complete: {migrated} rows carried over.")


def get_link_status(conn: sqlite3.Connection, message_id: int, url: str):
    row = conn.execute(
        "SELECT status FROM processed_links WHERE message_id = ? AND url = ?",
        (message_id, url),
    ).fetchone()
    return row[0] if row else None


def get_attempts(conn: sqlite3.Connection, message_id: int, url: str) -> int:
    row = conn.execute(
        "SELECT attempts FROM processed_links WHERE message_id = ? AND url = ?",
        (message_id, url),
    ).fetchone()
    return row[0] if row else 0


def upsert_link(conn, message_id, url, status, file_path=None, dest_message_id=None,
                 message_date=None, error=None, attempts=None):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO processed_links
            (message_id, url, status, file_path, dest_message_id, message_date, attempts, processed_at, error)
        VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, 0), ?, ?)
        ON CONFLICT(message_id, url) DO UPDATE SET
            status          = excluded.status,
            file_path       = COALESCE(excluded.file_path, processed_links.file_path),
            dest_message_id = COALESCE(excluded.dest_message_id, processed_links.dest_message_id),
            message_date    = COALESCE(excluded.message_date, processed_links.message_date),
            attempts        = COALESCE(?, processed_links.attempts),
            processed_at    = excluded.processed_at,
            error           = excluded.error
        """,
        (message_id, url, status, file_path, dest_message_id, message_date,
         attempts, now, error, attempts),
    )
    conn.commit()


def print_summary(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM processed_links GROUP BY status"
    ).fetchall()
    counts = {status: count for status, count in rows}
    total = sum(counts.values())
    log.info(
        "DB summary: total=%d completed=%d pending=%d error=%d broken=%d",
        total,
        counts.get("completed", 0),
        counts.get("pending", 0),
        counts.get("error", 0),
        counts.get("broken", 0),
    )


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


def get_last_seen_date(conn):
    row = conn.execute("SELECT value FROM state WHERE key = 'last_seen_date'").fetchone()
    return row[0] if row else None


def set_last_seen_date(conn, iso_date: str):
    conn.execute(
        """
        INSERT INTO state (key, value) VALUES ('last_seen_date', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (iso_date,),
    )
    conn.commit()


# ============================================================
# FFMPEG LOCATION HELPER
# ============================================================
import shutil

_ffmpeg_path_cache = None

def _get_ffmpeg_path():
    global _ffmpeg_path_cache
    if _ffmpeg_path_cache is not None:
        return _ffmpeg_path_cache

    if MANUAL_FFMPEG_PATH:
        log.info(f"Using manually configured ffmpeg path: {MANUAL_FFMPEG_PATH}")
        _ffmpeg_path_cache = MANUAL_FFMPEG_PATH
        return MANUAL_FFMPEG_PATH

    if shutil.which("ffmpeg"):
        _ffmpeg_path_cache = ""
        return None

    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        log.info(f"Using ffmpeg from imageio-ffmpeg: {path}")
        _ffmpeg_path_cache = path
        return path
    except Exception:
        log.warning(
            "ffmpeg not found on PATH and imageio-ffmpeg is not installed. "
            "Run: pip install imageio-ffmpeg  (or install ffmpeg manually)."
        )
        _ffmpeg_path_cache = ""
        return None


# ============================================================
# DOWNLOAD (yt-dlp, best available video+audio quality)
# ============================================================

def probe_video(file_path: str, ffmpeg_path: str | None):
    """Runs ffmpeg -i on the file and checks whether a video stream exists.
    Returns (has_video: bool, width: int, height: int, duration: float)."""
    exe = ffmpeg_path or "ffmpeg"
    try:
        proc = subprocess.run(
            [exe, "-i", file_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=30,
        )
        out = proc.stdout
    except Exception as e:
        log.warning(f"probe_video failed to run ffmpeg: {e}")
        return False, 0, 0, 0.0

    has_video = "Video:" in out
    w = h = 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    duration = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if m:
        hh, mm, ss = m.groups()
        duration = int(hh) * 3600 + int(mm) * 60 + float(ss)
    return has_video, w, h, duration


def remux_faststart(file_path: str, ffmpeg_path: str | None) -> str:
    """Re-muxes the mp4 with -movflags +faststart (no re-encode, just moves
    the moov atom to the front) so Telegram can preview/stream it properly.
    Returns path to the fixed file (may be the same path)."""
    exe = ffmpeg_path or "ffmpeg"
    fixed_path = file_path + ".fixed.mp4"
    try:
        proc = subprocess.run(
            [exe, "-y", "-i", file_path, "-c", "copy", "-movflags", "+faststart", fixed_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120,
        )
        if proc.returncode == 0 and os.path.exists(fixed_path):
            os.replace(fixed_path, file_path)
        else:
            log.warning(f"faststart remux failed, keeping original file: {proc.stdout[-500:]}")
            if os.path.exists(fixed_path):
                os.remove(fixed_path)
    except Exception as e:
        log.warning(f"faststart remux errored, keeping original file: {e}")
        if os.path.exists(fixed_path):
            os.remove(fixed_path)
    return file_path


def download_instagram(url: str, out_dir: Path):
    """
    Downloads the highest quality video+audio available, merges to mp4,
    validates it actually contains a video stream, and faststart-remuxes it.
    Returns (file_path, caption_text, uploader_handle, width, height, duration).
    Raises RuntimeError with a descriptive message on any failure so the
    caller can log/classify it properly.
    Runs synchronously - call via asyncio.to_thread from async code.
    """
    outtmpl = str(out_dir / "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        # Prefer h264/mp4 streams explicitly - AV1/VP9 webm streams are the
        # usual culprit behind Telegram showing "audio only". Fall back to
        # generic bestvideo+bestaudio only if no mp4-compatible option exists.
        "format": (
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "best[ext=mp4]/bestvideo+bestaudio/best"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
    }
    if IG_COOKIES_FROM_BROWSER:
        ydl_opts["cookiesfrombrowser"] = IG_COOKIES_FROM_BROWSER
    elif IG_COOKIES_FILE:
        ydl_opts["cookiefile"] = IG_COOKIES_FILE

    ffmpeg_path = _get_ffmpeg_path()
    if ffmpeg_path:
        ydl_opts["ffmpeg_location"] = ffmpeg_path

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = ydl.prepare_filename(info)

        if not file_path.endswith(".mp4"):
            base, _ = os.path.splitext(file_path)
            if os.path.exists(base + ".mp4"):
                file_path = base + ".mp4"

        caption = (info.get("description") or info.get("title") or "").strip()
        uploader = info.get("uploader") or info.get("uploader_id") or ""

    if not os.path.exists(file_path):
        raise RuntimeError("download_failed: no output file produced by yt-dlp")

    has_video, width, height, duration = probe_video(file_path, ffmpeg_path)
    if not has_video:
        # Delete the bad audio-only file so it doesn't linger in DOWNLOAD_DIR.
        try:
            os.remove(file_path)
        except Exception:
            pass
        raise RuntimeError(
            "no_video_stream: downloaded media has no video track (audio-only "
            "response from Instagram/yt-dlp) - will retry"
        )

    file_path = remux_faststart(file_path, ffmpeg_path)

    return file_path, caption, uploader, width, height, duration


def classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if "no_video_stream" in msg or "download_failed" in msg:
        # Treat as transient - format selection / Instagram glitch, worth retrying.
        return "error"
    broken_markers = (
        "not found", "unavailable", "private", "does not exist",
        "unable to extract", "login required", "restricted video",
        "this content isn't available", "removed", "no video formats",
        "unsupported url",
    )
    if any(marker in msg for marker in broken_markers):
        return "broken"
    return "error"


# ============================================================
# CORE PROCESSING
# ============================================================

async def process_message(client: TelegramClient, conn: sqlite3.Connection, message: Message):
    if not message.text:
        return

    urls = INSTAGRAM_URL_RE.findall(message.text)
    if not urls:
        return

    msg_date = message.date.isoformat() if message.date else None

    for url in urls:
        existing_status = get_link_status(conn, message.id, url)

        if existing_status == "completed":
            continue
        if existing_status == "broken" and not RETRY_BROKEN:
            continue

        attempts = get_attempts(conn, message.id, url)
        upsert_link(conn, message.id, url, "pending", message_date=msg_date, attempts=attempts)
        log.info(f"[{('retry' if attempts else 'new')}] message {message.id} -> {url}")

        file_path = None
        try:
            file_path, ig_caption, uploader, width, height, duration = await asyncio.to_thread(
                download_instagram, url, DOWNLOAD_DIR
            )

            # Build the "full" caption first, then check Telegram's real limit
            # (1024 chars). If it's too long, drop the description entirely
            # and just send uploader + link, so we never fail on caption length.
            def build_caption(include_description: bool) -> str:
                parts = []
                if uploader:
                    parts.append(f"👤 @{uploader}")
                if include_description and ig_caption:
                    parts.append(ig_caption[:CAPTION_MAX_LEN])
                parts.append(f"🔗 {url}")
                return "\n\n".join(parts)

            final_caption = build_caption(include_description=True)
            caption_reason = None
            if len(final_caption) > 1024:
                final_caption = build_caption(include_description=False)
                caption_reason = "caption_too_long_dropped_description"
                if len(final_caption) > 1024:
                    final_caption = f"🔗 {url}"

            attributes = [
                DocumentAttributeVideo(
                    duration=int(duration),
                    w=width,
                    h=height,
                    supports_streaming=True,
                )
            ]

            sent = await client.send_file(
                DEST_CHANNEL,
                file_path,
                caption=final_caption,
                attributes=attributes,
                supports_streaming=True,
            )

            error_note = caption_reason  # record it even on success, for visibility
            upsert_link(conn, message.id, url, "completed",
                        file_path=file_path, dest_message_id=sent.id,
                        message_date=msg_date, attempts=attempts + 1, error=error_note)
            log.info(f"[done] message {message.id} -> uploaded as {sent.id}"
                     + (f" (note: {caption_reason})" if caption_reason else ""))

        except MessageTooLongError:
            # Belt-and-suspenders: if it still somehow exceeds the limit, retry
            # once immediately with just the link, instead of counting it as
            # a hard failure.
            try:
                sent = await client.send_file(
                    DEST_CHANNEL, file_path, caption=f"🔗 {url}",
                    supports_streaming=True,
                )
                upsert_link(conn, message.id, url, "completed",
                            file_path=file_path, dest_message_id=sent.id,
                            message_date=msg_date, attempts=attempts + 1,
                            error="caption_too_long_retried_link_only")
                log.info(f"[done] message {message.id} -> uploaded as {sent.id} (link-only caption)")
            except Exception as e2:
                attempts += 1
                upsert_link(conn, message.id, url, "error", message_date=msg_date,
                            error=f"caption_too_long_retry_failed: {e2}", attempts=attempts)

        except Exception as e:
            attempts += 1
            status = classify_error(e)
            if status == "error" and attempts >= MAX_ATTEMPTS:
                status = "broken"
                log.warning(f"[broken] message {message.id} gave up after {attempts} attempts: {e}")
            else:
                log.exception(f"[{status}] message {message.id}: {e}")
            upsert_link(conn, message.id, url, status, message_date=msg_date,
                        error=str(e), attempts=attempts)

        finally:
            try:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass


# ============================================================
# DATABASE BACKUP -> TELEGRAM CHANNEL
# ============================================================

_last_backup_at = None

async def backup_database(client: TelegramClient, conn: sqlite3.Connection, force: bool = False):
    if not BACKUP_CHANNEL:
        return
    global _last_backup_at
    now = datetime.now(timezone.utc)
    if not force and _last_backup_at is not None:
        elapsed_min = (now - _last_backup_at).total_seconds() / 60
        if elapsed_min < BACKUP_INTERVAL_MINUTES:
            return

    try:
        conn.commit()
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM processed_links GROUP BY status"
        ).fetchall()
        counts = {status: count for status, count in rows}
        caption = (
            f"📦 insta_forwarder DB backup\n"
            f"🕒 {now.isoformat()}\n"
            f"✅ completed: {counts.get('completed', 0)}\n"
            f"⏳ pending: {counts.get('pending', 0)}\n"
            f"⚠️ error: {counts.get('error', 0)}\n"
            f"❌ broken: {counts.get('broken', 0)}"
        )
        await client.send_file(BACKUP_CHANNEL, DB_PATH, caption=caption)
        _last_backup_at = now
        log.info("Database backup uploaded.")
    except Exception:
        log.exception("Database backup upload failed.")


async def periodic_backup(client: TelegramClient, conn: sqlite3.Connection):
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_MINUTES * 60)
        await backup_database(client, conn)


# ============================================================
# CATCH-UP: scan history for anything missed while offline
# ============================================================

async def catch_up(client: TelegramClient, conn: sqlite3.Connection):
    last_id = get_last_seen_id(conn)
    last_date = get_last_seen_date(conn)
    log.info(f"Starting catch-up. last_seen_id={last_id} last_seen_date={last_date}")

    checked = 0
    newest_id = last_id

    # FULL_RESCAN_EVERY_RUN walks the whole channel history each run and
    # relies on the DB (message_id+url uniqueness, 'completed' status) to
    # skip work that's already done - this is what guarantees old/missed
    # links get picked up even if the script only runs occasionally.
    # Otherwise it falls back to the cheaper incremental min_id scan.
    if FULL_RESCAN_EVERY_RUN:
        iterator = client.iter_messages(SOURCE_CHANNEL, reverse=True)
    else:
        iterator = client.iter_messages(SOURCE_CHANNEL, min_id=last_id, reverse=True)

    async for msg in iterator:
        checked += 1
        await process_message(client, conn, msg)
        if msg.id > newest_id:
            newest_id = msg.id
        if msg.date:
            iso = msg.date.isoformat()
            if not last_date or iso > last_date:
                last_date = iso

    if newest_id > last_id:
        set_last_seen_id(conn, newest_id)
    if last_date:
        set_last_seen_date(conn, last_date)

    print_summary(conn)
    log.info(f"Catch-up complete. Checked {checked} messages. Newest message id={newest_id}, date={last_date}")

    await backup_database(client, conn, force=True)


# ============================================================
# LIVE LISTENER: handle new messages as they arrive
# ============================================================

def register_handlers(client: TelegramClient, conn: sqlite3.Connection):
    @client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def handler(event):
        await process_message(client, conn, event.message)
        set_last_seen_id(conn, event.message.id)
        if event.message.date:
            set_last_seen_date(conn, event.message.date.isoformat())
        await backup_database(client, conn)  # throttled internally


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

    if BACKUP_CHANNEL:
        asyncio.create_task(periodic_backup(client, conn))

    log.info("Listening for new messages... (Ctrl+C to stop)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
