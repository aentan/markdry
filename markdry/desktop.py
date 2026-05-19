import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT = 7777
LOG_FILE = Path.home() / ".markdry" / "markdry.log"
log = logging.getLogger(__name__)


def _configure_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, mode="w"),
            logging.StreamHandler(),
        ],
    )


def _start_server() -> None:
    import uvicorn
    uvicorn.run(
        "markdry.server:app",
        host="127.0.0.1",
        port=PORT,
        reload=False,
        log_level="info",
        access_log=True,
    )


def _wait_for_server(timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/", timeout=1
            ) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.URLError:
            time.sleep(0.15)
        except Exception:
            time.sleep(0.15)
    return False


class _API:
    """Python functions exposed to the webview as window.pywebview.api.*"""

    def __init__(self) -> None:
        self.window: "webview.Window | None" = None

    def download(self, job_id: str) -> dict:
        import shutil
        import webview
        from markdry.jobs import JobStatus, get_job

        job = get_job(job_id)
        if not job or job.status != JobStatus.DONE:
            return {"error": "Job not ready"}

        output = job.dir / "output.mp4"
        if not output.exists():
            return {"error": "Output file missing"}

        filename = f"{job.original_name}-clean.mp4"
        result = self.window.create_file_dialog(
            webview.SAVE_DIALOG,
            directory=str(Path.home() / "Movies"),
            save_filename=filename,
        )

        if not result:
            return {"cancelled": True}

        dest = result[0] if isinstance(result, (list, tuple)) else result
        shutil.copy2(output, dest)
        log.info("Saved clean video to %s", dest)
        return {"ok": True}


def run() -> None:
    _configure_logging()

    try:
        import webview
    except ImportError:
        raise SystemExit(
            "pywebview is required for the desktop window.\n"
            "Install it with:  pip install pywebview"
        )

    log.info("Starting markdry server on port %d", PORT)
    threading.Thread(target=_start_server, daemon=True).start()

    if not _wait_for_server():
        log.error("Server did not become ready in time. See %s for details.", LOG_FILE)
        raise SystemExit(f"Server did not start. Check {LOG_FILE} for details.")

    api = _API()
    window = webview.create_window(
        "markdry",
        f"http://127.0.0.1:{PORT}",
        width=400,
        height=480,
        min_size=(400, 480),
        js_api=api,
    )
    api.window = window
    webview.start()
