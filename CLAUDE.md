# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# First-time install (simple-lama-inpainting pins Pillow <10, so install it
# without deps first — otherwise it downgrades Pillow and breaks Torch).
pip install simple-lama-inpainting --no-deps
pip install -e .

# Run as desktop app (pywebview window) — default when invoked with no args
markdry            # equivalent to `markdry gui`
markdry gui

# Run as plain web server (FastAPI + browser at http://localhost:7777)
markdry serve

# Launch the macOS .app (thin shell launcher → `markdry gui`)
open markdry.app
```

There is no test suite, linter, or build step. The `.app` bundle is a
hand-maintained shell-script launcher, not a PyInstaller package — its
`Contents/MacOS/markdry` finds a system Python, sets `PYTHONPATH=<repo>`,
and `exec`s `python3 -m markdry gui`. It does NOT need to be rebuilt when
Python source changes; only rebuild the icns / Info.plist if those change.

External requirements: `ffmpeg` and `ffprobe` on `$PATH`. On macOS the
.app launcher injects `/opt/homebrew/bin:/usr/local/bin` into `PATH`
because Finder-launched .apps otherwise get a minimal `PATH` and can't
find Homebrew binaries.

## Architecture

Two execution modes share the same FastAPI app:

- **`gui`** (`desktop.py`) — runs `uvicorn` on `127.0.0.1:7777` in a
  daemon thread, polls `GET /` until ready, then opens a pywebview
  window pointed at the local server. A small `_API` class is exposed
  to JS as `window.pywebview.api.*` to bridge things WKWebView can't do
  (notably: native Save dialog, because WKWebView ignores the HTML
  `download` attribute).
- **`serve`** (`__main__.py`) — same uvicorn server, opens the system
  browser instead.

The frontend is a single `markdry/static/index.html` — vanilla JS, no
build step. It uses Server-Sent Events on `/api/progress/{job_id}` for
frame-by-frame progress, and a square canvas that letterboxes the
video preview (geometry tracked as `imgX`, `imgY`, `imgScale` —
mask rectangles are drawn in canvas coordinates and translated back
to source-video coordinates before being POSTed).

### Processing pipeline (`pipeline.py`)

For each job:

1. `ffmpeg` extracts audio to `audio.m4a` (try `-acodec copy`, fall back to AAC).
2. `ffmpeg` extracts every frame to `frames/%06d.png`.
3. Mask is built from user-drawn rects as a single `uint8` PIL image.
4. Each frame is inpainted by `simple_lama_inpainting.SimpleLama` (the same
   static mask is reused for every frame — there is no temporal model).
5. Frames are re-encoded to `output_video.mp4`, then muxed with audio
   to `output.mp4`.

The `Job.current_process` field holds the active `ffmpeg` subprocess so
`DELETE /api/job/{id}` can kill it for cancellation.

### Device selection (`inpaint.py`)

`_pick_device()` order: **CoreML+MPS** (if `onnxruntime` exposes
`CoreMLExecutionProvider` *and* `torch.backends.mps.is_available()`) →
**CUDA** → **CPU**. The model is loaded lazily under a lock and reused.
The FastAPI lifespan pre-loads it in a background thread on startup so
the first job doesn't pay the load cost.

### Jobs (`jobs.py`)

Jobs are in-memory only (`_jobs: dict[str, Job]`) with files under
`~/.markdry/jobs/<uuid>/`. A `cleanup_sweeper` background task drops
jobs older than `JOB_TTL` (1 hour). 500 MB upload cap is enforced
during streaming, not at the end.

## Things that have bitten us

- `webview.SAVE_DIALOG` must be imported *inside* `_API.download()` —
  `import webview` lives in `desktop.run()` so it's a local, not a
  module global. Errors raised from `js_api` methods are silently
  swallowed by pywebview, so a `NameError` looks like "nothing happens
  on click."
- The .app launcher uses `exec arch -arm64 …` because macOS may launch
  a universal Python binary as x86_64 under Rosetta when invoked from
  Finder, which then can't load arm64-only wheels (`pydantic_core`,
  etc.).
- `simple-lama-inpainting` pins `Pillow<10`. Install it with
  `--no-deps` and let the project's own `pillow>=9.5` win.
