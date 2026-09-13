"""
YTFetch v2 — YouTube → MP4 downloader built for very long videos (10h / 20h / 30h+).

Two download modes, chosen automatically per file size vs. free disk:

  STORE mode (default):
    Download on the server (yt-dlp + ffmpeg merge, stream-copy only — never
    re-encodes video), finalize to MP4, then serve the file with full
    HTTP Range support so the browser can RESUME an interrupted save.

  STREAM mode (huge files that won't fit on disk):
    Zero-disk pipeline:  yt-dlp → ffmpeg (fragmented MP4, stream copy) →
    browser, with backpressure. The file is handed to the user's browser
    directly; the browser's download bar shows progress.

Endpoints:
  GET  /api/info?url=…             video details + available qualities
  POST /api/download               {url, quality, mode: "auto"|"store"|"stream"}
  GET  /api/job/{id}               store-mode job status (percent/speed/eta)
  GET  /api/job/{id}/file          the finished MP4 (Range/resume supported)
  GET  /api/sess/{id}              stream-mode session status
  GET  /api/sess/{id}/file         the live MP4 stream (attachment)
  POST /api/sess/{id}/cancel       cancel a stream session
"""

import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="YTFetch", version="2.0")

YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.|music\.|youtube-nocookie\.)?(youtube\.com|youtu\.be)/\S+",
    re.IGNORECASE,
)

QUALITY_LABELS = {
    2160: ("4K", "Ultra HD"),
    1440: ("2K", "QHD"),
    1080: ("1080p", "Full HD"),
    720: ("720p", "HD"),
    480: ("480p", "SD"),
    360: ("360p", "SD"),
}

GB = 1024 ** 3
MB = 1024 ** 2
FASTSTART_MAX = 3 * GB          # moov rewrite (2x temp disk) only for smaller files
STREAM_DISCONNECT_TIMEOUT = 120 # seconds to wait for the browser to pick up a stream
QUEUE_MAX = 96                  # 96 x 1MB chunks in memory (bounded backpressure)

STORE_JOBS: dict = {}
STORE_LOCK = threading.Lock()
STREAM_SESS: dict = {}
STREAM_LOCK = threading.Lock()

PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%")
SPEED_RE = re.compile(r"at\s+([\d.]+)([KMGT]?i?B)/s")
ETA_RE = re.compile(r"ETA\s+(\d+):(\d+)")
UNIT = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
        "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def parse_video_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Please paste a YouTube video link.")
    if not YOUTUBE_RE.match(url):
        raise HTTPException(
            status_code=400,
            detail="That doesn't look like a YouTube link. Paste a URL like https://www.youtube.com/watch?v=…",
        )
    return url


def friendly_error(msg: str) -> str:
    m = msg or ""
    if "ERROR:" in m:
        m = m.split("ERROR:", 1)[1].strip()
    m = re.sub(r"^\[[^\]]+\]\s*[^:]*:\s*", "", m)
    low = m.lower()
    if "sign in to confirm" in low or "not a bot" in low or "robot check" in low or "captcha" in low:
        return "YouTube is temporarily blocking automated access from this server. Please wait a minute and try again."
    if "private video" in low or "this video is private" in low:
        return "This is a private video and can't be downloaded."
    if "unavailable" in low:
        return "This video is unavailable — it may have been removed, made private, or is region-blocked."
    if "age" in low and ("restrict" in low or "confirm" in low or "verify" in low):
        return "This video is age-restricted and can't be downloaded here."
    if "members only" in low or "membership" in low:
        return "This is a members-only video and can't be downloaded here."
    if "copyright" in low:
        return "This video is blocked for copyright reasons and can't be downloaded."
    if "no space left" in low:
        return "The server ran out of disk space for this file. Please try a lower quality."
    if "timed out" in low or "timeout" in low or "datasize" in low:
        return "The request timed out. Please try again."
    if "isn't allowed" in low or "not allowed on your network" in low:
        return "This video can't be played or downloaded from this network."
    if "404" in low or "not found" in low:
        return "That video could not be found. Double-check the link."
    out = m.strip()
    return (out[:300] + "…") if len(out) > 300 else (out or "Something went wrong while processing this video.")


def safe_filename(title: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]+", " ", title or "video")
    name = re.sub(r"\s+", " ", name).strip()
    if len(name) > 120:
        name = name[:120].rstrip()
    return name or "video"


