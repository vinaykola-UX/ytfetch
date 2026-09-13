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

### One-command deploy (Ubuntu/Debian)
```bash
git clone https://github.com/vinaykola-UX/ytfetch.git
cd ytfetch
sudo ./deploy.sh    # installs deps, venv, systemd service → http://SERVER_IP:8000
```
Requires `ffmpeg` (deploy.sh installs it). Open the firewall port first.

### Host the UI on Netlify (or anywhere) + the API on your server
The frontend is a single static file, so it can live on Netlify/Vercel/GitHub
Pages while the download engine runs on a free server:

**Easiest — Render (one click):**
1. https://render.com → **New → Blueprint** → connect this repo (it finds
   `render.yaml` automatically) → **Apply**.
2. Wait ~2 min → you get an URL like `https://ytfetch-api.onrender.com`.
3. Open your Netlify page → paste that URL once (it's remembered), or
   hard-code it in `static/index.html`:
   ```html
   <script>window.YTFETCH_API = "https://ytfetch-api.onrender.com";</script>
   ```
   (On Netlify: Settings → Site details → add that `<script>` tag above the
   main `<script>` in `index.html`.)
4. Done — Netlify hosts the page, Render runs the downloads.

Note: Render's free instance sleeps after 15 min of idle; the first request
after that takes ~30–60 s to wake. For a server that NEVER sleeps, use
Oracle Cloud Always Free (A1) with `sudo ./deploy.sh` instead.

The API allows cross-origin requests (CORS), so the two can live on different
domains.

### YouTube "sign in to confirm you're not a bot" on some videos
YouTube occasionally enforces a sign-in/bot check on specific videos from
datacenter IPs. The app shows a friendly message for this. To make
sign-in-protected videos work on *your* server:

1. Install the "Get cookies.txt LOCALLY" browser extension.
2. Go to youtube.com, sign in, export **cookies.txt**.
3. Put that file in the app folder (as `cookies.txt`) or point
   `YTFETCH_COOKIES=/path/to/cookies.txt` in the systemd unit. Restart the service.

⚠️ `cookies.txt` is a personal credential — it is git-ignored, never commit it.

### Best FREE hosting (fast responses)
This app needs a **long-running Python + ffmpeg backend** (not a static host like
Netlify/Vercel — downloads are processed server-side). Best always-free options:

1. **Oracle Cloud Always Free — Ampere A1** (recommended): permanently free,
   up to 4 ARM cores + 24 GB RAM + 200 GB disk, always-on (no sleeping). The only
   big free tier that can *store* 30-hour 1080p files. ~10 Gbps network.
2. **Google Cloud free e2-micro** (1 vCPU / 1 GB / 30 GB disk): easiest to spin up,
   good if you pick a region close to your users (e.g. Mumbai for India);
   30 GB disk is plenty for stream mode + medium files.
3. **AWS Free Tier t4g/t3.micro** (12 months) / **Fly.io, Render, Railway** —
   fine for short videos, but free tiers sleep, cap RAM/disk, and will struggle
   with multi-hour, multi-GB downloads.

⚠️ Free VPS IPs are datacenter IPs: YouTube occasionally rate-limits them
("sign in to confirm you're not a bot"). The app detects this and shows a friendly
"try again in a minute" message; it clears on its own.


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
