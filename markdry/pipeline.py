import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .inpaint import inpaint_frame
from .jobs import Job, JobStatus

log = logging.getLogger(__name__)


async def _ffmpeg(*args, cwd=None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def _ffprobe(*args) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def get_video_info(source: Path) -> dict:
    rc, stdout, stderr = await _ffprobe(
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(source),
    )
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {stderr.strip()}")

    data = json.loads(stdout)
    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if not video:
        raise RuntimeError("No video stream found in file")

    width = int(video["width"])
    height = int(video["height"])

    fps_str = video.get("r_frame_rate", "30/1")
    num, den = fps_str.split("/")
    fps = float(num) / float(den) if float(den) else 30.0

    duration = float(data.get("format", {}).get("duration", 0) or 0)

    nb = video.get("nb_frames", "")
    frame_count = int(nb) if (nb and nb != "N/A") else max(1, round(duration * fps))

    return {
        "width": width,
        "height": height,
        "fps": fps,
        "fps_str": fps_str,
        "duration": duration,
        "frame_count": frame_count,
    }


async def extract_preview(job: Job) -> Path:
    source = job.dir / f"source{job.source_ext}"
    preview = job.dir / "preview.jpg"
    rc, _, stderr = await _ffmpeg(
        "-i", str(source),
        "-frames:v", "1",
        "-q:v", "3",
        str(preview),
    )
    if rc != 0:
        raise RuntimeError(f"Preview extraction failed: {stderr.strip()}")
    return preview


def _build_mask(width: int, height: int, rects: list[dict]) -> Image.Image:
    arr = np.zeros((height, width), dtype=np.uint8)
    for r in rects:
        x = int(max(0, r["x"]))
        y = int(max(0, r["y"]))
        w = int(min(r["w"], width - x))
        h = int(min(r["h"], height - y))
        if w > 0 and h > 0:
            arr[y:y + h, x:x + w] = 255
    return Image.fromarray(arr, mode="L")


async def _extract_audio(source: Path, audio_path: Path) -> bool:
    # Try stream copy first, then AAC transcode as fallback.
    for extra in (("-acodec", "copy"), ("-acodec", "aac")):
        rc, _, _ = await _ffmpeg(
            "-i", str(source),
            "-vn", *extra,
            str(audio_path),
        )
        if rc == 0 and audio_path.exists() and audio_path.stat().st_size > 256:
            return True
    return False


async def process_job(job: Job, rects: list[dict]) -> None:
    job.status = JobStatus.PROCESSING
    frames_dir = job.dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    source = job.dir / f"source{job.source_ext}"
    audio_path = job.dir / "audio.m4a"
    tmp_video = job.dir / "output_video.mp4"
    output = job.dir / "output.mp4"

    async def emit(event: dict) -> None:
        await job.progress_queue.put(event)

    try:
        # ── 1. Extract audio (parallel-safe, fast) ────────────────────────
        log.info("[%s] Extracting audio…", job.id)
        has_audio = await _extract_audio(source, audio_path)
        log.info("[%s] Audio present: %s", job.id, has_audio)

        # ── 2. Extract frames ─────────────────────────────────────────────
        log.info("[%s] Extracting frames…", job.id)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", str(source),
            "-vsync", "0",
            str(frames_dir / "%06d.png"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        job.current_process = proc
        _, stderr_bytes = await proc.communicate()
        job.current_process = None

        if job.cancelled:
            job.status = JobStatus.CANCELLED
            await emit({"type": "cancelled"})
            return

        if proc.returncode != 0:
            raise RuntimeError(f"Frame extraction failed: {stderr_bytes.decode(errors='replace').strip()}")

        frame_files = sorted(frames_dir.glob("*.png"))
        total = len(frame_files)
        if total == 0:
            raise RuntimeError("ffmpeg produced no frames")
        log.info("[%s] %d frames extracted", job.id, total)

        # ── 3. Build mask ─────────────────────────────────────────────────
        mask_img = _build_mask(job.width, job.height, rects)

        # ── 4. Inpaint frame by frame ─────────────────────────────────────
        log.info("[%s] Starting inpainting…", job.id)
        loop = asyncio.get_event_loop()
        t0 = time.monotonic()

        for i, frame_path in enumerate(frame_files):
            if job.cancelled:
                job.status = JobStatus.CANCELLED
                await emit({"type": "cancelled"})
                return

            result = await loop.run_in_executor(None, inpaint_frame, frame_path, mask_img)
            result.save(frame_path)

            done = i + 1
            elapsed = time.monotonic() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            await emit({
                "type": "progress",
                "frame": done,
                "total": total,
                "eta_seconds": round(eta, 1),
            })

        if job.cancelled:
            job.status = JobStatus.CANCELLED
            await emit({"type": "cancelled"})
            return

        # ── 5. Re-encode ──────────────────────────────────────────────────
        log.info("[%s] Re-encoding…", job.id)
        rc, _, stderr = await _ffmpeg(
            "-framerate", job.fps_str,
            "-i", str(frames_dir / "%06d.png"),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "18",
            "-preset", "medium",
            str(tmp_video),
        )
        if rc != 0:
            raise RuntimeError(f"Re-encode failed: {stderr.strip()}")

        # ── 6. Mux audio ──────────────────────────────────────────────────
        if has_audio:
            rc, _, stderr = await _ffmpeg(
                "-i", str(tmp_video),
                "-i", str(audio_path),
                "-c", "copy",
                "-shortest",
                str(output),
            )
            if rc == 0:
                tmp_video.unlink(missing_ok=True)
            else:
                log.warning("[%s] Audio mux failed, keeping video-only: %s", job.id, stderr.strip())
                shutil.move(str(tmp_video), str(output))
        else:
            shutil.move(str(tmp_video), str(output))

        # ── 7. Clean up frames ────────────────────────────────────────────
        shutil.rmtree(frames_dir, ignore_errors=True)
        audio_path.unlink(missing_ok=True)
        tmp_video.unlink(missing_ok=True)

        job.status = JobStatus.DONE
        await emit({"type": "done"})
        log.info("[%s] Done", job.id)

    except Exception as exc:
        log.exception("[%s] Processing failed", job.id)
        job.status = JobStatus.ERROR
        job.error = str(exc)
        await emit({"type": "error", "message": str(exc)})
