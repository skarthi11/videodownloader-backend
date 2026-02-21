from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
import os
import uuid
from typing import Optional

app = FastAPI(title="VideoDownloader API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs: dict = {}

class VideoInfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    title: Optional[str] = "video"

def sanitize_filename(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).strip()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/video-info")
def get_video_info(req: VideoInfoRequest):
    ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            formats = []
            seen = set()
            for f in info.get("formats", []):
                height = f.get("height")
                ext = f.get("ext", "mp4")
                fid = f.get("format_id")
                vcodec = f.get("vcodec", "none")
                acodec = f.get("acodec", "none")
                if vcodec == "none" or not height:
                    continue
                label = f"{height}p ({ext})"
                if label in seen:
                    continue
                seen.add(label)
                formats.append({
                    "format_id": fid,
                    "label": label,
                    "height": height,
                    "ext": ext,
                    "has_audio": acodec != "none",
                    "filesize": f.get("filesize"),
                })
            formats.sort(key=lambda x: x["height"], reverse=True)
            return {
                "title": info.get("title", "Unknown"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
                "formats": formats,
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

def do_download(job_id: str, url: str, format_id: str, title: str):
    jobs[job_id]["status"] = "downloading"
    safe_title = sanitize_filename(title)
    output_template = os.path.join(DOWNLOAD_DIR, f"{safe_title}_%(height)sp.%(ext)s")

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                jobs[job_id]["progress"] = round((downloaded / total) * 100, 1)
        elif d["status"] == "finished":
            jobs[job_id]["status"] = "processing"

    ydl_opts = {
        'format': f'{format_id}+bestaudio[ext=m4a]/bestaudio/{format_id}',
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'progress_hooks': [progress_hook],
        'quiet': True,
        'no_warnings': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not filename.endswith(".mp4"):
                filename = os.path.splitext(filename)[0] + ".mp4"
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["file_path"] = filename
            jobs[job_id]["filename"] = os.path.basename(filename)
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

@app.post("/download/start")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued", "progress": 0}
    background_tasks.add_task(do_download, job_id, req.url, req.format_id, req.title or "video")
    return {"job_id": job_id}

@app.get("/download/status/{job_id}")
def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.get("/download/file/{job_id}")
def get_file(job_id: str):
    from fastapi.responses import FileResponse
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "done":
        raise HTTPException(status_code=400, detail="Download not ready yet")
    file_path = job.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found on server")
    return FileResponse(path=file_path, media_type="video/mp4", filename=job.get("filename", "video.mp4"))
