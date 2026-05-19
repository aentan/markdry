import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

JOBS_ROOT = Path.home() / ".markdry" / "jobs"
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB
JOB_TTL = 3600  # 1 hour


class JobStatus(str, Enum):
    READY = "ready"
    PROCESSING = "processing"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class Job:
    id: str
    dir: Path
    status: JobStatus = JobStatus.READY
    source_ext: str = ""
    original_name: str = "video"
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    fps_str: str = "30/1"
    frame_count: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    downloaded_at: Optional[float] = None
    cancelled: bool = False
    current_process: Optional[Any] = None
    progress_queue: Optional[asyncio.Queue] = None

    def __post_init__(self):
        self.progress_queue = asyncio.Queue()


_jobs: dict[str, "Job"] = {}


def create_job() -> Job:
    job_id = str(uuid.uuid4())
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job = Job(id=job_id, dir=job_dir)
    _jobs[job_id] = job
    return job


def get_job(job_id: str) -> Optional[Job]:
    return _jobs.get(job_id)


def remove_job(job_id: str) -> None:
    _jobs.pop(job_id, None)


async def cleanup_sweeper() -> None:
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [
            job for job in list(_jobs.values())
            if (now - job.created_at > JOB_TTL)
            or (job.downloaded_at and now - job.downloaded_at > 60)
        ]
        for job in expired:
            log.info("Cleaning up job %s", job.id)
            try:
                shutil.rmtree(job.dir, ignore_errors=True)
            except Exception:
                pass
            remove_job(job.id)
