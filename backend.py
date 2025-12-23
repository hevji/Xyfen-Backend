# backend.py - Flask Backend for YouTube Downloader with Progress Streaming

from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import yt_dlp
import re
import os
import json
import time
import uuid
import threading
from pathlib import Path
from collections import defaultdict

app = Flask(__name__)
CORS(app)

# --------------------
# CONFIG
# --------------------
DOWNLOAD_TTL = 600        # seconds (10 minutes)
RATE_LIMIT = 3            # downloads
RATE_WINDOW = 600         # seconds

# --------------------
# STORES
# --------------------
downloads = {}
rate_limits = defaultdict(list)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# --------------------
# HELPERS
# --------------------
def get_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr)

def rate_limited(ip):
    now = time.time()
    rate_limits[ip] = [t for t in rate_limits[ip] if now - t < RATE_WINDOW]
    if len(rate_limits[ip]) >= RATE_LIMIT:
        return True
    rate_limits[ip].append(now)
    return False

def validate_youtube_url(url):
    youtube_regex = r'^(https?://)?(www\.)?(youtube\.com/(watch\?v=|embed/|v/|shorts/)|youtu\.be/)[a-zA-Z0-9_-]{11}'
    return bool(re.match(youtube_regex, url))

def format_duration(seconds):
    if not seconds:
        return "Unknown"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def format_views(views):
    if not views:
        return "Unknown"
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}M views"
    if views >= 1_000:
        return f"{views / 1_000:.1f}K views"
    return f"{views} views"

# --------------------
# FETCH INFO
# --------------------
@app.route("/api/fetch", methods=["POST"])
def fetch_video():
    data = request.get_json()
    url = data.get("url", "")

    if not url or not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(url, download=False)

    formats, seen = [], set()
    for f in info.get("formats", []):
        h = f.get("height")
        if h and h >= 360:
            q = f"{h}p"
            if q not in seen:
                seen.add(q)
                size = f.get("filesize") or f.get("filesize_approx")
                formats.append({
                    "quality": q,
                    "format": f.get("ext", "mp4"),
                    "size": f"{size / (1024*1024):.1f} MB" if size else "Unknown"
                })

    formats.sort(key=lambda x: int(x["quality"][:-1]), reverse=True)

    return jsonify({
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": format_duration(info.get("duration")),
        "views": format_views(info.get("view_count")),
        "channel": info.get("uploader"),
        "formats": formats[:4]
    })

# --------------------
# DOWNLOAD WORKER
# --------------------
def download_worker(download_id, url, quality):
    downloads[download_id] = {
        "status": "downloading",
        "progress": 0,
        "created": time.time()
    }

    height = int(quality.replace("p", ""))
    filepath = DOWNLOAD_DIR / f"{download_id}.mp4"

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            if total:
                downloads[download_id]["progress"] = min(
                    (d.get("downloaded_bytes", 0) / total) * 100, 99
                )

    opts = {
        "format": f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best",
        "outtmpl": str(filepath.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        downloads[download_id].update({
            "status": "ready",
            "progress": 100,
            "filename": filepath.name
        })

    except Exception as e:
        downloads[download_id] = {
            "status": "error",
            "error": str(e)
        }

# --------------------
# STREAM ENDPOINT
# --------------------
@app.route("/api/download/stream")
def download_stream():
    ip = get_ip()
    if rate_limited(ip):
        return jsonify({"error": "Too many downloads, slow down"}), 429

    url = request.args.get("url")
    quality = request.args.get("quality", "720p")
    download_id = str(uuid.uuid4())

    threading.Thread(
        target=download_worker,
        args=(download_id, url, quality),
        daemon=True
    ).start()

    def gen():
        while True:
            d = downloads.get(download_id)
            if not d:
                break
            if d["status"] == "ready":
                yield f"data: {json.dumps({'type':'complete','filename':d['filename']})}\n\n"
                break
            if d["status"] == "error":
                yield f"data: {json.dumps({'type':'error','message':d['error']})}\n\n"
                break
            yield f"data: {json.dumps({'type':'progress','progress':d.get('progress',0)})}\n\n"
            time.sleep(0.5)

    return Response(gen(), mimetype="text/event-stream")

# --------------------
# FILE SERVE
# --------------------
@app.route("/api/download/file/<filename>")
def download_file(filename):
    path = DOWNLOAD_DIR / filename
    if not path.exists():
        return jsonify({"error": "Not found"}), 404

    resp = send_file(path, as_attachment=True)
    try:
        os.remove(path)
    except:
        pass
    return resp

# --------------------
# AUTO CLEANUP THREAD
# --------------------
def cleanup_loop():
    while True:
        now = time.time()
        for k, v in list(downloads.items()):
            if now - v.get("created", now) > DOWNLOAD_TTL:
                file = v.get("filename")
                if file:
                    p = DOWNLOAD_DIR / file
                    if p.exists():
                        p.unlink()
                downloads.pop(k, None)
        time.sleep(60)

threading.Thread(target=cleanup_loop, daemon=True).start()

# --------------------
# START
# --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
