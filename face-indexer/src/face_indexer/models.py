"""Pinned ONNX model manifest, download, and SHA-256 integrity verification."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

OPENCV_ZOO_COMMIT = "48b10fa505aeb51fbd127fc60a5c660040e823b7"
COMBINED_MODEL_VERSION = "yunet_2023mar+sface_2021dec"

DEFAULT_MODELS_DIR = Path(__file__).resolve().parents[2] / "models"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Immutable provenance and integrity specification for a pinned ONNX model."""

    key: str
    filename: str
    version: str
    license_spdx: str
    upstream_commit: str
    download_url: str
    license_url: str
    expected_size_bytes: int
    expected_sha256: str


YUNET_SPEC = ModelSpec(
    key="yunet",
    filename="face_detection_yunet_2023mar.onnx",
    version="yunet_2023mar",
    license_spdx="MIT",
    upstream_commit=OPENCV_ZOO_COMMIT,
    download_url=(
        f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{OPENCV_ZOO_COMMIT}"
        "/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
    ),
    license_url=(
        f"https://raw.githubusercontent.com/opencv/opencv_zoo/{OPENCV_ZOO_COMMIT}"
        "/models/face_detection_yunet/LICENSE"
    ),
    expected_size_bytes=232_589,
    expected_sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
)

SFACE_SPEC = ModelSpec(
    key="sface",
    filename="face_recognition_sface_2021dec.onnx",
    version="sface_2021dec",
    license_spdx="Apache-2.0",
    upstream_commit=OPENCV_ZOO_COMMIT,
    download_url=(
        f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{OPENCV_ZOO_COMMIT}"
        "/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
    ),
    license_url=(
        f"https://raw.githubusercontent.com/opencv/opencv_zoo/{OPENCV_ZOO_COMMIT}"
        "/models/face_recognition_sface/LICENSE"
    ),
    expected_size_bytes=38_696_353,
    expected_sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
)

MODEL_MANIFEST: dict[str, ModelSpec] = {
    YUNET_SPEC.key: YUNET_SPEC,
    SFACE_SPEC.key: SFACE_SPEC,
}


class ModelIntegrityError(RuntimeError):
    """Raised when a model file is missing, truncated, or fails SHA-256 verification."""


def compute_sha256(path: Path) -> str:
    """Compute the lowercase hex SHA-256 digest of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_model_file(spec: ModelSpec, models_dir: Path = DEFAULT_MODELS_DIR) -> Path:
    """Verify that a pinned model exists and matches its exact size and SHA-256 digest.

    Args:
        spec: Pinned model specification.
        models_dir: Directory containing downloaded ONNX files.

    Returns:
        Verified absolute path to the model file.

    Raises:
        ModelIntegrityError: If missing, wrong size, or SHA-256 mismatch.
    """
    model_path = (models_dir / spec.filename).resolve()
    if not model_path.is_file():
        raise ModelIntegrityError(
            f"Missing model file '{model_path}'. Run `uv run face-indexer download-models` first."
        )

    actual_size = model_path.stat().st_size
    if actual_size != spec.expected_size_bytes:
        raise ModelIntegrityError(
            f"Model '{spec.filename}' size mismatch: expected {spec.expected_size_bytes} bytes, "
            f"got {actual_size} bytes."
        )

    actual_sha = compute_sha256(model_path)
    if actual_sha != spec.expected_sha256:
        raise ModelIntegrityError(
            f"Model '{spec.filename}' SHA-256 mismatch: expected {spec.expected_sha256}, "
            f"got {actual_sha}."
        )

    return model_path


def download_model(
    spec: ModelSpec,
    models_dir: Path = DEFAULT_MODELS_DIR,
    *,
    force: bool = False,
    timeout_seconds: float = 120.0,
) -> Path:
    """Download a pinned ONNX model atomically and verify its SHA-256 before saving.

    Args:
        spec: Pinned model specification.
        models_dir: Target directory for models.
        force: If True, re-download even when a verified copy already exists.
        timeout_seconds: Network read timeout per download request.

    Returns:
        Verified path to the local model file.

    Raises:
        ModelIntegrityError: If downloaded payload fails size or SHA-256 validation.
    """
    models_dir.mkdir(parents=True, exist_ok=True)
    target_path = (models_dir / spec.filename).resolve()

    if not force and target_path.is_file():
        try:
            return verify_model_file(spec, models_dir)
        except ModelIntegrityError:
            pass

    request = urllib.request.Request(
        spec.download_url,
        headers={"User-Agent": "silver-jubilee-face-indexer/0.1.0"},
    )
    with tempfile.NamedTemporaryFile(dir=models_dir, prefix=f".{spec.key}_", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with (
            urllib.request.urlopen(request, timeout=timeout_seconds) as response,
            tmp_path.open("wb") as out_file,
        ):
            shutil.copyfileobj(response, out_file)

        actual_size = tmp_path.stat().st_size
        if actual_size != spec.expected_size_bytes:
            raise ModelIntegrityError(
                f"Downloaded '{spec.filename}' size mismatch: expected "
                f"{spec.expected_size_bytes} bytes, got {actual_size} bytes."
            )

        actual_sha = compute_sha256(tmp_path)
        if actual_sha != spec.expected_sha256:
            raise ModelIntegrityError(
                f"Downloaded '{spec.filename}' SHA-256 mismatch: expected "
                f"{spec.expected_sha256}, got {actual_sha}."
            )

        tmp_path.replace(target_path)
        return target_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def verify_all_models(models_dir: Path = DEFAULT_MODELS_DIR) -> dict[str, Path]:
    """Verify all pinned models exist and pass SHA-256 checks."""
    return {key: verify_model_file(spec, models_dir) for key, spec in MODEL_MANIFEST.items()}


def download_all_models(
    models_dir: Path = DEFAULT_MODELS_DIR,
    *,
    force: bool = False,
) -> dict[str, Path]:
    """Download and verify all pinned models."""
    return {
        key: download_model(spec, models_dir, force=force) for key, spec in MODEL_MANIFEST.items()
    }
