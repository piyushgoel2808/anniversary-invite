"""Phase 1 local benchmark runner, ground-truth evaluation, and threshold calibration."""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from face_indexer.imaging import (
    crop_face_thumbnail_data_url,
    list_images_in_dir,
    load_image_bgr,
    resize_to_medium,
)
from face_indexer.matcher import (
    DEFAULT_COSINE_THRESHOLD,
    GuestProfile,
    MatchSuggestion,
    ScoringStrategy,
    build_guest_profile,
    match_faces_to_guests,
    score_face_against_profile,
    select_reference_face,
)
from face_indexer.models import COMBINED_MODEL_VERSION
from face_indexer.recognizer import EngineConfig, FaceRecognitionEngine, PhotoAnalysisResult


@dataclass(frozen=True, slots=True)
class ThresholdMetrics:
    """Precision/Recall/F1 metrics evaluated at a single cosine threshold."""

    threshold: float
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True, slots=True)
class PhotoBenchmarkRecord:
    """Per-photo benchmark summary (contains zero raw face embeddings)."""

    photo_id: str
    filename: str
    original_width: int
    original_height: int
    analyzed_width: int
    analyzed_height: int
    resolution_source: str
    used_original_retry: bool
    retry_reason: str | None
    medium_face_count: int
    face_count: int
    detect_ms: float
    embed_ms: float
    total_ms: float
    suggestions: list[dict[str, Any]]
    # Inline face crops for offline HTML report only (excluded from JSON/CSV exports)
    suggestion_crops: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class GuestProfileSummary:
    """Serializable summary of a guest's reference profile (zero embeddings)."""

    guest_id: str
    display_name: str
    accepted_reference_count: int
    accepted_files: list[str]
    mean_intra_cosine: float
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class BenchmarkSummary:
    """Complete Phase 1 benchmark output."""

    model_version: str
    scoring_strategy: ScoringStrategy
    cosine_threshold: float
    guest_count: int
    photo_count: int
    total_faces_detected: int
    total_suggestions: int
    original_retry_count: int
    original_retry_rate: float
    mean_ms_per_photo: float
    p50_ms_per_photo: float
    p95_ms_per_photo: float
    throughput_photos_per_sec: float
    guest_profiles: list[GuestProfileSummary]
    photos: list[PhotoBenchmarkRecord]
    ground_truth_metrics: ThresholdMetrics | None
    threshold_sweep: list[ThresholdMetrics]
    recommended_threshold: float | None


def load_ground_truth_csv(path: Path) -> set[tuple[str, str]]:
    """Load ground-truth pairs of ``(photo_filename, guest_id)`` from CSV.

    Expected CSV header: ``photo_filename,guest_id`` (also accepts ``photo_id,guest_id``).
    """
    if not path.is_file():
        raise FileNotFoundError(f"Ground truth CSV not found: {path}")

    pairs: set[tuple[str, str]] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"Ground truth CSV '{path}' is missing a header row.")

        normalized_fields = {f.strip().lower(): f for f in reader.fieldnames}
        photo_col = normalized_fields.get("photo_filename") or normalized_fields.get("photo_id")
        guest_col = normalized_fields.get("guest_id")
        if not photo_col or not guest_col:
            raise ValueError(
                "Ground truth CSV must have headers 'photo_filename,guest_id' "
                "(or 'photo_id,guest_id')."
            )

        for row in reader:
            photo_val = str(row.get(photo_col) or "").strip()
            guest_val = str(row.get(guest_col) or "").strip()
            if photo_val and guest_val:
                pairs.add((photo_val, guest_val))
    return pairs


def evaluate_pairs(
    predicted: set[tuple[str, str]],
    ground_truth: set[tuple[str, str]],
    threshold: float,
) -> ThresholdMetrics:
    """Compute TP, FP, FN, precision, recall, and F1 for predicted vs ground truth."""
    tp = len(predicted & ground_truth)
    fp = len(predicted - ground_truth)
    fn = len(ground_truth - predicted)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2.0 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    return ThresholdMetrics(
        threshold=round(threshold, 3),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
    )


