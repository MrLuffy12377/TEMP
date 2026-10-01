# Telegram → AI Caption → Buffer pipeline

Watches a Telegram channel for links, downloads the video at the highest
available quality, backs it up to a second Telegram channel, writes a new
caption with a model of your choice via OpenRouter, and schedules it to
Buffer.

```
Channel A (links) → SQLite → yt-dlp (best quality) → Channel B (backup)
                                                    → OpenRouter (new caption)
                                                    → Cloudinary (public URL)
                                                    → Buffer (schedule post)
```

Each link moves through these statuses in order: `pending → downloaded →
backed_up → captioned → hosted → done` (or `failed`, with the error saved).
If the script crashes or you stop it, restarting picks up exactly where it
left off — nothing gets re-downloaded or re-posted.

## One thing worth knowing up front

Buffer's API does **not** accept direct file uploads for post media — it
only accepts a public URL it can fetch the file from
([docs](https://developers.buffer.com/guides/hosting-media.html)). Since
your videos live on your laptop, this pipeline pushes each one to
Cloudinary's free tier first to get that public URL, then hands it to
Buffer. That's the one extra hop beyond what you sketched.

## 1. Install

Requires Python 3.9+ and **ffmpeg** (yt-dlp needs it to merge separate
best-video + best-audio streams into one file).

```bash
# macOS
brew install ffmpeg
# Ubuntu/Debian
sudo apt install ffmpeg
# Windows
choco install ffmpeg

pip install -r requirements.txt
cp .env.example .env
```

## 2. Fill in `.env`

- **Telegram (`TELEGRAM_API_ID` / `TELEGRAM_API_HASH`)** — from
  https://my.telegram.org → "API Development Tools". This logs in as your
  own account (not a bot), so it can read any channel you're already a
  member of and isn't limited by the Bot API's small file-size caps.
  First run will prompt you in the terminal for your phone number and the
  login code Telegram texts/sends you — after that it's saved in a
  `.session` file (keep it secret, it's a real login credential).
- **`CHANNEL_A` / `CHANNEL_B`** — `@username`, invite link, or numeric ID.
  You must already be a member of both.
- **`YTDLP_COOKIES_FILE`** (optional) — if a source needs a logged-in
  Instagram/etc. account for higher quality or private content, export
  `cookies.txt` from your browser (e.g. the "Get cookies.txt" extension)
  and point this at it.
- **`OPENROUTER_API_KEY`** — from https://openrouter.ai/keys. Set
  `OPENROUTER_MODEL` to whatever model you want writing captions.
- **`CLOUDINARY_*`** — free account at https://cloudinary.com/console,
  credentials are on the dashboard.
- **`BUFFER_API_KEY`** — personal API key from Settings → API in your
  Buffer account (or https://publish.buffer.com/settings/api). Every
  Buffer plan, including Free, includes API access.
- **`BUFFER_CHANNEL_ID`** — you won't know this yet. Once `BUFFER_API_KEY`
  is set, run:

  ```bash
  python setup_helpers.py orgs
  python setup_helpers.py channels <organization_id>
  ```

  and copy the `id` of the channel (e.g. your Instagram account connected
  to Buffer) you want these posts going to.

## 3. Run

```bash
python main.py
```

It polls channel A every `POLL_INTERVAL_SECONDS` (default 60s), pulls in
any new links, and works through the pipeline. Leave it running in a
terminal, tmux/screen session, or as a background process on your laptop.

## Notes / things you may want to tune

- **Quality**: `downloader.py` uses `bestvideo*+bestaudio/best` merged to
  mp4 — the actual highest quality yt-dlp can find for that source.
- **Rewriting the caption prompt**: edit `DEFAULT_PROMPT` in
  `caption_ai.py` to match your voice/hashtag style.
- **Multiple source accounts**: yt-dlp works with Instagram, TikTok,
  YouTube, X/Twitter, and most other platforms out of the box — channel A
  can contain links from any of them, no code changes needed. If a
  particular account needs authenticated access, that's what
  `YTDLP_COOKIES_FILE` is for.
- **Buffer posting mode**: `BUFFER_MODE=addToQueue` adds to your existing
  Buffer queue/schedule; set it to `shareNow` to post immediately instead.
- **Rate limits**: Buffer's API is in public beta with per-plan request
  caps (e.g. 100 requests/24h on the Free plan) — worth checking your plan
  if you're processing a high volume of links.
- **Disk space**: set `DELETE_LOCAL_AFTER_DONE=true` in `.env` to remove
  the local file once it's backed up to channel B and posted — you'll
  still have the Telegram backup copy.
- **Rights to repost**: this pipeline can move video from any link to your
  own accounts — make sure you actually have the rights/permission to
  repost whatever channel A is pointing at before it goes out on Buffer.

## Files

| File                 | Responsibility                                   |
| -------------------- | ------------------------------------------------- |
| `config.py`           | Loads/validates `.env`                            |
| `db.py`                | SQLite schema + state machine helpers             |
| `reader.py`            | Reads channel A, extracts links                   |
| `downloader.py`        | yt-dlp download at highest quality                |
| `telegram_backup.py`   | Uploads to channel B                              |
| `caption_ai.py`        | OpenRouter caption generation                     |
| `media_host.py`        | Cloudinary upload (public URL for Buffer)         |
| `buffer_client.py`     | Buffer GraphQL API (post creation, org/channel lookups) |
| `setup_helpers.py`     | One-off CLI to find your `BUFFER_CHANNEL_ID`      |
| `main.py`              | Orchestrates the loop                             |
| `utils.py`             | URL-extraction helper                             |
