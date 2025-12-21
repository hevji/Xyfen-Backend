# backend.py - Flask Backend for YouTube Downloader with Progress Streaming
# Run with: python backend.py

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

app = Flask(__name__)
CORS(app)

# Store for tracking download progress
downloads = {}
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

def validate_youtube_url(url):
    """Validate that the URL is a valid YouTube URL"""
    youtube_regex = r'^(https?://)?(www\.)?(youtube\.com/(watch\?v=|embed/|v/|shorts/)|youtu\.be/)[a-zA-Z0-9_-]{11}'
    return bool(re.match(youtube_regex, url))

def format_duration(seconds):
    """Convert seconds to MM:SS or HH:MM:SS format"""
    if not seconds:
        return "Unknown"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def format_views(views):
    """Format view count with K/M suffix"""
    if not views:
        return "Unknown"
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}M views"
    if views >= 1_000:
        return f"{views / 1_000:.1f}K views"
    return f"{views} views"

@app.route('/api/fetch', methods=['POST'])
def fetch_video():
    """Fetch video metadata from YouTube URL"""
    try:
        data = request.get_json()
        url = data.get('url', '')
        
        if not url or not validate_youtube_url(url):
            return jsonify({'error': 'Invalid YouTube URL'}), 400
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            formats = []
            seen_qualities = set()
            
            for f in info.get('formats', []):
                height = f.get('height')
                if height and height >= 360:
                    quality = f"{height}p"
                    if quality not in seen_qualities:
                        seen_qualities.add(quality)
                        filesize = f.get('filesize') or f.get('filesize_approx')
                        size_str = f"{filesize / (1024*1024):.1f} MB" if filesize else "Unknown"
                        formats.append({
                            'quality': quality,
                            'format': f.get('ext', 'mp4'),
                            'size': size_str
                        })
            
            formats.sort(key=lambda x: int(x['quality'].replace('p', '')), reverse=True)
            
            return jsonify({
                'title': info.get('title', 'Unknown'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': format_duration(info.get('duration')),
                'views': format_views(info.get('view_count')),
                'channel': info.get('uploader', 'Unknown'),
                'formats': formats[:4]
            })
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def download_worker(download_id, url, quality):
    """Background worker for downloading videos"""
    try:
        downloads[download_id] = {
            'status': 'downloading',
            'progress': 0,
            'stage': 'downloading'
        }
        
        height = int(quality.replace('p', ''))
        filename = f"{download_id}.mp4"
        filepath = DOWNLOAD_DIR / filename
        
        def progress_hook(d):
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded = d.get('downloaded_bytes', 0)
                if total > 0:
                    progress = (downloaded / total) * 100
                    downloads[download_id]['progress'] = min(progress, 99)
                    downloads[download_id]['stage'] = 'downloading'
            elif d['status'] == 'finished':
                downloads[download_id]['progress'] = 95
                downloads[download_id]['stage'] = 'converting'
        
        ydl_opts = {
            'format': f'bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height}][ext=mp4]/best',
            'outtmpl': str(filepath.with_suffix('.%(ext)s')),
            'progress_hooks': [progress_hook],
            'merge_output_format': 'mp4',
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        
        # Find the downloaded file (might have different extension initially)
        actual_file = None
        for ext in ['mp4', 'webm', 'mkv']:
            check_path = DOWNLOAD_DIR / f"{download_id}.{ext}"
            if check_path.exists():
                actual_file = check_path
                break
        
        if actual_file and actual_file.suffix != '.mp4':
            actual_file.rename(filepath)
        
        downloads[download_id] = {
            'status': 'ready',
            'progress': 100,
            'filename': filename,
            'stage': 'complete'
        }
        
    except Exception as e:
        downloads[download_id] = {
            'status': 'error',
            'error': str(e),
            'progress': 0
        }

@app.route('/api/download/stream')
def download_stream():
    """SSE endpoint for streaming download progress"""
    url = request.args.get('url', '')
    quality = request.args.get('quality', '720p')
    download_id = request.args.get('id', str(uuid.uuid4()))
    
    if not url or not validate_youtube_url(url):
        return jsonify({'error': 'Invalid YouTube URL'}), 400
    
    # Start download in background thread
    thread = threading.Thread(target=download_worker, args=(download_id, url, quality))
    thread.start()
    
    def generate():
        while True:
            if download_id in downloads:
                status = downloads[download_id]
                
                if status['status'] == 'downloading' or status['status'] == 'converting':
                    yield f"data: {json.dumps({'type': 'progress', 'progress': status['progress'], 'stage': status.get('stage', 'downloading')})}\n\n"
                elif status['status'] == 'ready':
                    yield f"data: {json.dumps({'type': 'complete', 'filename': status['filename']})}\n\n"
                    break
                elif status['status'] == 'error':
                    yield f"data: {json.dumps({'type': 'error', 'message': status.get('error', 'Unknown error')})}\n\n"
                    break
            
            time.sleep(0.5)
    
    return Response(generate(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'Access-Control-Allow-Origin': '*'
    })

@app.route('/api/download', methods=['POST'])
def start_download():
    """Start a download (for polling fallback)"""
    try:
        data = request.get_json()
        url = data.get('url', '')
        quality = data.get('quality', '720p')
        download_id = data.get('id', str(uuid.uuid4()))
        
        if not url or not validate_youtube_url(url):
            return jsonify({'error': 'Invalid YouTube URL'}), 400
        
        thread = threading.Thread(target=download_worker, args=(download_id, url, quality))
        thread.start()
        
        return jsonify({'status': 'started', 'id': download_id})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download/status/<download_id>')
def download_status(download_id):
    """Get download status (for polling)"""
    if download_id not in downloads:
        return jsonify({'status': 'not_found'}), 404
    
    status = downloads[download_id]
    return jsonify(status)

@app.route('/api/download/file/<filename>')
def download_file(filename):
    """Serve the downloaded file"""
    filepath = DOWNLOAD_DIR / filename
    
    if not filepath.exists():
        return jsonify({'error': 'File not found'}), 404
    
    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename,
        mimetype='video/mp4'
    )

if __name__ == '__main__':
    print("🚀 Starting Flask server on http://localhost:5000")
    print("📡 Frontend should connect to this URL")
    print("📁 Downloads will be saved to:", DOWNLOAD_DIR.absolute())
    app.run(debug=True, port=5000, threaded=True)