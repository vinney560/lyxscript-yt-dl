"""
YT-Downloader Backend – FastAPI Server (Streaming Edition)
Uses yt-dlp Python module - no binary needed.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
import json
import threading
import time
import asyncio
from typing import Optional
from pydantic import BaseModel
import httpx
from urllib.parse import quote
import traceback
import yt_dlp

app = FastAPI(title="YT-DLP Streaming Server")

# -------------------------------------------------------------------
# Storage
# -------------------------------------------------------------------
downloads = {}
downloads_lock = threading.RLock()

# -------------------------------------------------------------------
# Models
# -------------------------------------------------------------------
class URLRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_type: str = "video"
    format_id: Optional[str] = None
    bitrate: str = "192"
    title: Optional[str] = None

# -------------------------------------------------------------------
# Helper: Extract info using Python API
# -------------------------------------------------------------------
def get_video_info(url):
    """Get video info using yt-dlp Python API."""
    opts = {
        'quiet': True,
        'no_warnings': True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def get_direct_url(url, format_selector):
    """Get direct download URL for a format."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'format': format_selector,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        # Return the URL from the selected format
        if 'url' in info:
            return info['url']
        # For combined formats, get from requested_formats
        if 'requested_formats' in info:
            return info['requested_formats'][0]['url']
        raise Exception("Could not extract direct URL")

# -------------------------------------------------------------------
# Format helpers
# -------------------------------------------------------------------
def is_downloadable_format(f):
    """Check if a format ID is likely to give a direct file."""
    fmt_id = f.get('format_id', '')
    protocol = f.get('protocol', '')
    url = f.get('url', '')
    vcodec = f.get('vcodec', 'none')
    if vcodec == 'none': return False
    if 'm3u8' in fmt_id.lower() or 'hls' in fmt_id.lower(): return False
    if 'm3u8' in protocol.lower(): return False
    if 'manifest' in url.lower() or 'm3u8' in url.lower(): return False
    return True

def calculate_display_size(f, duration):
    """Calculate human-readable size string."""
    filesize = f.get('filesize') or f.get('filesize_approx', 0)
    tbr = f.get('tbr', 0)
    vbr = f.get('vbr', 0)
    abr = f.get('abr', 0)
    
    if filesize > 0:
        mb = filesize / (1024 * 1024)
        return f" ~{mb/1024:.1f}GB" if mb >= 1000 else f" ~{mb:.0f}MB"
    if tbr > 0 and duration > 0:
        size_mb = (tbr * 1000 * duration) / (8 * 1024 * 1024)
        return f" ~{size_mb/1024:.1f}GB" if size_mb >= 1000 else f" ~{size_mb:.0f}MB"
    if vbr > 0 and duration > 0:
        total_br = vbr + (abr if abr > 0 else 128)
        size_mb = (total_br * 1000 * duration) / (8 * 1024 * 1024)
        return f" ~{size_mb/1024:.1f}GB" if size_mb >= 1000 else f" ~{size_mb:.0f}MB"
    return ""

def get_resolution_label(height):
    if height >= 4320: return "8K"
    if height >= 2160: return "4K"
    if height >= 1440: return "2K (1440p)"
    if height >= 1080: return "1080p Full HD"
    if height >= 720: return "720p HD"
    if height >= 480: return "480p"
    if height >= 360: return "360p"
    if height >= 240: return "240p"
    return f"{height}p"

# -------------------------------------------------------------------
# Frontend
# -------------------------------------------------------------------
@app.get("/")
async def index():
    try:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader("templates"))
        template = env.get_template("yt-downloader.html")
        return HTMLResponse(template.render())
    except Exception as e:
        return HTMLResponse(f"<h1>Error: {e}</h1>")

