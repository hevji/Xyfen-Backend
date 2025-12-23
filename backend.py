# backend.py - YouTube Downloader with Cookies + Ping Endpoint

from flask import Flask, request, jsonify, Response, send_file
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
from pathlib import Path
import json
import time

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIE_FILE = "cookies.txt"  # Required for anti-bot / age-restricted

downloads = {}

# --------------------
# PING ENDPOINT
# --------------------
@app.route("/api/ping")
def ping():
    return "ok", 200

# --------------------
# URL VALIDATION
# --------------------
def validate_youtube_url(url):
    return "youtube.com" in url or "youtu.be" in url

# --------------------
# DOWNLOAD WORKER
# --------------------
def download_worker(download_id, url, quality):
    downloads[download_id] = {"status": "downloading", "progress": 0}
    height = int(quality.replace("p", ""))
    filepath = DOWNLOAD_DIR / f"{download_id}.mp4"

    ydl_opts = {
        "format": f"bestvideo[height<={height}]+bestaudio/best",
        "outtmpl": str(filepath.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "cookiefile": COOKIE_FILE,
    }

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                downloads[download_id]["progress"] = min((downloaded/total)*100, 99)
        elif d["status"] == "finished":
            downloads[download_id]["progress"] = 100
            downloads[download_id]["status"] = "ready"

    ydl_opts["progress_hooks"] = [hook]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        downloads[download_id]["filename"] = filepath.name
    except Exception as e:
        downloads[download_id] = {"status": "error", "error": str(e), "progress": 0}

# --------------------
# STREAM DOWNLOAD PROGRESS (SSE)
# --------------------
@app.route("/api/download/stream")
def download_stream():
    url = request.args.get("url", "")
    quality = request.args.get("quality", "720p")
    download_id = request.args.get("id", str(uuid.uuid4()))

    if not url or not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    threading.Thread(target=download_worker, args=(download_id, url, quality), daemon=True).start()

    def generate():
        while True:
            status = downloads.get(download_id)
            if not status:
                break
            if status["status"] in ["downloading"]:
                yield f"data: {json.dumps({'type':'progress','progress':status['progress']})}\n\n"
            elif status["status"] == "ready":
                yield f"data: {json.dumps({'type':'complete','filename':status['filename']})}\n\n"
                break
            elif status["status"] == "error":
                yield f"data: {json.dumps({'type':'error','message':status.get('error','Unknown')})}\n\n"
                break
            time.sleep(0.1)

    return Response(generate(), mimetype="text/event-stream")

# --------------------
# SERVE DOWNLOADED FILE
# --------------------
@app.route("/api/download/file/<filename>")
def download_file(filename):
    path = DOWNLOAD_DIR / filename
    if not path.exists():
        return jsonify({"error": "File not found"}), 404

    response = send_file(path, as_attachment=True)
    try:
        os.remove(path)
        for key, val in list(downloads.items()):
            if val.get("filename") == filename:
                downloads.pop(key)
    except:
        pass

    return response

# --------------------
# START SERVER
# --------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
