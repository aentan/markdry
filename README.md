# markdry

Remove watermarks from videos via drag-and-drop. Uses LaMa inpainting.
SVGOMG-style UX: drop a file, draw mask rectangles, download the result.

## Install

```bash
# simple-lama-inpainting pins Pillow <10; install it separately first
pip install simple-lama-inpainting --no-deps
pip install -e .
markdry serve
```

The browser opens at http://localhost:7777 automatically.

On first run, the LaMa model (~196 MB) is downloaded once to `~/.cache/torch/`.

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe` on `$PATH` (`brew install ffmpeg`)
- Apple Silicon: CoreML/MPS is selected automatically, CPU otherwise

## Usage

1. Drop a video onto the page (MP4, MOV, MKV — up to 500 MB).
2. Drag rectangles over the watermark on the preview frame.
3. Click **Process**. Progress streams frame-by-frame.
4. Click **Download clean video** when done.

Audio is preserved. Processing uses the same static mask for every frame.
