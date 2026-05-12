"""
YT-Downloader Backend – FastAPI Server (Streaming Edition)
Resolves direct CDN URLs and streams them straight to the browser.
No temporary files are created on disk.
Uses standalone yt-dlp binary for maximum compatibility.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
import subprocess
import os
import uuid
import json
import threading
import time
import asyncio
from typing import Optional
from pydantic import BaseModel
import httpx
from urllib.parse import quote
import traceback
import sys

app = FastAPI(title="YT-DLP Streaming Server")

# -------------------------------------------------------------------
# Find yt-dlp binary
# -------------------------------------------------------------------
def find_ytdlp_binary():
    """Find the yt-dlp binary in current directory or PATH."""
    for name in ['yt-dlp', 'yt-dlp.exe', './yt-dlp', './yt-dlp_linux', './yt-dlp_macos']:
        if os.path.isfile(name) and os.access(name, os.X_OK):
            return os.path.abspath(name)
    if sys.platform == 'win32':
        result = subprocess.run(['where', 'yt-dlp'], capture_output=True, text=True)
    else:
        result = subprocess.run(['which', 'yt-dlp'], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split('\n')[0]
    return None

YTDLP_PATH = find_ytdlp_binary()
if not YTDLP_PATH:
    print("❌ yt-dlp binary not found!")
    print("   Download from: https://github.com/yt-dlp/yt-dlp/releases")
    exit(1)
print(f"✅ yt-dlp: {YTDLP_PATH}")

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
# Format helpers
# -------------------------------------------------------------------
def is_downloadable_format(f):
    """Check if a format ID is likely to give a direct file (not HLS manifest)."""
    fmt_id = f.get('format_id', '')
    protocol = f.get('protocol', '')
    ext = f.get('ext', '')
    url = f.get('url', '')
    vcodec = f.get('vcodec', 'none')
    
    # Skip audio-only here (handled separately)
    if vcodec == 'none':
        return False
    
    # Skip HLS/m3u8 formats
    if 'm3u8' in fmt_id.lower() or 'hls' in fmt_id.lower():
        return False
    if 'm3u8' in protocol.lower():
        return False
    if 'm3u8' in ext.lower():
        return False
    if 'manifest' in url.lower() or 'm3u8' in url.lower():
        return False
    
    return True

def calculate_display_size(f, duration):
    """Calculate a human-readable size string for a format."""
    filesize = f.get('filesize') or f.get('filesize_approx', 0)
    tbr = f.get('tbr', 0)
    vbr = f.get('vbr', 0)
    abr = f.get('abr', 0)
    
    if filesize > 0:
        mb = filesize / (1024 * 1024)
        if mb >= 1000:
            return f" ~{mb/1024:.1f}GB"
        return f" ~{mb:.0f}MB"
    
    if tbr > 0 and duration > 0:
        size_mb = (tbr * 1000 * duration) / (8 * 1024 * 1024)
        if size_mb >= 1000:
            return f" ~{size_mb/1024:.1f}GB"
        return f" ~{size_mb:.0f}MB"
    
    if vbr > 0 and duration > 0:
        total_br = vbr + (abr if abr > 0 else 128)
        size_mb = (total_br * 1000 * duration) / (8 * 1024 * 1024)
        if size_mb >= 1000:
            return f" ~{size_mb/1024:.1f}GB"
        return f" ~{size_mb:.0f}MB"
    
    # Rough estimates based on resolution
    height = f.get('height', 0)
    if height >= 4320: return " ~15-30GB"
    if height >= 2160: return " ~4-8GB"
    if height >= 1440: return " ~2-4GB"
    if height >= 1080: return " ~1-3GB"
    if height >= 720: return " ~500MB-1GB"
    if height >= 480: return " ~200-500MB"
    return ""

def get_resolution_label(height):
    """Get a human-readable resolution label."""
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
# Video Info - show downloadable qualities
# -------------------------------------------------------------------
@app.post("/api/info")
async def video_info(request: Request):
    """Fetch ALL available formats from the video."""
    try:
        data = await request.json()
        url = data.get('url', '').strip()
        if not url:
            return JSONResponse(status_code=400, content={"error": "URL required"})

        result = subprocess.run(
            [YTDLP_PATH, '-j', '--no-warnings', url],
            capture_output=True, text=True, timeout=45
        )
        if result.returncode != 0:
            raise Exception(f"Failed to fetch: {result.stderr.strip()}")

        info = json.loads(result.stdout)
        duration = info.get('duration', 0)
        print(f"✅ Info for: {info.get('title', 'Unknown')}")

        formats = []
        heights_seen = set()

        # ---- VIDEO FORMATS ----
        # First pass: collect all downloadable formats
        downloadable = []
        for f in info.get('formats', []):
            if not is_downloadable_format(f):
                continue
            
            height = f.get('height') or 0
            if height < 144:
                continue
            if 'storyboard' in f.get('format_id', '').lower():
                continue
            
            downloadable.append(f)
        
        # Sort by height desc, then by preference (has audio, larger filesize)
        downloadable.sort(key=lambda f: (
            -(f.get('height') or 0),
            -(1 if f.get('acodec', 'none') != 'none' else 0),
            -(f.get('filesize') or f.get('filesize_approx', 0)),
        ))
        
        # Keep best per resolution
        for f in downloadable:
            height = f.get('height') or 0
            fmt_id = f.get('format_id', '')
            vcodec = f.get('vcodec', '').split('.')[0].upper() if f.get('vcodec') else ''
            acodec = f.get('acodec', 'none')
            ext = f.get('ext', 'mp4')
            fps = f.get('fps', 0)
            has_audio = acodec != 'none'
            
            if height in heights_seen:
                continue
            heights_seen.add(height)
            
            # Build label
            res_label = get_resolution_label(height)
            label = f"🎬 {res_label}"
            if fps > 30:
                label += f" {fps}fps"
            if vcodec and vcodec not in ('UNKNOWN', ''):
                label += f" {vcodec}"
            if has_audio:
                label += " 🔊"
            
            size_str = calculate_display_size(f, duration)
            if size_str:
                label += size_str
            
            formats.append({
                'id': fmt_id,
                'quality': label,
                'height': height,
                'ext': ext,
                'type': 'video',
                'has_audio': has_audio,
                'filesize': f.get('filesize') or f.get('filesize_approx', 0),
            })
        
        # Sort formats by height desc
        formats.sort(key=lambda x: x['height'], reverse=True)
        
        # Add BEST options at top
        formats.insert(0, {
            'id': 'bestvideo+bestaudio/best',
            'quality': '⭐ BEST QUALITY (Video+Audio)',
            'height': 99999,
            'ext': 'mp4',
            'type': 'video',
            'has_audio': True,
            'filesize': 0,
        })
        formats.insert(1, {
            'id': 'best',
            'quality': '🎯 Best Single File (Already Merged)',
            'height': 99998,
            'ext': 'mp4',
            'type': 'video',
            'has_audio': True,
            'filesize': 0,
        })
        
        # ---- AUDIO FORMATS ----
        formats.append({
            'id': 'bestaudio/best',
            'quality': '🎵 MP3 320kbps (Best Audio)',
            'ext': 'mp3',
            'type': 'audio',
            'bitrate': '320'
        })
        formats.append({
            'id': 'bestaudio/best',
            'quality': '🎵 MP3 192kbps (Good)',
            'ext': 'mp3',
            'type': 'audio',
            'bitrate': '192'
        })
        formats.append({
            'id': 'bestaudio/best',
            'quality': '🎵 MP3 128kbps (Small)',
            'ext': 'mp3',
            'type': 'audio',
            'bitrate': '128'
        })
        
        print(f"✅ {len(formats)} formats available")
        for f in formats[:10]:
            print(f"   {f['quality']}")

        return JSONResponse({
            'title': info.get('title', 'Untitled'),
            'duration': duration,
            'thumbnail': info.get('thumbnail', ''),
            'uploader': info.get('uploader', 'Unknown'),
            'formats': formats
        })
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

# -------------------------------------------------------------------
# Prepare download - resolve the actual CDN URL
# -------------------------------------------------------------------
@app.post("/api/download")
async def prepare_download(request: DownloadRequest):
    """Resolve download URL. Chooses a working format if the selected one fails."""
    url = request.url
    format_type = request.format_type
    format_id = request.format_id
    bitrate = request.bitrate
    title_hint = request.title

    print(f"\n📥 Resolving: {url}")
    print(f"   Format ID: {format_id}, Type: {format_type}")

    try:
        if format_type == 'audio':
            selector = "bestaudio/best"
            ext = "mp3"
        else:
            selector = format_id if format_id else "bestvideo+bestaudio/best"
            ext = "mp4"

        print(f"   Selector: {selector}")

        # --- TRY TO GET DIRECT URL ---
        result = subprocess.run(
            [YTDLP_PATH, '-g', '-f', selector, '--no-warnings', url],
            capture_output=True, text=True, timeout=30
        )

        if result.returncode != 0:
            print(f"   ⚠️  Failed, trying 'best'...")
            result = subprocess.run(
                [YTDLP_PATH, '-g', '-f', 'best', '--no-warnings', url],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                raise Exception(f"Cannot resolve URL: {result.stderr.strip()}")
            selector = "best"

        output_lines = result.stdout.strip().split('\n')
        urls = [line.strip() for line in output_lines if line.strip().startswith('http')]

        if not urls:
            raise Exception("No download URLs found")

        print(f"   Got {len(urls)} URL(s)")

        direct_url = urls[0]

        # --- CHECK FOR HLS/MANIFEST ---
        is_manifest = 'm3u8' in direct_url.lower() or 'manifest' in direct_url.lower()

        if is_manifest or (len(urls) > 1 and any('m3u8' in u.lower() for u in urls)):
            print(f"   ⚠️  Manifest detected, falling back to 'best'...")
            
            # Try 'best' format
            result = subprocess.run(
                [YTDLP_PATH, '-g', '-f', 'best', '--no-warnings', url],
                capture_output=True, text=True, timeout=30
            )
            
            if result.returncode == 0:
                new_urls = [l.strip() for l in result.stdout.split('\n') if l.strip().startswith('http')]
                # Find first non-manifest URL
                found = False
                for u in new_urls:
                    if 'm3u8' not in u.lower() and 'manifest' not in u.lower():
                        direct_url = u
                        found = True
                        print(f"   ✅ Found direct URL via 'best'")
                        break
                
                if not found:
                    # Even 'best' gave manifest - try individual format IDs
                    print(f"   Trying individual format '22' (720p)...")
                    result = subprocess.run(
                        [YTDLP_PATH, '-g', '-f', '22', '--no-warnings', url],
                        capture_output=True, text=True, timeout=30
                    )
                    if result.returncode == 0:
                        final_urls = [l.strip() for l in result.stdout.split('\n') if l.strip().startswith('http')]
                        for u in final_urls:
                            if 'm3u8' not in u.lower() and 'manifest' not in u.lower():
                                direct_url = u
                                found = True
                                print(f"   ✅ Found direct URL via format 22")
                                break
                    
                    if not found:
                        raise Exception(
                            "This video only provides HLS streaming for all formats. "
                            "Unable to download directly. Try a different video."
                        )
            else:
                raise Exception(f"Failed to resolve any format: {result.stderr.strip()}")

        print(f"   ✅ Final URL: {direct_url[:100]}...")

        # Get title
        title = title_hint
        if not title:
            info_result = subprocess.run(
                [YTDLP_PATH, '-j', '--no-warnings', url],
                capture_output=True, text=True, timeout=30
            )
            if info_result.returncode == 0:
                info = json.loads(info_result.stdout)
                title = info.get('title', 'video')

        mime = "audio/mpeg" if format_type == 'audio' else "video/mp4"
        download_id = str(uuid.uuid4())

        with downloads_lock:
            downloads[download_id] = {
                "id": download_id,
                "url": direct_url,
                "title": title,
                "ext": ext,
                "mimetype": mime,
                "time": time.time(),
            }

        return JSONResponse({
            "download_id": download_id,
            "title": title,
        })

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

# -------------------------------------------------------------------
# Stream file directly to browser
# -------------------------------------------------------------------
@app.get("/api/download/{download_id}/file")
async def stream_file(download_id: str):
    """Stream the file with correct filename."""
    with downloads_lock:
        dl = downloads.get(download_id)
        if not dl:
            raise HTTPException(status_code=404, detail="Download not found")

        direct_url = dl["url"]
        title = dl["title"]
        ext = dl["ext"]
        mimetype = dl.get("mimetype", "video/mp4")

    safe_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
    if not safe_title:
        safe_title = "video"
    filename = f"{safe_title}.{ext}"
    safe_filename = quote(filename)

    print(f"\n🔄 Streaming: {filename}")

    async def chunk_generator():
        sent_bytes = 0
        try:
            timeout = httpx.Timeout(10.0, read=600.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': '*/*',
                    'Accept-Encoding': 'identity',
                }
                async with client.stream("GET", direct_url, headers=headers) as remote:
                    if remote.status_code != 200:
                        raise Exception(f"CDN error: HTTP {remote.status_code}")

                    async for chunk in remote.aiter_bytes(chunk_size=1024*1024):
                        sent_bytes += len(chunk)
                        yield chunk

            mb = sent_bytes / (1024*1024)
            print(f"   ✅ Complete: {mb:.1f}MB")

        except Exception as e:
            print(f"   ❌ Stream error: {str(e)}")
            raise

    return StreamingResponse(
        chunk_generator(),
        media_type=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
        }
    )

# -------------------------------------------------------------------
# Entry Point
# -------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print(f"✅ yt-dlp: {YTDLP_PATH}")
    print("\n🔥 YT-DLP Streaming Server")
    print("➡️  http://127.0.0.1:5000\n")
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="info")