def format_selector(quality: str) -> str:
    """Prefer H.264/AAC (best compatibility); fall back to any stream at/under the cap."""
    cap = 1080 if quality == "best" else int(quality)
    return (
        f"bv*[height<=?{cap}][vcodec^=avc1]+ba[acodec^=mp4a]"
        f"/bv*[height<=?{cap}]+ba"
        f"/b[height<=?{cap}]"
    )


def estimate_bytes(formats, cap: int):
    """Approximate final MP4 size for 'best video at/under cap + best audio'."""
    vs = [f for f in formats if f.get("vcodec") not in (None, "none")
          and (f.get("height") or 0) <= cap]
    as_ = [f for f in formats if f.get("acodec") not in (None, "none")
           and f.get("vcodec") in (None, "none")]
    total = 0
    if vs:
        total += max(f.get("filesize_approx") or 0 for f in vs)
    if as_:
        total += max(f.get("filesize_approx") or 0 for f in as_)
    return total or None


def probe_codec(path: Path, stream: str) -> str:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        lines = out.stdout.strip().splitlines()
        return lines[0].strip() if lines else ""
    except Exception:
        return ""


def finalize_mp4(path: Path, est_bytes, status_cb):
    """Stream-copy finalization ONLY — a 30h video can never be re-encoded here.

    - H.264/AAC + MP4 → faststart remux (small files) or nothing (big files)
    - VP9/AV1/AAC     → keep as VP9/AV1-in-MP4 (Chrome/Edge/VLC compatible)
    - Opus audio      → audio-only re-encode to AAC (video untouched), when disk allows

    Returns (video_codec, audio_codec, note).
    """
    v = probe_codec(path, "v:0")
    a = probe_codec(path, "a:0")
    notes = {
        "h264": "H.264 video · plays on every device",
        "hevc": "HEVC video · most modern devices",
        "vp9": "VP9 video · Chrome / Edge / VLC",
        "av1": "AV1 video · Chrome / Edge / VLC",
    }
    note = notes.get(v, f"{v or 'video'} in MP4")

    free = shutil.disk_usage(path.parent).free
    small = (est_bytes or path.stat().st_size) < FASTSTART_MAX
    opus = a == "opus"

    if opus and (est_bytes or path.stat().st_size) * 0.6 < free:
        status_cb("Converting audio to AAC (video untouched)…")
        tmp = path.with_name(path.stem + "_fin.mp4")
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(path),
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
        if small:
            cmd += ["-movflags", "+faststart"]
        subprocess.run(cmd, check=True, capture_output=True, timeout=86400)
        os.replace(tmp, path)
        a = "aac"
    elif small:
        status_cb("Finalizing MP4…")
        tmp = path.with_name(path.stem + "_fin.mp4")
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(path),
             "-c", "copy", "-movflags", "+faststart", str(tmp)],
            capture_output=True, timeout=86400,
        )
        if r.returncode == 0:
            os.replace(tmp, path)

    return v, a, note


