"""Tests for EXIF-safe image loading, resizing, and face crop encoding."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from face_indexer.imaging import (
    crop_face_thumbnail_data_url,
    is_supported_image,
    list_images_in_dir,
    load_image_bgr,
    resize_to_medium,
)


def test_load_image_applies_exif_orientation(tmp_path: Path) -> None:
    # Create a 40x20 (W=40, H=20) image tagged with EXIF Orientation=6 (Rotate 90 CW)
    # After EXIF transpose, it should become H=40, W=20.
    img = Image.new("RGB", (40, 20), color=(10, 20, 30))
    exif = img.getexif()
    exif[274] = 6  # Orientation tag = Rotate 90 CW
    out_path = tmp_path / "rotated.jpg"
    img.save(out_path, format="JPEG", exif=exif)

    bgr = load_image_bgr(out_path)
    assert bgr.shape == (40, 20, 3)
    assert bgr.dtype == np.uint8


def test_load_image_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_image_bgr(tmp_path / "missing.jpg")

    bad_file = tmp_path / "corrupt.jpg"
    bad_file.write_bytes(b"not-a-real-image")
    with pytest.raises(ValueError, match="Failed to decode"):
        load_image_bgr(bad_file)


def test_resize_to_medium_preserves_aspect_and_never_upscales() -> None:
    large = np.zeros((2000, 3000, 3), dtype=np.uint8)
    resized, scale = resize_to_medium(large, max_edge=1400)
    assert resized.shape[1] == 1400
    assert resized.shape[0] == 933
    assert pytest.approx(scale, rel=1e-3) == 1400 / 3000

    small = np.zeros((600, 800, 3), dtype=np.uint8)
    unchanged, same_scale = resize_to_medium(small, max_edge=1400)
    assert unchanged.shape == (600, 800, 3)
    assert same_scale == 1.0

    with pytest.raises(ValueError, match="positive"):
        resize_to_medium(small, max_edge=0)


def test_crop_face_thumbnail_data_url_handles_edge_bounds() -> None:
    img = np.full((200, 300, 3), 180, dtype=np.uint8)
    data_url = crop_face_thumbnail_data_url(img, (-10.0, -5.0, 60.0, 60.0), thumb_size=64)
    assert data_url.startswith("data:image/jpeg;base64,")
    assert len(data_url) > 100


def test_list_images_filters_extensions(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(b"1")
    (tmp_path / "b.WEBP").write_bytes(b"1")
    (tmp_path / "c.txt").write_text("ignore", encoding="utf-8")
    (tmp_path / "subdir").mkdir()

    found = list_images_in_dir(tmp_path)
    assert [p.name for p in found] == ["a.jpg", "b.WEBP"]
    assert is_supported_image(tmp_path / "a.jpg")
    assert not is_supported_image(tmp_path / "c.txt")
