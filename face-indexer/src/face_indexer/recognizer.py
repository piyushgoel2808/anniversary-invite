"""OpenCV YuNet face detection and SFace face embedding engine."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from numpy.typing import NDArray

from face_indexer.imaging import DEFAULT_MEDIUM_MAX_EDGE, resize_to_medium
from face_indexer.models import (
    COMBINED_MODEL_VERSION,
    DEFAULT_MODELS_DIR,
    SFACE_SPEC,
    YUNET_SPEC,
    verify_model_file,
)

ResolutionSource = Literal["medium", "original"]


@dataclass(frozen=True, slots=True)
class DetectedFace:
    """Single detected face with YuNet geometry and 128-D L2-normalized SFace embedding."""

    face_index: int
    bbox: tuple[float, float, float, float]  # x, y, w, h in image coordinates
    detection_score: float
    landmarks: tuple[tuple[float, float], ...]  # 5 facial keypoints
    embedding: NDArray[np.float32]  # shape (128,), unit L2 norm
    resolution_source: ResolutionSource


@dataclass(frozen=True, slots=True)
class PhotoAnalysisResult:
    """Output and timing telemetry from analyzing one image."""

    faces: list[DetectedFace]
    original_size: tuple[int, int]  # (width, height)
    analyzed_size: tuple[int, int]  # (width, height) of the chosen pass
    resolution_source: ResolutionSource
    used_original_retry: bool
    retry_reason: str | None
    medium_face_count: int
    detect_ms: float
    embed_ms: float
    total_ms: float
    model_version: str = COMBINED_MODEL_VERSION


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Tunable parameters for YuNet detection, SFace embedding, and original-res retry."""

    score_threshold: float = 0.65
    nms_threshold: float = 0.30
    top_k: int = 5000
    medium_max_edge: int = DEFAULT_MEDIUM_MAX_EDGE
    enable_original_retry: bool = True
    retry_if_zero_faces: bool = True
    retry_if_min_face_px_below: float = 48.0
    retry_if_min_score_below: float = 0.80
    min_face_px: float = 20.0
    max_original_edge: int = 4096


