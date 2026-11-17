"""EXIF-safe image loading, medium-resolution scaling, and face crop helpers."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - optional fallback if heif unavailable
    HEIF_SUPPORTED = False

SUPPORTED_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"}
)

DEFAULT_MEDIUM_MAX_EDGE = 1400


def is_supported_image(path: Path) -> bool:
    """Return True if path is a regular file with a supported image extension."""
    return path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def list_images_in_dir(directory: Path) -> list[Path]:
    """Return sorted supported image files directly inside a directory."""
    if not directory.is_dir():
        raise FileNotFoundError(f"Image directory not found: {directory}")
    return sorted(p for p in directory.iterdir() if is_supported_image(p))


def load_image_bgr(path: Path) -> NDArray[np.uint8]:
    """Load an image with EXIF orientation applied and return an OpenCV BGR uint8 array.

    Args:
        path: Path to the image file (JPEG, PNG, WebP, HEIC/HEIF, BMP).

    Returns:
        Contiguous HxWx3 uint8 array in BGR channel order.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file cannot be decoded into a valid image.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Image file not found: {path}")

    try:
        with Image.open(path) as img:
            oriented = ImageOps.exif_transpose(img)
            rgb = oriented.convert("RGB")
            rgb_array = np.asarray(rgb, dtype=np.uint8)
    except Exception as exc:
        raise ValueError(f"Failed to decode image '{path}': {exc}") from exc

    if rgb_array.ndim != 3 or rgb_array.shape[2] != 3:
        raise ValueError(f"Unexpected image shape {rgb_array.shape} for '{path}'")

    bgr: NDArray[np.uint8] = np.ascontiguousarray(cv2.cvtColor(rgb_array, cv2.COLOR_RGB2BGR))
    return bgr


def resize_to_medium(
    image_bgr: NDArray[np.uint8],
    max_edge: int = DEFAULT_MEDIUM_MAX_EDGE,
) -> tuple[NDArray[np.uint8], float]:
    """Downscale an image so its longest edge is at most ``max_edge`` pixels.

    Never upscales images already smaller than or equal to ``max_edge``.

    Returns:
        Tuple of ``(resized_image_bgr, scale_factor)`` where ``scale_factor <= 1.0``.
    """
    if max_edge <= 0:
        raise ValueError(f"max_edge must be positive, got {max_edge}")

    height, width = image_bgr.shape[:2]
    longest = max(height, width)
    if longest <= max_edge:
        return image_bgr, 1.0

    scale = max_edge / float(longest)
    new_w = max(1, round(width * scale))
    new_h = max(1, round(height * scale))
    resized_raw: Any = cv2.resize(image_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)
    resized: NDArray[np.uint8] = np.ascontiguousarray(resized_raw, dtype=np.uint8)
    return resized, scale


def crop_face_thumbnail_data_url(
    image_bgr: NDArray[np.uint8],
    bbox: tuple[float, float, float, float],
    *,
    margin_ratio: float = 0.25,
    thumb_size: int = 128,
    jpeg_quality: int = 82,
) -> str:
    """Crop a face bounding box with padding and encode as an inline JPEG data URL.

    Used strictly for offline local HTML visual benchmark reports.
    """
    img_h, img_w = image_bgr.shape[:2]
    x, y, w, h = bbox
    pad_x = w * margin_ratio
    pad_y = h * margin_ratio

    x1 = max(0, round(x - pad_x))
    y1 = max(0, round(y - pad_y))
    x2 = min(img_w, round(x + w + pad_x))
    y2 = min(img_h, round(y + h + pad_y))

    if x2 <= x1 or y2 <= y1:
        x1, y1, x2, y2 = 0, 0, img_w, img_h

    crop_bgr = image_bgr[y1:y2, x1:x2]
    crop_rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(crop_rgb)
    pil_img.thumbnail((thumb_size, thumb_size), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    pil_img.save(buffer, format="JPEG", quality=jpeg_quality, optimize=True)
    b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"
