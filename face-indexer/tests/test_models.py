"""Tests for pinned model manifest and SHA-256 integrity verification."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from face_indexer.models import (
    COMBINED_MODEL_VERSION,
    MODEL_MANIFEST,
    SFACE_SPEC,
    YUNET_SPEC,
    ModelIntegrityError,
    ModelSpec,
    compute_sha256,
    download_model,
    verify_model_file,
)


def test_pinned_manifest_licenses_and_versions() -> None:
    assert COMBINED_MODEL_VERSION == "yunet_2023mar+sface_2021dec"
    assert YUNET_SPEC.license_spdx == "MIT"
    assert SFACE_SPEC.license_spdx == "Apache-2.0"
    assert set(MODEL_MANIFEST.keys()) == {"yunet", "sface"}
    assert len(YUNET_SPEC.expected_sha256) == 64
    assert len(SFACE_SPEC.expected_sha256) == 64


def test_verify_model_file_valid_missing_size_and_hash(tmp_path: Path) -> None:
    payload = b"silver-jubilee-onnx-test-payload"
    digest = hashlib.sha256(payload).hexdigest()
    spec = ModelSpec(
        key="dummy",
        filename="dummy.onnx",
        version="dummy_v1",
        license_spdx="MIT",
        upstream_commit="abc123",
        download_url="https://example.invalid/dummy.onnx",
        license_url="https://example.invalid/LICENSE",
        expected_size_bytes=len(payload),
        expected_sha256=digest,
    )

    # Missing file
    with pytest.raises(ModelIntegrityError, match="Missing model file"):
        verify_model_file(spec, tmp_path)

    # Valid file
    model_file = tmp_path / spec.filename
    model_file.write_bytes(payload)
    assert compute_sha256(model_file) == digest
    assert verify_model_file(spec, tmp_path) == model_file.resolve()

    # Wrong size
    model_file.write_bytes(payload[:-1])
    with pytest.raises(ModelIntegrityError, match="size mismatch"):
        verify_model_file(spec, tmp_path)

    # Same size, tampered byte -> SHA-256 mismatch
    tampered = b"X" + payload[1:]
    model_file.write_bytes(tampered)
    with pytest.raises(ModelIntegrityError, match="SHA-256 mismatch"):
        verify_model_file(spec, tmp_path)


def test_download_model_atomic_verification_and_rejection(tmp_path: Path) -> None:
    payload = b"valid-onnx-bytes-0123456789"
    spec = ModelSpec(
        key="dummy",
        filename="dummy.onnx",
        version="dummy_v1",
        license_spdx="Apache-2.0",
        upstream_commit="abc123",
        download_url="https://example.invalid/dummy.onnx",
        license_url="https://example.invalid/LICENSE",
        expected_size_bytes=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    class _FakeResponse(io.BytesIO):
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    # Corrupted payload must fail and leave no target/temp files behind
    with (
        patch(
            "face_indexer.models.urllib.request.urlopen",
            return_value=_FakeResponse(b"X" + payload[1:]),
        ),
        pytest.raises(ModelIntegrityError, match="SHA-256 mismatch"),
    ):
        download_model(spec, tmp_path)
    assert list(tmp_path.iterdir()) == []

    # Valid payload succeeds and verifies
    with patch(
        "face_indexer.models.urllib.request.urlopen",
        return_value=_FakeResponse(payload),
    ):
        saved = download_model(spec, tmp_path)
    assert saved.is_file()
    assert saved.read_bytes() == payload
