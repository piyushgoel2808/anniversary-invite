"""Tests for benchmark metrics, threshold sweep, exports, and offline HTML reporting."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray
from PIL import Image

from face_indexer.benchmark import (
    compute_threshold_sweep,
    evaluate_pairs,
    export_benchmark_artifacts,
    load_ground_truth_csv,
    run_benchmark,
)
from face_indexer.recognizer import DetectedFace, EngineConfig, PhotoAnalysisResult
from face_indexer.report import render_html_report, write_html_report


def _save_dummy_jpg(path: Path, size: tuple[int, int] = (400, 300)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=(120, 140, 160)).save(path, format="JPEG")


def _basis(idx: int) -> NDArray[np.float32]:
    v = np.zeros(128, dtype=np.float32)
    v[idx] = 1.0
    return v


class _FakeEngine:
    """Deterministic engine stub for testing full benchmark + report orchestration."""

    def __init__(self, mapping: dict[tuple[int, int], list[DetectedFace]]) -> None:
        self.config = EngineConfig()
        self._mapping = mapping

    def analyze_image(
        self,
        image_bgr: NDArray[np.uint8],
        *,
        force_original: bool = False,
    ) -> PhotoAnalysisResult:
        h, w = image_bgr.shape[:2]
        faces = self._mapping.get((w, h), [])
        used_retry = w > 1400
        return PhotoAnalysisResult(
            faces=faces,
            original_size=(w, h),
            analyzed_size=(w, h),
            resolution_source="original" if used_retry else "medium",
            used_original_retry=used_retry,
            retry_reason="small_face_on_medium(32.0px)" if used_retry else None,
            medium_face_count=len(faces),
            detect_ms=12.5,
            embed_ms=4.5,
            total_ms=17.0,
        )


def test_ground_truth_loading_and_sweep(tmp_path: Path) -> None:
    gt_file = tmp_path / "gt.csv"
    gt_file.write_text(
        "photo_filename,guest_id\np1.jpg,navneet\np1.jpg,ruby\np2.jpg,navneet\n",
        encoding="utf-8",
    )
    gt = load_ground_truth_csv(gt_file)
    assert gt == {("p1.jpg", "navneet"), ("p1.jpg", "ruby"), ("p2.jpg", "navneet")}

    metrics = evaluate_pairs({("p1.jpg", "navneet"), ("p2.jpg", "ruby")}, gt, 0.363)
    assert metrics.true_positives == 1
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 2
    assert pytest.approx(metrics.precision, abs=1e-4) == 0.5
    assert pytest.approx(metrics.recall, abs=1e-4) == 1 / 3

    scores = {
        ("p1.jpg", "navneet"): 0.62,
        ("p1.jpg", "ruby"): 0.44,
        ("p2.jpg", "navneet"): 0.39,
        ("p2.jpg", "ruby"): 0.31,
    }
    sweep = compute_threshold_sweep(scores, gt, thresholds=[0.30, 0.38, 0.50])
    # At 0.38, all 3 GT matches pass and the 0.31 false positive is excluded -> F1 = 1.0
    thr_38 = next(m for m in sweep if m.threshold == 0.38)
    assert thr_38.f1 == 1.0


def test_end_to_end_benchmark_export_and_html_report(tmp_path: Path) -> None:
    refs_dir = tmp_path / "references"
    album_dir = tmp_path / "album"
    out_dir = tmp_path / "report"

    # Encode identity via distinct image dimensions so _FakeEngine maps deterministically
    _save_dummy_jpg(refs_dir / "navneet" / "n1.jpg", size=(301, 300))
    _save_dummy_jpg(refs_dir / "navneet" / "n2.jpg", size=(301, 300))
    _save_dummy_jpg(refs_dir / "ruby" / "r1.jpg", size=(302, 300))

    # Album photos: one with both, one large group photo triggering original retry,
    # and one with HTML-special characters to verify XSS escaping in report.html
    xss_name = "p2_<img src=x onerror=alert(1)>.jpg"
    _save_dummy_jpg(album_dir / "p1_couple.jpg", size=(501, 400))
    _save_dummy_jpg(album_dir / xss_name, size=(1800, 1200))

    f_nav = DetectedFace(0, (20, 20, 80, 80), 0.96, ((0, 0),) * 5, _basis(0), "medium")
    f_ruby = DetectedFace(1, (140, 20, 80, 80), 0.94, ((0, 0),) * 5, _basis(1), "medium")
    f_nav_orig = DetectedFace(0, (40, 40, 120, 120), 0.92, ((0, 0),) * 5, _basis(0), "original")

    fake_engine: Any = _FakeEngine(
        {
            (301, 300): [f_nav],
            (302, 300): [f_ruby],
            (501, 400): [f_nav, f_ruby],
            (1800, 1200): [f_nav_orig],
        }
    )

    gt_csv = tmp_path / "gt.csv"
    gt_csv.write_text(
        "photo_filename,guest_id\n"
        "p1_couple.jpg,navneet\n"
        "p1_couple.jpg,ruby\n"
        f"{xss_name},navneet\n",
        encoding="utf-8",
    )

    summary = run_benchmark(
        references_dir=refs_dir,
        album_dir=album_dir,
        engine=fake_engine,
        cosine_threshold=0.363,
        ground_truth_csv=gt_csv,
    )

    assert summary.guest_count == 2
    assert summary.photo_count == 2
    assert summary.total_suggestions == 3
    assert summary.original_retry_count == 1
    assert summary.ground_truth_metrics is not None
    assert summary.ground_truth_metrics.f1 == 1.0

    json_path, csv_path = export_benchmark_artifacts(summary, out_dir)
    raw_json = json.loads(json_path.read_text(encoding="utf-8"))
    # Privacy check: zero raw embeddings or crop base64 blobs exported in JSON/CSV
    assert "embedding" not in json_path.read_text(encoding="utf-8")
    assert "suggestion_crops" not in json_path.read_text(encoding="utf-8")
    assert raw_json["total_suggestions"] == 3

    with csv_path.open("r", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert all(r["status"] == "PENDING" for r in rows)

    html_path = write_html_report(summary, out_dir / "report.html", load_ground_truth_csv(gt_csv))
    html_text = html_path.read_text(encoding="utf-8")
    assert "<img src=x onerror=alert(1)>" not in html_text
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_text
    assert "https://" not in html_text  # Strictly 100% offline report
    assert "Threshold Calibration Sweep" in render_html_report(summary)


def test_cli_benchmark_and_check_photo_with_stub_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from face_indexer import cli

    refs_dir = tmp_path / "references"
    album_dir = tmp_path / "album"
    out_dir = tmp_path / "out"

    _save_dummy_jpg(refs_dir / "navneet" / "n1.jpg", size=(301, 300))
    _save_dummy_jpg(album_dir / "p1.jpg", size=(501, 400))

    f_nav = DetectedFace(0, (20, 20, 80, 80), 0.96, ((0, 0),) * 5, _basis(0), "medium")
    stub = _FakeEngine({(301, 300): [f_nav], (501, 400): [f_nav]})
    monkeypatch.setattr(cli, "FaceRecognitionEngine", lambda **kwargs: stub)

    rc_bm = cli.main(
        [
            "benchmark",
            "--references",
            str(refs_dir),
            "--album",
            str(album_dir),
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc_bm == 0
    assert (out_dir / "results.json").is_file()
    assert (out_dir / "suggestions.csv").is_file()
    assert (out_dir / "report.html").is_file()

    rc_cp = cli.main(
        [
            "check-photo",
            "--references",
            str(refs_dir),
            "--photo",
            str(album_dir / "p1.jpg"),
        ]
    )
    assert rc_cp == 0
    out_text = capsys.readouterr().out
    assert '"guest_id": "navneet"' in out_text
    assert '"status": "PENDING"' in out_text

    # verify-models against empty directory returns non-zero exit code
    assert cli.main(["--models-dir", str(tmp_path / "empty_models"), "verify-models"]) == 1
