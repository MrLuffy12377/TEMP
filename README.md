# Instagram -> Telegram Forwarder

Watches **Channel A** for messages containing Instagram links, downloads each
video at the highest available quality, and re-uploads it to **Channel B**
with a caption. A local SQLite database tracks which messages have already
been handled, so it never re-downloads/re-uploads the same post, and can
safely "catch up" on anything it missed while offline.

## 1. Install dependencies

You need Python 3.9+ and `ffmpeg` (yt-dlp uses it to merge video+audio).

```bash
sudo apt install ffmpeg      # Debian/Ubuntu
pip install -r requirements.txt
```

## 2. Get Telegram API credentials

1. Go to <https://my.telegram.org> and log in with your phone number.
2. Open **API development tools** and create an app.
3. Copy the `api_id` and `api_hash` into `main.py`:

```python
API_ID = 12345678
API_HASH = "your_api_hash_here"
```

This script logs in as **your own Telegram account** (via Telethon), not as
a bot — that's what lets it read a channel's history and post into another
channel you're a member/admin of. The first run will ask for your phone
number and the login code sent to Telegram; after that it saves a
`.session` file so you won't need to log in again.

## 3. Configure the channels

In `main.py`, set:

```python
SOURCE_CHANNEL = "channel_a_username"   # or a numeric ID, e.g. -100123456789
DEST_CHANNEL   = "channel_b_username"
```

- Username form works for public channels (`"mychannel"`, no `@` needed).
- For private channels, use the numeric chat ID (you can get it by
  forwarding a message from the channel to `@userinfobot`, or by using
  Telethon's `client.get_dialogs()` once to print IDs).
- Your account must already be a member of Channel A and have posting
  rights in Channel B.

## 4. (Optional) Instagram cookies for private/rate-limited posts

If you get login-required or rate-limit errors from Instagram:

1. Log into Instagram in your browser.
2. Export cookies to a Netscape-format `cookies.txt` (e.g. using a browser
   extension like "Get cookies.txt").
3. Set in `main.py`:
   ```python
   IG_COOKIES_FILE = "cookies.txt"
   ```

## 5. Run it

```bash
python main.py
```

On startup it will:
1. Connect to Telegram (asks for login on first run only).
2. **Catch up**: scan Channel A's history from the last message id it has
   seen (stored in SQLite) up to now, processing any Instagram links found.
3. **Listen live**: stay connected and process new messages as they arrive.

Keep it running with `screen`, `tmux`, `pm2`, a `systemd` service, or in a
Docker container, since it needs to stay connected to catch new messages.

## How the database works (`insta_forwarder.db`)

- `processed_messages` — one row per (message, link) processed, with
  status (`done`/`failed`), the downloaded file path, the resulting
  message id in Channel B, and any error. Anything already `done` is
  skipped on re-runs.
- `state` — stores `last_seen_id`, the highest Telegram message id
  processed so far, used to resume correctly after a restart.

You can inspect it any time with:

```bash
sqlite3 insta_forwarder.db "SELECT * FROM processed_messages ORDER BY message_id DESC LIMIT 20;"
```

To force a re-download of a specific message, delete its row:

```bash
sqlite3 insta_forwarder.db "DELETE FROM processed_messages WHERE message_id = 12345;"
```

## Notes & limits

- **File size**: a regular (non-Telegram-Premium) account can upload files
  up to 2 GB via the API; Premium raises this to 4 GB. Very long/high-bitrate
  reels could hit this.
- **Rate limits**: hammering Instagram with many downloads back-to-back can
  get you temporarily rate-limited or asked to log in — space out large
  backlogs, and/or use `IG_COOKIES_FILE` above.
- **Captions**: Telegram captions are capped at 1024 characters; the script
  truncates the original Instagram caption to fit alongside the credited
  uploader and source link.
- **Rights**: only forward/repost content you have permission to redistribute
  — the script credits the original uploader and links back to the source
  post in every caption, but the legal responsibility for reposting is yours.
