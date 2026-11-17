"""Real Apple Silicon integration tests with pinned YuNet + SFace ONNX models."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from face_indexer.cli import main as cli_main
from face_indexer.matcher import cosine_similarity, normalized_l2_distance
from face_indexer.models import DEFAULT_MODELS_DIR, verify_all_models
from face_indexer.recognizer import EngineConfig, FaceRecognitionEngine


@pytest.fixture(scope="module")
def real_engine() -> FaceRecognitionEngine:
    """Load the verified pinned YuNet + SFace ONNX models."""
    verify_all_models(DEFAULT_MODELS_DIR)
    return FaceRecognitionEngine(models_dir=DEFAULT_MODELS_DIR)


def test_real_models_verify_cli() -> None:
    rc = cli_main(["--models-dir", str(DEFAULT_MODELS_DIR), "verify-models"])
    assert rc == 0


def test_real_sface_embedding_and_cv2_metric_equivalence(
    real_engine: FaceRecognitionEngine,
) -> None:
    # Run SFace directly on two deterministic 112x112 BGR synthetic crops to confirm
    # native Apple Silicon ONNX execution and exact equivalence with OpenCV match().
    rng = np.random.default_rng(42)
    crop_a = rng.integers(0, 255, size=(112, 112, 3), dtype=np.uint8)
    crop_b = rng.integers(0, 255, size=(112, 112, 3), dtype=np.uint8)

    feat_a = real_engine._recognizer.feature(crop_a)
    feat_b = real_engine._recognizer.feature(crop_b)

    assert feat_a.shape == (1, 128)
    assert feat_b.shape == (1, 128)

    cv2_cos, cv2_l2 = real_engine.match_embeddings_cv2(feat_a, feat_b)
    np_cos = cosine_similarity(feat_a, feat_b)
    np_l2 = normalized_l2_distance(feat_a, feat_b)

    assert pytest.approx(np_cos, abs=1e-5) == cv2_cos
    assert pytest.approx(np_l2, abs=1e-5) == cv2_l2

    # Self-match must be ~1.0 cosine and ~0.0 L2
    self_cos, self_l2 = real_engine.match_embeddings_cv2(feat_a, feat_a)
    assert pytest.approx(self_cos, abs=1e-5) == 1.0
    assert pytest.approx(self_l2, abs=1e-5) == 0.0


def test_real_yunet_medium_pass_and_original_retry_flow(
    real_engine: FaceRecognitionEngine,
) -> None:
    # Blank medium-sized image (1000x800): no faces, no retry since <= 1400px
    blank_small = np.full((800, 1000, 3), 240, dtype=np.uint8)
    res_small = real_engine.analyze_image(blank_small)
    assert res_small.faces == []
    assert res_small.resolution_source == "medium"
    assert not res_small.used_original_retry
    assert res_small.detect_ms > 0.0

    # Blank high-res image (2400x1600): 0 faces on medium triggers automatic original-res retry
    blank_large = np.full((1600, 2400, 3), 240, dtype=np.uint8)
    res_large = real_engine.analyze_image(blank_large)
    assert res_large.faces == []
    assert res_large.used_original_retry
    assert res_large.retry_reason == "zero_faces_on_medium"
    assert res_large.detect_ms > 0.0


def test_real_engine_retry_can_be_disabled(tmp_path: Path) -> None:
    engine = FaceRecognitionEngine(
        models_dir=DEFAULT_MODELS_DIR,
        config=EngineConfig(enable_original_retry=False),
    )
    blank_large = np.full((1600, 2400, 3), 240, dtype=np.uint8)
    res = engine.analyze_image(blank_large)
    assert not res.used_original_retry
    assert res.resolution_source == "medium"
