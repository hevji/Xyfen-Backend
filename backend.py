# backend.py - Optimized Flask Backend for YouTube Downloader

from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import yt_dlp
import os
import time
import uuid
import threading
from pathlib import Path
from collections import defaultdict
import json
import requests

app = Flask(__name__)
CORS(app)

DOWNLOAD_TTL = 600        # 10 minutes
RATE_LIMIT = 3
RATE_WINDOW = 600
downloads = defaultdict(dict)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# --------------------
# Helpers
# --------------------
def get_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr)

def rate_limited(ip):
    now = time.time()
    if ip not in downloads:
        downloads[ip] = []
    downloads[ip] = [t for t in downloads[ip] if now - t < RATE_WINDOW]
    if len(downloads[ip]) >= RATE_LIMIT:
        return True
    downloads[ip].append(now)
    return False

def validate_youtube_url(url):
    return "youtube.com" in url or "youtu.be" in url

# --------------------
# Download Worker (streamed)
# --------------------
def download_worker(download_id, url, height):
    downloads[download_id] = {"status": "downloading", "progress": 0, "filename": None, "created": time.time()}
    filepath = DOWNLOAD_DIR / f"{download_id}.mp4"

    ydl_opts = {
        "format": f"bestvideo[height<={height}]+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            video_url = info.get("url")
            
        # Stream download in chunks
        with requests.get(video_url, stream=True) as r, open(filepath, "wb") as f:
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        downloads[download_id]["progress"] = min((downloaded / total) * 100, 100)

        downloads[download_id].update({"status": "ready", "progress": 100, "filename": filepath.name})
    except Exception as e:
        downloads[download_id].update({"status": "error", "error": str(e)})

# --------------------
# SSE Progress Stream
# --------------------
@app.route("/api/download/stream")
def download_stream():
    url = request.args.get("url")
    quality = request.args.get("quality", "720p")
    download_id = str(uuid.uuid4())
    height = int(quality.replace("p", ""))

    threading.Thread(target=download_worker, args=(download_id, url, height), daemon=True).start()

    def gen():
        last_progress = -1
        while True:
            d = downloads.get(download_id)
            if not d:
                break
            status = d.get("status")
            progress = int(d.get("progress", 0))

            if progress != last_progress or status in ["ready", "error"]:
                last_progress = progress
                if status == "ready":
                    yield f"data: {json.dumps({'type':'complete','filename':d['filename']})}\n\n"
                    break
                elif status == "error":
                    yield f"data: {json.dumps({'type':'error','message':d['error']})}\n\n"
                    break
                else:
                    yield f"data: {json.dumps({'type':'progress','progress':progress})}\n\n"

            time.sleep(0.1)  # tiny sleep to prevent tight loop

    return Response(gen(), mimetype="text/event-stream")

# --------------------
# Serve Downloaded File
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
# Cleanup Thread
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
# Run
# --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
