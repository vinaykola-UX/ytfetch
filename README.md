# ⚡ YTFetch

A fully working YouTube → MP4 downloader built to handle **very long videos (10h / 20h / 30h+)**.
Paste a link, pick a quality (360p / 480p / 720p / 1080p — plus 2K/4K when available), and download
a real MP4. No mocks, no demo data — every download is processed live on the server.

## Two download modes (chosen automatically)

A 30-hour 1080p video is ~12–20 GB, so YTFetch picks the right strategy per file:

### STORE mode (default — when the file fits on the server disk)
```
Browser
 1. GET  /api/info?url=…           → title, thumb, duration, qualities + sizes
 2. POST /api/download             → starts a background job {url, quality, mode}
 3. GET  /api/job/{id}  (polling)  → real progress %, speed, ETA
 4. GET  /api/job/{id}/file        → the finished MP4
      └─ full HTTP Range support  → the browser can RESUME an interrupted save
```
- Finalization is **stream-copy only** — a 30h video is never re-encoded.
  H.264/AAC → `+faststart` remux (small files). VP9/AV1 → kept in MP4 (Chrome/Edge/VLC).
  Opus audio → audio-only AAC re-encode when disk allows.
- Finished files are kept for **1h + 20min/GB above 5GB (max 8h)**, then auto-deleted.

### STREAM mode (huge files that won't fit on disk)
```
yt-dlp ──(mpegts)──▶ ffmpeg -c copy (fragmented MP4) ──▶ queue ──▶ browser
```
- **Zero disk usage**, bounded memory (~96 MB) with real backpressure: a slow browser
  stalls ffmpeg, which stalls yt-dlp.
- The file is handed to the user's browser directly; the browser's download bar
  shows the save progress. Progress/speed/ETA of the YouTube side are still polled.
- Auto-cancels after 120 s if no browser connects; cancellable at any time;
  both processes are killed on disconnect.

Mode is auto-selected by comparing the estimated file size against free disk
(`needed = est*1.15 + 2 GB`). You can also force it with `mode: "store"|"stream"`.

## Verified end-to-end (real downloads, not demo data)
- Short video (144p) and 1080p of "Rick Astley" → genuine H.264/AAC MP4, correct filename.
- 4.4-hour feature (up to 1080p) → long-video pipeline exercised (fragmented fetch, merge).
- STDOUT/pipe pipeline (`-o -` → fMP4) validated for the stream path.
- Error handling: invalid links, removed/private/age-restricted videos, bot-checks,
  and out-of-disk all return friendly messages.

## Run it

Requirements: Python 3.10+, `yt-dlp`, `fastapi`, `uvicorn`, and `ffmpeg` on PATH.

```bash
pip install yt-dlp fastapi uvicorn
./run.sh            # or: python3 -m uvicorn app:app --host 0.0.0.0 --port 8000
```
Then open http://localhost:8000.

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI backend: info, mode selection, store jobs, stream pipeline, progress |
| `static/index.html` | Mobile-first single-page UI (vanilla HTML/CSS/JS, no build step) |
| `static/favicon.svg` | Favicon |
| `run.sh` | One-line start script |
| `jobs/` | Temporary finished downloads (auto-cleaned) |

## Notes & limits
- Quality options come from the formats YouTube actually offers (audio-only pseudo-formats filtered out).
- Stream mode produces **fragmented MP4** (same container, streamable) — plays in Chrome/Edge/VLC/QuickTime.
- Long downloads run in background threads; the web server stays responsive.
- Personal use only — respect YouTube's Terms of Service and copyright law.
