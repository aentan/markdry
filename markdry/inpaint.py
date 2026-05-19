import logging
import threading
from pathlib import Path
from typing import Optional

import torch
from PIL import Image

log = logging.getLogger(__name__)

_lock = threading.Lock()
_model = None
_device_name: Optional[str] = None


def selected_device() -> Optional[str]:
    return _device_name


def _pick_device() -> tuple[str, torch.device]:
    """Return (display_name, torch.device).

    Detection order:
    1. CoreML via onnxruntime → use PyTorch MPS (Apple Silicon GPU)
    2. CUDA
    3. CPU
    """
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        log.debug("Available ONNX providers: %s", providers)
        if "CoreMLExecutionProvider" in providers and torch.backends.mps.is_available():
            return "CoreML (PyTorch MPS)", torch.device("mps")
    except Exception as exc:
        log.debug("onnxruntime provider detection skipped: %s", exc)

    if torch.cuda.is_available():
        return "CUDA", torch.device("cuda")

    return "CPU", torch.device("cpu")


def load_model():
    global _model, _device_name

    with _lock:
        if _model is not None:
            return _model

        name, device = _pick_device()
        _device_name = name
        log.info("Inpaint execution provider selected: %s", name)

        from simple_lama_inpainting import SimpleLama
        _model = SimpleLama(device=device)
        return _model


def inpaint_frame(frame_path: Path, mask_img: Image.Image) -> Image.Image:
    model = load_model()
    image = Image.open(frame_path).convert("RGB")
    result = model(image, mask_img)
    return result