def normalize_embedding(raw_feature: NDArray[np.float32]) -> NDArray[np.float32]:
    """Flatten SFace output to 1-D float32 and L2-normalize to unit length."""
    vec = np.asarray(raw_feature, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-12:
        raise ValueError("SFace produced a zero-norm embedding vector.")
    normalized: NDArray[np.float32] = (vec / norm).astype(np.float32)
    return normalized


class FaceRecognitionEngine:
    """Local Apple Silicon face detection (YuNet) & recognition (SFace) engine.

    Strictly verifies pinned ONNX model SHA-256 checksums on initialization.
    """

    def __init__(
        self,
        models_dir: Path = DEFAULT_MODELS_DIR,
        config: EngineConfig | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.models_dir = models_dir

        yunet_path = verify_model_file(YUNET_SPEC, models_dir)
        sface_path = verify_model_file(SFACE_SPEC, models_dir)

        self._detector: Any = cv2.FaceDetectorYN.create(
            model=str(yunet_path),
            config="",
            input_size=(320, 320),
            score_threshold=self.config.score_threshold,
            nms_threshold=self.config.nms_threshold,
            top_k=self.config.top_k,
        )
        self._recognizer: Any = cv2.FaceRecognizerSF.create(
            model=str(sface_path),
            config="",
        )

    def _detect_raw(self, image_bgr: NDArray[np.uint8]) -> tuple[NDArray[np.float32], float]:
        """Run YuNet on a single BGR image and return valid face rows + elapsed ms."""
        height, width = image_bgr.shape[:2]
        start = time.perf_counter()
        self._detector.setInputSize((width, height))
        _, raw_faces = self._detector.detect(image_bgr)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        if raw_faces is None or len(raw_faces) == 0:
            return np.empty((0, 15), dtype=np.float32), elapsed_ms

        faces_arr = np.asarray(raw_faces, dtype=np.float32)
        min_px = self.config.min_face_px
        valid_mask = (faces_arr[:, 2] >= min_px) & (faces_arr[:, 3] >= min_px)
        return faces_arr[valid_mask], elapsed_ms

    def _extract_embeddings(
        self,
        image_bgr: NDArray[np.uint8],
        face_rows: NDArray[np.float32],
        source: ResolutionSource,
    ) -> tuple[list[DetectedFace], float]:
        """Align, crop, and compute 128-D SFace embeddings for each detected face."""
        start = time.perf_counter()
        detected: list[DetectedFace] = []
        for idx, row in enumerate(face_rows):
            aligned = self._recognizer.alignCrop(image_bgr, row)
            raw_feat = self._recognizer.feature(aligned)
            embedding = normalize_embedding(raw_feat)
            bbox = (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
            landmarks = (
                (float(row[4]), float(row[5])),
                (float(row[6]), float(row[7])),
                (float(row[8]), float(row[9])),
                (float(row[10]), float(row[11])),
                (float(row[12]), float(row[13])),
            )
            detected.append(
                DetectedFace(
                    face_index=idx,
                    bbox=bbox,
                    detection_score=float(row[14]),
                    landmarks=landmarks,
                    embedding=embedding,
                    resolution_source=source,
                )
            )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return detected, elapsed_ms

    def _should_retry_original(
        self,
        medium_faces: NDArray[np.float32],
        medium_scale: float,
    ) -> str | None:
        """Decide whether to retry detection/embedding on the higher-resolution original."""
        if not self.config.enable_original_retry or medium_scale >= 0.999:
            return None

        if len(medium_faces) == 0:
            return "zero_faces_on_medium" if self.config.retry_if_zero_faces else None

        min_dim = float(np.min(np.minimum(medium_faces[:, 2], medium_faces[:, 3])))
        if min_dim < self.config.retry_if_min_face_px_below:
            return f"small_face_on_medium({min_dim:.1f}px)"

        min_score = float(np.min(medium_faces[:, 14]))
        if min_score < self.config.retry_if_min_score_below:
            return f"low_confidence_on_medium({min_score:.2f})"

        return None

    def analyze_image(
        self,
        image_bgr: NDArray[np.uint8],
        *,
        force_original: bool = False,
    ) -> PhotoAnalysisResult:
        """Analyze an image using medium-resolution-first with automatic original retry.

        Args:
            image_bgr: Full-resolution BGR image.
            force_original: If True, skip medium pass and run directly on original (capped at
                ``max_original_edge`` to prevent excessive memory use on huge panoramas).

        Returns:
            Detected faces with SFace embeddings and performance telemetry.
        """
        t0 = time.perf_counter()
        orig_h, orig_w = image_bgr.shape[:2]

        if force_original:
            orig_capped, _ = resize_to_medium(image_bgr, max_edge=self.config.max_original_edge)
            faces_raw, detect_ms = self._detect_raw(orig_capped)
            faces, embed_ms = self._extract_embeddings(orig_capped, faces_raw, "original")
            cap_h, cap_w = orig_capped.shape[:2]
            return PhotoAnalysisResult(
                faces=faces,
                original_size=(orig_w, orig_h),
                analyzed_size=(cap_w, cap_h),
                resolution_source="original",
                used_original_retry=False,
                retry_reason="forced_original",
                medium_face_count=0,
                detect_ms=detect_ms,
                embed_ms=embed_ms,
                total_ms=(time.perf_counter() - t0) * 1000.0,
            )

        medium_img, medium_scale = resize_to_medium(
            image_bgr, max_edge=self.config.medium_max_edge
        )
        medium_raw, detect_ms = self._detect_raw(medium_img)
        medium_count = len(medium_raw)
        retry_reason = self._should_retry_original(medium_raw, medium_scale)

        chosen_img = medium_img
        chosen_raw = medium_raw
        chosen_source: ResolutionSource = "medium"
        used_retry = False

        if retry_reason is not None:
            orig_capped, _ = resize_to_medium(image_bgr, max_edge=self.config.max_original_edge)
            orig_raw, retry_detect_ms = self._detect_raw(orig_capped)
            detect_ms += retry_detect_ms
            used_retry = True

            # Prefer original pass whenever it finds at least as many faces (higher crop quality)
            if len(orig_raw) >= len(medium_raw) and len(orig_raw) > 0:
                chosen_img = orig_capped
                chosen_raw = orig_raw
                chosen_source = "original"

        faces, embed_ms = self._extract_embeddings(chosen_img, chosen_raw, chosen_source)
        chosen_h, chosen_w = chosen_img.shape[:2]
        return PhotoAnalysisResult(
            faces=faces,
            original_size=(orig_w, orig_h),
            analyzed_size=(chosen_w, chosen_h),
            resolution_source=chosen_source,
            used_original_retry=used_retry,
            retry_reason=retry_reason,
            medium_face_count=medium_count,
            detect_ms=detect_ms,
            embed_ms=embed_ms,
            total_ms=(time.perf_counter() - t0) * 1000.0,
        )

    def match_embeddings_cv2(
        self,
        embedding_a: NDArray[np.float32],
        embedding_b: NDArray[np.float32],
    ) -> tuple[float, float]:
        """Compute official OpenCV SFace cosine similarity and normalized L2 distance."""
        a = np.asarray(embedding_a, dtype=np.float32).reshape(1, -1)
        b = np.asarray(embedding_b, dtype=np.float32).reshape(1, -1)
        cosine = float(self._recognizer.match(a, b, cv2.FaceRecognizerSF_FR_COSINE))
        l2 = float(self._recognizer.match(a, b, cv2.FaceRecognizerSF_FR_NORM_L2))
        return cosine, l2
