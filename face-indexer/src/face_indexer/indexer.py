"""indexer.py — Incremental local album indexing, guest registration, and SQLite matching.

Uses SHA-256 content hashes to skip already-processed photos and reference images,
and stores face embeddings locally in SQLite so matching a newly registered guest
against 1,000+ album photos takes milliseconds without re-running YuNet or SFace.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from face_indexer.imaging import list_images_in_dir, load_image_bgr
from face_indexer.matcher import (
    ScoringStrategy,
    match_faces_to_guests,
    select_reference_face,
)
from face_indexer.models import COMBINED_MODEL_VERSION
from face_indexer.recognizer import DetectedFace, FaceRecognitionEngine
from face_indexer.storage import SQLiteFaceStore

# Calibrated on Silver Jubilee real family album (100% precision across Navneet & Ruby)
CALIBRATED_COSINE_THRESHOLD = 0.50


@dataclass(frozen=True)
class IndexSummary:
    """Telemetry returned by incremental album indexing."""

    scanned: int
    skipped_cached: int
    newly_indexed: int
    faces_detected: int
    errors: tuple[str, ...] = field(default_factory=tuple)
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class GuestRegistrationSummary:
    """Telemetry returned by guest reference registration."""

    guest_id: str
    display_name: str
    scanned: int
    skipped_cached: int
    accepted: int
    rejected: int
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MatchSummary:
    """Telemetry returned by matching stored faces against active guest profiles."""

    photos_checked: int
    guests_checked: int
    new_suggestions: int
    total_pending: int
    threshold: float
    elapsed_ms: float


def compute_file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file using streaming 64 KB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def index_album_folder(
    album_dir: Path,
    store: SQLiteFaceStore,
    engine: FaceRecognitionEngine,
    *,
    photo_id_by_filename: dict[str, str] | None = None,
) -> IndexSummary:
    """Incrementally index all supported photos in ``album_dir`` into SQLite.

    Photos whose ``(photo_id, file_hash, model_version)`` already exist in SQLite
    are skipped without re-running face detection or embedding extraction.
    """
    t0 = time.perf_counter()
    image_paths = list_images_in_dir(album_dir)
    skipped = 0
    newly_indexed = 0
    faces_detected = 0
    errors: list[str] = []

    for img_path in image_paths:
        photo_id = (
            photo_id_by_filename.get(img_path.name, img_path.name)
            if photo_id_by_filename
            else img_path.name
        )
        try:
            file_hash = compute_file_sha256(img_path)
            if store.is_photo_indexed(photo_id, file_hash, COMBINED_MODEL_VERSION):
                skipped += 1
                continue

            bgr = load_image_bgr(img_path)
            orig_h, orig_w = bgr.shape[:2]
            analysis = engine.analyze_image(bgr)
            store.save_photo_analysis(
                photo_id=photo_id,
                file_hash=file_hash,
                filename=img_path.name,
                original_width=orig_w,
                original_height=orig_h,
                analysis=analysis,
            )
            newly_indexed += 1
            faces_detected += len(analysis.faces)
        except Exception as exc:
            errors.append(f"{img_path.name}: {exc}")

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return IndexSummary(
        scanned=len(image_paths),
        skipped_cached=skipped,
        newly_indexed=newly_indexed,
        faces_detected=faces_detected,
        errors=tuple(errors),
        elapsed_ms=round(elapsed_ms, 2),
    )


def register_guest_folder(
    guest_id: str,
    display_name: str,
    ref_dir: Path,
    store: SQLiteFaceStore,
    engine: FaceRecognitionEngine,
    *,
    profile_version: int = 1,
) -> GuestRegistrationSummary:
    """Incrementally register a guest's reference photos into SQLite."""
    store.save_guest(
        guest_id=guest_id,
        display_name=display_name,
        profile_version=profile_version,
        status="ACTIVE",
    )
    image_paths = list_images_in_dir(ref_dir)
    skipped = 0
    accepted = 0
    rejected = 0
    reasons: list[str] = []

    for img_path in image_paths:
        file_hash = compute_file_sha256(img_path)
        if store.has_guest_reference(guest_id, file_hash):
            skipped += 1
            continue

        try:
            bgr = load_image_bgr(img_path)
            analysis = engine.analyze_image(bgr)
            res = select_reference_face(analysis.faces, img_path.name)
            if res.accepted and res.embedding is not None:
                store.save_guest_reference(
                    guest_id=guest_id,
                    filename=img_path.name,
                    file_hash=file_hash,
                    detection_score=res.detection_score or 0.0,
                    embedding=res.embedding,
                    accepted=True,
                )
                accepted += 1
                if res.reason:
                    reasons.append(f"{img_path.name}: {res.reason}")
            else:
                rejected += 1
                reasons.append(f"{img_path.name}: {res.reason or 'rejected'}")
        except Exception as exc:
            rejected += 1
            reasons.append(f"{img_path.name}: {exc}")

    return GuestRegistrationSummary(
        guest_id=guest_id,
        display_name=display_name,
        scanned=len(image_paths),
        skipped_cached=skipped,
        accepted=accepted,
        rejected=rejected,
        reasons=tuple(reasons),
    )


def register_references_root(
    references_root: Path,
    store: SQLiteFaceStore,
    engine: FaceRecognitionEngine,
) -> list[GuestRegistrationSummary]:
    """Register all guest subfolders under ``references_root`` (one subfolder per guest_id)."""
    if not references_root.is_dir():
        raise FileNotFoundError(f"References directory not found: {references_root}")

    summaries: list[GuestRegistrationSummary] = []
    for subdir in sorted(p for p in references_root.iterdir() if p.is_dir()):
        guest_id = subdir.name
        display_name = guest_id.replace("_", " ").replace("-", " ").title()
        summaries.append(
            register_guest_folder(
                guest_id=guest_id,
                display_name=display_name,
                ref_dir=subdir,
                store=store,
                engine=engine,
            )
        )
    return summaries


def run_matching(
    store: SQLiteFaceStore,
    *,
    threshold: float = CALIBRATED_COSINE_THRESHOLD,
    strategy: ScoringStrategy = "max_ref",
) -> MatchSummary:
    """Match all indexed faces in SQLite against all active guest profiles.

    Inserts new PENDING suggestions into SQLite while preserving any previously
    CONFIRMED or REJECTED decisions.
    """
    t0 = time.perf_counter()
    profiles = store.get_guest_profiles()
    face_records = store.get_all_faces()

    faces_by_photo: dict[str, list[DetectedFace]] = defaultdict(list)
    for rec in face_records:
        faces_by_photo[rec.photo_id].append(
            DetectedFace(
                face_index=rec.face_index,
                bbox=rec.bbox,
                detection_score=rec.detection_score,
                landmarks=(),
                embedding=rec.embedding,
                resolution_source=rec.resolution_source,
            )
        )

    new_suggestions = 0
    if profiles and faces_by_photo:
        for photo_id, faces in faces_by_photo.items():
            suggestions = match_faces_to_guests(
                photo_id=photo_id,
                faces=faces,
                profiles=profiles,
                cosine_threshold=threshold,
                strategy=strategy,
            )
            if suggestions:
                new_suggestions += store.save_suggestions(suggestions)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    total_pending = len(store.get_pending_suggestions())

    return MatchSummary(
        photos_checked=len(faces_by_photo),
        guests_checked=len(profiles),
        new_suggestions=new_suggestions,
        total_pending=total_pending,
        threshold=threshold,
        elapsed_ms=round(elapsed_ms, 2),
    )