def compute_threshold_sweep(
    all_photo_guest_scores: dict[tuple[str, str], float],
    ground_truth: set[tuple[str, str]],
    thresholds: list[float] | None = None,
) -> list[ThresholdMetrics]:
    """Evaluate precision/recall/F1 across a range of cosine similarity thresholds."""
    sweep_points = thresholds or [round(0.24 + 0.02 * i, 3) for i in range(18)]
    results: list[ThresholdMetrics] = []
    for thr in sweep_points:
        predicted = {pair for pair, score in all_photo_guest_scores.items() if score >= thr}
        results.append(evaluate_pairs(predicted, ground_truth, thr))
    return results


def build_profiles_from_directory(
    references_dir: Path,
    engine: FaceRecognitionEngine,
) -> tuple[list[GuestProfile], list[GuestProfileSummary]]:
    """Build multi-reference guest profiles from ``references_dir/<guest_id>/*``."""
    if not references_dir.is_dir():
        raise FileNotFoundError(f"References directory not found: {references_dir}")

    guest_dirs = sorted(
        p for p in references_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if not guest_dirs:
        raise ValueError(
            f"No guest subdirectories found in '{references_dir}'. "
            "Expected layout: references/<guest_id>/<photo.jpg>"
        )

    profiles: list[GuestProfile] = []
    summaries: list[GuestProfileSummary] = []

    for guest_dir in guest_dirs:
        guest_id = guest_dir.name
        display_name = guest_id.replace("_", " ").replace("-", " ").title()
        ref_images = list_images_in_dir(guest_dir)
        if not ref_images:
            summaries.append(
                GuestProfileSummary(
                    guest_id=guest_id,
                    display_name=display_name,
                    accepted_reference_count=0,
                    accepted_files=[],
                    mean_intra_cosine=0.0,
                    warnings=["No supported reference images found in guest folder."],
                )
            )
            continue

        extraction_results = []
        for img_path in ref_images:
            bgr = load_image_bgr(img_path)
            analysis = engine.analyze_image(bgr)
            extraction_results.append(select_reference_face(analysis.faces, img_path.name))

        try:
            profile = build_guest_profile(guest_id, display_name, extraction_results)
        except ValueError as err:
            summaries.append(
                GuestProfileSummary(
                    guest_id=guest_id,
                    display_name=display_name,
                    accepted_reference_count=0,
                    accepted_files=[],
                    mean_intra_cosine=0.0,
                    warnings=[str(err)],
                )
            )
            continue

        profiles.append(profile)
        summaries.append(
            GuestProfileSummary(
                guest_id=profile.guest_id,
                display_name=profile.display_name,
                accepted_reference_count=len(profile.accepted_files),
                accepted_files=list(profile.accepted_files),
                mean_intra_cosine=round(profile.mean_intra_cosine, 4),
                warnings=list(profile.warnings),
            )
        )

    return profiles, summaries


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (len(sorted_values) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = idx - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _suggestion_to_dict(sug: MatchSuggestion) -> dict[str, Any]:
    return {
        "photo_id": sug.photo_id,
        "guest_id": sug.guest_id,
        "status": sug.status,
        "confidence": round(sug.confidence, 4),
        "max_ref_cosine": round(sug.max_ref_cosine, 4),
        "centroid_cosine": round(sug.centroid_cosine, 4),
        "l2_distance": round(sug.l2_distance, 4),
        "face_index": sug.face_index,
        "bbox": [round(v, 1) for v in sug.bbox],
        "resolution_source": sug.resolution_source,
        "scoring_strategy": sug.scoring_strategy,
        "ambiguous_face": sug.ambiguous_face,
        "model_version": sug.model_version,
    }


def _build_crop_image(
    image_bgr: Any,
    analysis: PhotoAnalysisResult,
    config: EngineConfig,
) -> Any:
    """Return the image coordinate space corresponding to ``analysis.faces``."""
    if analysis.resolution_source == "medium":
        medium_img, _ = resize_to_medium(image_bgr, max_edge=config.medium_max_edge)
        return medium_img
    orig_capped, _ = resize_to_medium(image_bgr, max_edge=config.max_original_edge)
    return orig_capped


def run_benchmark(
    references_dir: Path,
    album_dir: Path,
    engine: FaceRecognitionEngine,
    *,
    cosine_threshold: float = DEFAULT_COSINE_THRESHOLD,
    strategy: ScoringStrategy = "max_ref",
    ground_truth_csv: Path | None = None,
    include_crops: bool = True,
) -> BenchmarkSummary:
    """Run an end-to-end Phase 1 local recognition benchmark on a folder of photos."""
    profiles, profile_summaries = build_profiles_from_directory(references_dir, engine)
    album_paths = list_images_in_dir(album_dir)
    if not album_paths:
        raise ValueError(f"No supported images found in album directory: {album_dir}")

    ground_truth = load_ground_truth_csv(ground_truth_csv) if ground_truth_csv else None

    photo_records: list[PhotoBenchmarkRecord] = []
    all_photo_guest_scores: dict[tuple[str, str], float] = {}
    predicted_pairs: set[tuple[str, str]] = set()
    latencies_ms: list[float] = []
    total_faces = 0
    total_suggestions = 0
    retry_count = 0

    for photo_path in album_paths:
        photo_id = photo_path.name
        image_bgr = load_image_bgr(photo_path)
        analysis = engine.analyze_image(image_bgr)

        latencies_ms.append(analysis.total_ms)
        total_faces += len(analysis.faces)
        if analysis.used_original_retry:
            retry_count += 1

        # Record full score matrix for threshold sweep (best face score per photo/guest)
        for face in analysis.faces:
            for profile in profiles:
                conf, _, _, _ = score_face_against_profile(
                    face.embedding, profile, strategy=strategy
                )
                key = (photo_id, profile.guest_id)
                if key not in all_photo_guest_scores or conf > all_photo_guest_scores[key]:
                    all_photo_guest_scores[key] = conf

        suggestions = match_faces_to_guests(
            photo_id=photo_id,
            faces=analysis.faces,
            profiles=profiles,
            cosine_threshold=cosine_threshold,
            strategy=strategy,
        )
        total_suggestions += len(suggestions)

        crops: dict[str, str] = {}
        if include_crops and suggestions:
            coord_img = _build_crop_image(image_bgr, analysis, engine.config)
            for sug in suggestions:
                crops[sug.guest_id] = crop_face_thumbnail_data_url(coord_img, sug.bbox)

        for sug in suggestions:
            predicted_pairs.add((photo_id, sug.guest_id))

        photo_records.append(
            PhotoBenchmarkRecord(
                photo_id=photo_id,
                filename=photo_path.name,
                original_width=analysis.original_size[0],
                original_height=analysis.original_size[1],
                analyzed_width=analysis.analyzed_size[0],
                analyzed_height=analysis.analyzed_size[1],
                resolution_source=analysis.resolution_source,
                used_original_retry=analysis.used_original_retry,
                retry_reason=analysis.retry_reason,
                medium_face_count=analysis.medium_face_count,
                face_count=len(analysis.faces),
                detect_ms=round(analysis.detect_ms, 2),
                embed_ms=round(analysis.embed_ms, 2),
                total_ms=round(analysis.total_ms, 2),
                suggestions=[_suggestion_to_dict(s) for s in suggestions],
                suggestion_crops=crops,
            )
        )

    sorted_latencies = sorted(latencies_ms)
    total_sec = sum(latencies_ms) / 1000.0
    mean_ms = statistics.fmean(latencies_ms) if latencies_ms else 0.0
    p50_ms = _percentile(sorted_latencies, 50.0)
    p95_ms = _percentile(sorted_latencies, 95.0)
    throughput = (len(album_paths) / total_sec) if total_sec > 0 else 0.0

    gt_metrics: ThresholdMetrics | None = None
    sweep: list[ThresholdMetrics] = []
    recommended_thr: float | None = None
    if ground_truth is not None:
        gt_metrics = evaluate_pairs(predicted_pairs, ground_truth, cosine_threshold)
        sweep = compute_threshold_sweep(all_photo_guest_scores, ground_truth)
        best = max(sweep, key=lambda m: (m.f1, m.recall, m.precision))
        recommended_thr = best.threshold

    return BenchmarkSummary(
        model_version=COMBINED_MODEL_VERSION,
        scoring_strategy=strategy,
        cosine_threshold=cosine_threshold,
        guest_count=len(profiles),
        photo_count=len(album_paths),
        total_faces_detected=total_faces,
        total_suggestions=total_suggestions,
        original_retry_count=retry_count,
        original_retry_rate=round(retry_count / len(album_paths), 4) if album_paths else 0.0,
        mean_ms_per_photo=round(mean_ms, 2),
        p50_ms_per_photo=round(p50_ms, 2),
        p95_ms_per_photo=round(p95_ms, 2),
        throughput_photos_per_sec=round(throughput, 2),
        guest_profiles=profile_summaries,
        photos=photo_records,
        ground_truth_metrics=gt_metrics,
        threshold_sweep=sweep,
        recommended_threshold=recommended_thr,
    )


def export_benchmark_artifacts(
    summary: BenchmarkSummary,
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write ``results.json`` and ``suggestions.csv`` (strictly zero embeddings)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "results.json"
    csv_path = output_dir / "suggestions.csv"

    serializable_photos = []
    for rec in summary.photos:
        d = asdict(rec)
        d.pop("suggestion_crops", None)
        serializable_photos.append(d)

    payload: dict[str, Any] = {
        "model_version": summary.model_version,
        "scoring_strategy": summary.scoring_strategy,
        "cosine_threshold": summary.cosine_threshold,
        "guest_count": summary.guest_count,
        "photo_count": summary.photo_count,
        "total_faces_detected": summary.total_faces_detected,
        "total_suggestions": summary.total_suggestions,
        "original_retry_count": summary.original_retry_count,
        "original_retry_rate": summary.original_retry_rate,
        "mean_ms_per_photo": summary.mean_ms_per_photo,
        "p50_ms_per_photo": summary.p50_ms_per_photo,
        "p95_ms_per_photo": summary.p95_ms_per_photo,
        "throughput_photos_per_sec": summary.throughput_photos_per_sec,
        "guest_profiles": [asdict(g) for g in summary.guest_profiles],
        "ground_truth_metrics": (
            asdict(summary.ground_truth_metrics) if summary.ground_truth_metrics else None
        ),
        "threshold_sweep": [asdict(m) for m in summary.threshold_sweep],
        "recommended_threshold": summary.recommended_threshold,
        "photos": serializable_photos,
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "photo_id",
                "guest_id",
                "status",
                "confidence",
                "max_ref_cosine",
                "centroid_cosine",
                "l2_distance",
                "resolution_source",
                "ambiguous_face",
                "model_version",
            ]
        )
        for rec in summary.photos:
            for sug in rec.suggestions:
                writer.writerow(
                    [
                        sug["photo_id"],
                        sug["guest_id"],
                        sug["status"],
                        sug["confidence"],
                        sug["max_ref_cosine"],
                        sug["centroid_cosine"],
                        sug["l2_distance"],
                        sug["resolution_source"],
                        sug["ambiguous_face"],
                        sug["model_version"],
                    ]
                )

    return json_path, csv_path
