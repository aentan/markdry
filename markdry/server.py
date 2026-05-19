import asyncio
import json
import logging
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from .jobs import (
    JOBS_ROOT,
    MAX_FILE_SIZE,
    JobStatus,
    cleanup_sweeper,
    create_job,
    get_job,
    remove_job,
)
from .pipeline import extract_preview, get_video_info, process_job

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(cleanup_sweeper())
    # Pre-load LaMa model in background so first job starts faster.
    asyncio.create_task(_preload_model())
    yield
    task.cancel()


async def _preload_model() -> None:
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_preload)
    except Exception as exc:
        log.warning("Model pre-load failed (will load on first use): %s", exc)


def _do_preload() -> None:
    from .inpaint import load_model
    load_model()


app = FastAPI(title="markdry", lifespan=lifespan)


# ── Static / index ────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


# ── Upload ────────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    job = create_job()
    original_stem = Path(file.filename or "video").stem
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    source_path = job.dir / f"source{ext}"

    try:
        total_bytes = 0
        async with aiofiles.open(source_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_FILE_SIZE:
                    await f.close()
                    source_path.unlink(missing_ok=True)
                    shutil.rmtree(job.dir, ignore_errors=True)
                    remove_job(job.id)
                    raise HTTPException(413, "File exceeds 500 MB limit")
                await f.write(chunk)

        job.source_ext = ext
        job.original_name = original_stem

        info = await get_video_info(source_path)
        job.width = info["width"]
        job.height = info["height"]
        job.fps = info["fps"]
        job.fps_str = info["fps_str"]
        job.duration = info["duration"]
        job.frame_count = info["frame_count"]

        await extract_preview(job)

        return {
            "job_id": job.id,
            "preview_url": f"/api/preview/{job.id}",
            "width": job.width,
            "height": job.height,
            "duration": job.duration,
            "fps": job.fps,
            "frame_count": job.frame_count,
        }

    except HTTPException:
        shutil.rmtree(job.dir, ignore_errors=True)
        remove_job(job.id)
        raise
    except Exception as exc:
        log.exception("Upload failed")
        shutil.rmtree(job.dir, ignore_errors=True)
        remove_job(job.id)
        raise HTTPException(500, str(exc))


# ── Preview ───────────────────────────────────────────────────────────────────

@app.get("/api/preview/{job_id}")
async def preview(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    p = job.dir / "preview.jpg"
    if not p.exists():
        raise HTTPException(404, "Preview not ready")
    return FileResponse(p, media_type="image/jpeg")


# ── Process ───────────────────────────────────────────────────────────────────

@app.post("/api/process/{job_id}")
async def process(job_id: str, request: Request):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status == JobStatus.PROCESSING:
        raise HTTPException(409, "Job already processing")

    body = await request.json()
    rects = body.get("rects", [])
    if not rects:
        raise HTTPException(400, "No mask rectangles provided")

    job.progress_queue = asyncio.Queue()
    job.cancelled = False
    job.status = JobStatus.PROCESSING

    asyncio.create_task(process_job(job, rects))
    return {"status": "started"}


# ── SSE progress ──────────────────────────────────────────────────────────────

@app.get("/api/progress/{job_id}")
async def progress(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    async def stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        job.progress_queue.get(), timeout=25.0
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue

                yield f"data: {json.dumps(event)}\n\n"

                if event.get("type") in ("done", "cancelled", "error"):
                    break
        except (GeneratorExit, asyncio.CancelledError):
            pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Cancel ────────────────────────────────────────────────────────────────────

@app.delete("/api/job/{job_id}")
async def cancel_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.cancelled = True
    if job.current_process:
        try:
            job.current_process.kill()
        except Exception:
            pass
    return {"status": "cancelling"}


# ── Download ──────────────────────────────────────────────────────────────────

@app.get("/api/download/{job_id}")
async def download(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != JobStatus.DONE:
        raise HTTPException(400, f"Job not done (status: {job.status})")
    output = job.dir / "output.mp4"
    if not output.exists():
        raise HTTPException(404, "Output file missing")

    job.downloaded_at = time.time()
    filename = f"{job.original_name}-clean.mp4"

    return FileResponse(
        output,
        media_type="video/mp4",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