def job_ttl(est_bytes) -> int:
    """Larger files stay longer: 1h base + 20min per GB above 5GB, max 8h."""
    if not est_bytes or est_bytes <= 5 * GB:
        return 3600
    return min(8 * 3600, 3600 + int((est_bytes - 5 * GB) // GB) * 1200)


def schedule_store_cleanup(job_id: str, ttl: int):
    def _run():
        with STORE_LOCK:
            j = STORE_JOBS.pop(job_id, None)
        if j and j.get("path_dir"):
            shutil.rmtree(j["path_dir"], ignore_errors=True)

    t = threading.Timer(ttl, _run)
    t.daemon = True
    t.start()


def sweep_old_jobs():
    now = time.time()
    for entry in JOBS_DIR.iterdir():
        try:
            if now - entry.stat().st_mtime > 9 * 3600:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


threading.Thread(target=sweep_old_jobs, daemon=True).start()


# --------------------------------------------------------------------------- #
# Info
# --------------------------------------------------------------------------- #

INFO_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "socket_timeout": 20,
}


@app.get("/api/health")
def health():
    return {"ok": True, "service": "YTFetch"}


@app.get("/api/info")
def get_info(url: str):
    url = parse_video_url(url)
    try:
        with yt_dlp.YoutubeDL(INFO_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=friendly_error(str(e)))
    except Exception:
        raise HTTPException(status_code=500, detail="Could not read this video. Please try again.")

    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        info = next((e for e in entries if e), None)
        if not info:
            raise HTTPException(status_code=422, detail="Could not find a video in that link.")

    formats = info.get("formats") or []
    seen = {}
    for f in formats:
        h = f.get("height")
        if h and f.get("vcodec") not in (None, "none") and h not in seen:
            seen[h] = True
    qualities = []
    for h in sorted(seen, reverse=True):
        if h > 2160:
            continue
        label, tag = QUALITY_LABELS.get(h, (f"{h}p", "SD" if h <= 480 else "HD"))
        qualities.append({"q": str(h), "label": label, "tag": tag,
                          "size": estimate_bytes(formats, h)})

    vid = info.get("id") or ""
    return {
        "id": vid,
        "title": info.get("title") or "Untitled video",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "views": info.get("view_count"),
        "thumb": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
        "thumb_fallback": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "qualities": qualities,
    }


# --------------------------------------------------------------------------- #
# Download start (mode selection)
# --------------------------------------------------------------------------- #

class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"  # "360" | "480" | "720" | "1080" | "1440" | "2160" | "best"
    mode: str = "auto"     # "auto" | "store" | "stream"


@app.post("/api/download")
def create_download(req: DownloadRequest):
    url = parse_video_url(req.url)
    q = str(req.quality)
    if q != "best" and (not q.isdigit() or not (144 <= int(q) <= 2160)):
        raise HTTPException(status_code=400, detail="Invalid quality option.")
    if req.mode not in ("auto", "store", "stream"):
        raise HTTPException(status_code=400, detail="Invalid mode.")

    # Validate + measure the file before committing to a mode.
    try:
        with yt_dlp.YoutubeDL(INFO_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=friendly_error(str(e)))

    formats = info.get("formats") or []
    cap = 1080 if q == "best" else int(q)
    est = estimate_bytes(formats, cap)
    title = info.get("title") or "video"
    filename = safe_filename(title)

    free = shutil.disk_usage(JOBS_DIR).free
    needed = (est or 0) * 1.15 + 2 * GB
    if req.mode == "store":
        chosen = "store"
    elif req.mode == "stream":
        chosen = "stream"
    else:
        chosen = "store" if (est is not None and needed <= free) else "stream"

    if chosen == "store":
        job_id = uuid.uuid4().hex
        jobdir = JOBS_DIR / job_id
        jobdir.mkdir(parents=True, exist_ok=True)
        job = {
            "id": job_id, "state": "processing", "percent": 0.0,
            "speed": None, "eta": None, "status": "Contacting YouTube…",
            "path": None, "path_dir": str(jobdir), "filename": filename + ".mp4",
            "error": None, "est_bytes": est, "bytes": None, "note": None,
            "created": time.time(),
        }
        with STORE_LOCK:
            STORE_JOBS[job_id] = job
        threading.Thread(target=_store_worker, args=(job_id, url, q, jobdir, job),
                         daemon=True).start()
        return {"mode": "store", "job_id": job_id, "filename": job["filename"],
                "estimated_bytes": est}

    sid = uuid.uuid4().hex
    sess = {
        "id": sid, "url": url, "quality": q, "filename": filename,
        "estimated_bytes": est,
        "state": "preparing", "percent": 0.0, "speed": None, "eta": None,
        "status": "Starting pipeline…", "error": None, "aborted": False,
        "connected": False, "created": time.time(),
        "q": queue.Queue(maxsize=QUEUE_MAX),
        "proc_ydl": None, "proc_ff": None,
    }
    with STREAM_LOCK:
        STREAM_SESS[sid] = sess
    threading.Thread(target=_stream_worker, args=(sess,), daemon=True).start()
    return {"mode": "stream", "session_id": sid, "filename": filename + ".mp4",
            "estimated_bytes": est}


# --------------------------------------------------------------------------- #
# STORE mode
# --------------------------------------------------------------------------- #

def _store_worker(job_id: str, url: str, quality: str, jobdir: Path, job: dict):
    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = min(100.0, (d.get("downloaded_bytes") or 0) / total * 100.0)
                job["percent"] = max(job["percent"], pct)
            job["speed"] = d.get("speed")
            job["eta"] = d.get("eta")
            job["status"] = "Downloading from YouTube…"
        elif d.get("status") == "finished":
            job["status"] = "Merging video + audio…"

    def pp_hook(d):
        if d.get("status") == "started":
            job["status"] = "Merging video + audio…"

    opts = {
        "format": format_selector(quality),
        "outtmpl": str(jobdir / "video.%(ext)s"),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        mp4 = jobdir / "video.mp4"
        if not mp4.exists():
            candidates = [p for p in jobdir.glob("video.*") if p.suffix == ".mp4"]
            if not candidates:
                raise RuntimeError("Download finished but no MP4 file was produced.")
            mp4 = candidates[0]

        _, _, note = finalize_mp4(mp4, job.get("est_bytes"),
                                  status_cb=lambda s: job.update({"status": s}))
        job["note"] = note
        job["bytes"] = mp4.stat().st_size
        job["path"] = str(mp4)
        job["percent"] = 100.0
        job["state"] = "ready"
        schedule_store_cleanup(job_id, job_ttl(job.get("est_bytes")))
    except yt_dlp.utils.DownloadError as e:
        job["state"] = "error"
        job["error"] = friendly_error(str(e))
        shutil.rmtree(jobdir, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = friendly_error(str(e))
        shutil.rmtree(jobdir, ignore_errors=True)


@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    with STORE_LOCK:
        j = STORE_JOBS.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found. Please start the download again.")
    out = {
        "id": j["id"], "state": j["state"], "percent": round(j["percent"], 1),
        "speed": j["speed"], "eta": j["eta"], "status": j["status"],
        "error": j["error"], "filename": j["filename"],
        "bytes": j["bytes"], "note": j["note"], "estimated_bytes": j.get("est_bytes"),
    }
    if j["state"] == "ready":
        out["file"] = f"/api/job/{job_id}/file"
    return out


@app.get("/api/job/{job_id}/file")
def job_file(job_id: str):
    with STORE_LOCK:
        j = STORE_JOBS.get(job_id)
    if not j or j["state"] != "ready" or not j.get("path") or not os.path.exists(j["path"]):
        raise HTTPException(status_code=410,
                            detail="This file is no longer available. Please download the video again.")
    return FileResponse(
        j["path"],
        media_type="video/mp4",
        filename=j["filename"],
        headers={"Accept-Ranges": "bytes", "Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- #
# STREAM mode (zero-disk pipeline for huge files)
# --------------------------------------------------------------------------- #

def _kill_procs(s: dict):
    for key in ("proc_ydl", "proc_ff"):
        p = s.get(key)
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    deadline = time.time() + 5
    for key in ("proc_ydl", "proc_ff"):
        p = s.get(key)
        if p:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


def _stream_worker(s: dict):
    """yt-dlp → (mpegts pipe) → ffmpeg fMP4 (stream copy) → queue → browser.

    Backpressure: if the browser is slow, the queue fills and this loop blocks,
    which stalls ffmpeg, which stalls yt-dlp. Bounded memory (~96MB worst case).
    """
    try:
        s["status"] = "Contacting YouTube…"
        ydl_cmd = [
            "yt-dlp", "-f", format_selector(s["quality"]),
            "--merge-output-format", "mp4", "--no-part",
            "--retries", "5", "--fragment-retries", "10",
            "--socket-timeout", "30", "-o", "-", s["url"],
        ]
        ydl_proc = subprocess.Popen(ydl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        s["proc_ydl"] = ydl_proc

        ff_cmd = [
            "ffmpeg", "-v", "error", "-i", "pipe:0",
            "-c", "copy", "-bsf:a", "aac_adtstoasc",
            "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov", "-",
        ]
        ff_proc = subprocess.Popen(ff_cmd, stdin=ydl_proc.stdout, stdout=subprocess.PIPE)
        s["proc_ff"] = ff_proc
        ydl_proc.stdout.close()

        s["state"] = "streaming"
        s["status"] = "Fetching from YouTube…"
        threading.Thread(target=_stream_err_reader, args=(s, ydl_proc), daemon=True).start()

        while True:
            if not s.get("connected") and not s.get("aborted") and \
                    time.time() - s["created"] > STREAM_DISCONNECT_TIMEOUT:
                s["aborted"] = True
                s["state"] = "aborted"
                s["status"] = "No browser connected in time — stream cancelled."
                _kill_procs(s)
                break
            chunk = ff_proc.stdout.read1(1 << 20)
            if not chunk:
                break
            if _queue_put_or_abort(s, chunk):
                break

        rc = ff_proc.wait()
        yrc = ydl_proc.wait()
        if not s.get("aborted"):
            if rc != 0 and not s.get("error"):
                s["error"] = "The media pipeline stopped unexpectedly. Please try again."
            if s["state"] == "streaming":
                s["state"] = "done"
                s["percent"] = 100.0
                s["status"] = "Complete"
    except Exception as e:  # noqa: BLE001
        if not s.get("aborted"):
            s["state"] = "error"
            s["error"] = friendly_error(str(e))
    finally:
        _kill_procs(s)
        s["q"].put(None)


def _queue_put_or_abort(s: dict, chunk: bytes) -> bool:
    """Put a chunk into the browser queue; abort if nobody's listening."""
    while True:
        try:
            s["q"].put(chunk, timeout=3)
            return False
        except queue.Empty:
            if s.get("aborted"):
                return True
            if not s.get("connected") and time.time() - s["created"] > STREAM_DISCONNECT_TIMEOUT:
                s["aborted"] = True
                s["state"] = "aborted"
                s["status"] = "No browser connected in time — stream cancelled."
                _kill_procs(s)
                return True


def _stream_err_reader(s: dict, proc):
    """Parse yt-dlp's stderr progress into the session state."""
    buf = ""
    pass_no = 1
    try:
        while True:
            raw = proc.stderr.read1(4096)
            if not raw:
                break
            buf = (buf + raw.decode("utf-8", "ignore"))[-65536:]
            if "Merging formats into" in buf and pass_no == 1:
                pass_no = 2
                s["status"] = "Merging video + audio…"
            pcts = PCT_RE.findall(buf)
            if pcts:
                p = float(pcts[-1])
                s["percent"] = min(99.5, (pass_no - 1) * 50 + p / 2)
            speeds = SPEED_RE.findall(buf)
            if speeds:
                val, unit = speeds[-1]
                try:
                    s["speed"] = float(val) * UNIT.get(unit, 1)
                except ValueError:
                    pass
            etas = ETA_RE.findall(buf)
            if etas:
                s["eta"] = int(etas[-1][0]) * 60 + int(etas[-1][1])
            if "ERROR:" in buf:
                s["error"] = friendly_error(buf)
            time.sleep(0.15)
    except Exception:
        pass


def abort_stream_session(sid: str):
    with STREAM_LOCK:
        s = STREAM_SESS.get(sid)
    if not s or s.get("aborted"):
        return
    s["aborted"] = True
    if s["state"] in ("preparing", "streaming"):
        s["state"] = "aborted"
        s["status"] = "Cancelled."
    _kill_procs(s)

    def _clean():
        with STREAM_LOCK:
            STREAM_SESS.pop(sid, None)

    threading.Timer(300, _clean).daemon = True
    threading.Timer(300, _clean).start()


@app.get("/api/sess/{sid}")
def sess_status(sid: str):
    with STREAM_LOCK:
        s = STREAM_SESS.get(sid)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found. Please start again.")
    return {
        "id": s["id"], "state": s["state"], "percent": round(s["percent"], 1),
        "speed": s["speed"], "eta": s["eta"], "status": s["status"],
        "error": s["error"], "filename": s["filename"] + ".mp4",
        "estimated_bytes": s["estimated_bytes"], "connected": s["connected"],
    }


@app.get("/api/sess/{sid}/file")
def sess_file(sid: str):
    with STREAM_LOCK:
        s = STREAM_SESS.get(sid)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found.")
    if s["state"] == "preparing":
        raise HTTPException(status_code=409, detail="Stream is still starting up. Try again in a few seconds.")
    if s["state"] in ("done", "error", "aborted"):
        raise HTTPException(status_code=410, detail="This stream has ended. Please start again.")

    s["connected"] = True

    def gen():
        try:
            while True:
                item = s["q"].get()
                if item is None:
                    break
                yield item
        except GeneratorExit:
            pass
        finally:
            abort_stream_session(sid)

    fn = (s["filename"] + ".mp4").replace('"', "'")
    # NOTE: no Content-Length header on purpose — the exact fMP4 size is only
    # known at the end, and a wrong CL makes h11 abort the response.
    # Chunked transfer is used instead; browsers track the growing file size,
    # and the in-page panel shows the fetch percentage.
    headers = {
        "Content-Disposition": f"attachment; filename=\"{fn}\"; filename*=UTF-8''{urllib.parse.quote(fn)}",
        "Cache-Control": "no-store",
        "Accept-Ranges": "none",
    }
    return StreamingResponse(gen(), media_type="video/mp4", headers=headers)


@app.post("/api/sess/{sid}/cancel")
def sess_cancel(sid: str):
    abort_stream_session(sid)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html", media_type="text/html")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE_DIR / "static" / "favicon.svg", media_type="image/svg+xml")


# Compatibility patch (after every route is registered): recent
# fastapi/starlette versions drop the implicit HEAD method from GET routes.
for _route in app.router.routes:
    _methods = getattr(_route, "methods", None)
    if isinstance(_methods, set) and "GET" in _methods:
        _methods.add("HEAD")
