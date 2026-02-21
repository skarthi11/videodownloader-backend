import os
import uuid
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI()

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory job store ───────────────────────────────────────────────────────
jobs = {}

# ── Request models ────────────────────────────────────────────────────────────
class VideoInfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    title: str

# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}

# ── Video info ────────────────────────────────────────────────────────────────
@app.post("/video-info")
def get_video_info(req: VideoInfoRequest):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(req.url, download=False)

    formats = []
    seen = set()

    for f in info.get("formats", []):
        height = f.get("height") or 0
        has_video = f.get("vcodec", "none") != "none"
        has_audio = f.get("acodec", "none") != "none"

        if not has_video or height < 144:
            continue

        label = f"{height}p"
        if has_audio:
            label += " (with audio)"

        if label in seen:
            continue
        seen.add(label)

        formats.append({
            "format_id": f["format_id"],
            "label": label,
            "height": height,
            "ext": f.get("ext", "mp4"),
            "has_audio": has_audio,
            "filesize": f.get("filesize"),
        })

    # Sort best quality first
    formats.sort(key=lambda x: x["height"], reverse=True)

    # If no combined formats, add a best overall option
    if not formats:
        formats.append({
            "format_id": "best",
            "label": "Best Quality",
            "height": 0,
            "ext": "mp4",
            "has_audio": True,
            "filesize": None,
        })

    return {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "formats": formats,
    }

# ── Start download ────────────────────────────────────────────────────────────
@app.post("/download/start")
async def start_download(req: DownloadRequest):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "downloading", "progress": 0, "file_path": None, "filename": None, "error": None}

    asyncio.create_task(run_download(job_id, req.url, req.format_id, req.title))
    return {"job_id": job_id}

async def run_download(job_id: str, url: str, format_id: str, title: str):
    out_dir = "/tmp/downloads"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{job_id}.%(ext)s")

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total else 0
            jobs[job_id]["progress"] = min(pct, 95)
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 95
            jobs[job_id]["status"] = "processing"

    ydl_opts = {
        "format": format_id if format_id != "best" else "bestvideo+bestaudio/best",
        "outtmpl": out_path,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "merge_output_format": "mp4",
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _do_download(ydl_opts, url))

        # Find the output file
        for f in os.listdir(out_dir):
            if f.startswith(job_id):
                jobs[job_id]["file_path"] = os.path.join(out_dir, f)
                jobs[job_id]["filename"] = f"{title[:50]}.mp4"
                break

        jobs[job_id]["status"] = "done"
        jobs[job_id]["progress"] = 100
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

def _do_download(opts, url):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

# ── Job status ────────────────────────────────────────────────────────────────
@app.get("/download/status/{job_id}")
def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return {"status": "error", "error": "Job not found", "progress": 0}
    return job

# ── Serve file ────────────────────────────────────────────────────────────────
@app.get("/download/file/{job_id}")
def get_file(job_id: str):
    job = jobs.get(job_id)
    if not job or not job.get("file_path"):
        return {"error": "File not found"}
    return FileResponse(
        job["file_path"],
        media_type="video/mp4",
        filename=job.get("filename", "video.mp4"),
        headers={"Access-Control-Allow-Origin": "*"}
    )