# -------------------------------------------------------------------
# Video Info
# -------------------------------------------------------------------
@app.post("/api/info")
async def video_info(request: Request):
    try:
        data = await request.json()
        url = data.get('url', '').strip()
        if not url:
            return JSONResponse(status_code=400, content={"error": "URL required"})

        info = await asyncio.to_thread(get_video_info, url)
        duration = info.get('duration', 0)
        print(f"✅ Info for: {info.get('title', 'Unknown')}")

        formats = []
        heights_seen = set()
        downloadable = []

        for f in info.get('formats', []):
            if not is_downloadable_format(f): continue
            height = f.get('height') or 0
            if height < 144: continue
            if 'storyboard' in f.get('format_id', '').lower(): continue
            downloadable.append(f)

        downloadable.sort(key=lambda f: (
            -(f.get('height') or 0),
            -(1 if f.get('acodec', 'none') != 'none' else 0),
            -(f.get('filesize') or f.get('filesize_approx', 0)),
        ))

        for f in downloadable:
            height = f.get('height') or 0
            if height in heights_seen: continue
            heights_seen.add(height)
            
            vcodec = f.get('vcodec', '').split('.')[0].upper() if f.get('vcodec') else ''
            fps = f.get('fps', 0)
            has_audio = f.get('acodec', 'none') != 'none'
            
            label = f"🎬 {get_resolution_label(height)}"
            if fps > 30: label += f" {fps}fps"
            if vcodec and vcodec not in ('UNKNOWN', ''): label += f" {vcodec}"
            if has_audio: label += " 🔊"
            size_str = calculate_display_size(f, duration)
            if size_str: label += size_str

            formats.append({
                'id': f.get('format_id', ''),
                'quality': label,
                'height': height,
                'ext': f.get('ext', 'mp4'),
                'type': 'video',
                'has_audio': has_audio,
                'filesize': f.get('filesize') or f.get('filesize_approx', 0),
            })

        formats.sort(key=lambda x: x['height'], reverse=True)
        
        formats.insert(0, {'id': 'bestvideo+bestaudio/best', 'quality': '⭐ BEST QUALITY (Video+Audio)', 'height': 99999, 'ext': 'mp4', 'type': 'video', 'has_audio': True, 'filesize': 0})
        formats.insert(1, {'id': 'best', 'quality': '🎯 Best Single File (Already Merged)', 'height': 99998, 'ext': 'mp4', 'type': 'video', 'has_audio': True, 'filesize': 0})
        
        formats.append({'id': 'bestaudio/best', 'quality': '🎵 MP3 320kbps (Best Audio)', 'ext': 'mp3', 'type': 'audio', 'bitrate': '320'})
        formats.append({'id': 'bestaudio/best', 'quality': '🎵 MP3 192kbps (Good)', 'ext': 'mp3', 'type': 'audio', 'bitrate': '192'})
        formats.append({'id': 'bestaudio/best', 'quality': '🎵 MP3 128kbps (Small)', 'ext': 'mp3', 'type': 'audio', 'bitrate': '128'})

        return JSONResponse({
            'title': info.get('title', 'Untitled'),
            'duration': duration,
            'thumbnail': info.get('thumbnail', ''),
            'uploader': info.get('uploader', 'Unknown'),
            'formats': formats
        })
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# -------------------------------------------------------------------
# Prepare download
# -------------------------------------------------------------------
@app.post("/api/download")
async def prepare_download(request: DownloadRequest):
    url = request.url
    format_type = request.format_type
    format_id = request.format_id
    title_hint = request.title

    print(f"\n📥 Resolving: {url}")

    try:
        selector = format_id if format_id else "bestvideo+bestaudio/best"
        if format_type == 'audio':
            selector = "bestaudio/best"
            ext = "mp3"
        else:
            ext = "mp4"

        # Try to get direct URL
        direct_url = await asyncio.to_thread(get_direct_url, url, selector)
        
        # If it's a manifest, fallback to 'best'
        if 'm3u8' in str(direct_url).lower() or 'manifest' in str(direct_url).lower():
            print("   ⚠️  Manifest detected, falling back to 'best'...")
            direct_url = await asyncio.to_thread(get_direct_url, url, 'best')

        # Get title
        title = title_hint
        if not title:
            info = get_video_info(url)
            title = info.get('title', 'video')

        mime = "audio/mpeg" if format_type == 'audio' else "video/mp4"
        download_id = str(__import__('uuid').uuid4())

        with downloads_lock:
            downloads[download_id] = {
                "id": download_id,
                "url": direct_url,
                "title": title,
                "ext": ext,
                "mimetype": mime,
                "time": time.time(),
            }

        return JSONResponse({"download_id": download_id, "title": title})

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return JSONResponse(status_code=500, content={"error": str(e)})

# -------------------------------------------------------------------
# Stream file
# -------------------------------------------------------------------
@app.get("/api/download/{download_id}/file")
async def stream_file(download_id: str):
    with downloads_lock:
        dl = downloads.get(download_id)
        if not dl:
            raise HTTPException(status_code=404, detail="Not found")

    safe_title = "".join(c for c in dl["title"] if c.isalnum() or c in (' ', '-', '_')).strip() or "video"
    filename = f"{safe_title}.{dl['ext']}"

    async def chunk_generator():
        try:
            timeout = httpx.Timeout(10.0, read=600.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                headers = {'User-Agent': 'Mozilla/5.0', 'Accept': '*/*', 'Accept-Encoding': 'identity'}
                async with client.stream("GET", dl["url"], headers=headers) as remote:
                    if remote.status_code != 200:
                        raise Exception(f"CDN error: HTTP {remote.status_code}")
                    async for chunk in remote.aiter_bytes(chunk_size=1024*1024):
                        yield chunk
        except Exception as e:
            print(f"❌ Stream error: {str(e)}")
            raise

    return StreamingResponse(
        chunk_generator(),
        media_type=dl["mimetype"],
        headers={"Content-Disposition": f'attachment; filename="{quote(filename)}"'}
    )

# -------------------------------------------------------------------
# Entry Point
# -------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print("\n🔥 YT-DLP Streaming Server (Python Module)")
    print("➡️  http://127.0.0.1:5000\n")
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="info")