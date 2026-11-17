"""Multi-reference guest profile building and suggestion matching logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from face_indexer.models import COMBINED_MODEL_VERSION
from face_indexer.recognizer import DetectedFace, ResolutionSource, normalize_embedding

# Official OpenCV SFace baseline thresholds (for calibration starting point only)
DEFAULT_COSINE_THRESHOLD = 0.363
DEFAULT_L2_THRESHOLD = 1.128

ScoringStrategy = Literal["max_ref", "centroid", "hybrid"]
MatchStatus = Literal["PENDING", "CONFIRMED", "REJECTED", "ADMIN_CONFIRMED", "REVOKED"]


@dataclass(frozen=True, slots=True)
class ReferencePhotoResult:
    """Extraction status for one guest reference photo."""

    filename: str
    accepted: bool
    faces_detected: int
    reason: str | None = None
    embedding: NDArray[np.float32] | None = None
    detection_score: float | None = None


@dataclass(frozen=True, slots=True)
class GuestProfile:
    """In-memory multi-reference face profile for a single consenting guest."""

    guest_id: str
    display_name: str
    reference_embeddings: tuple[NDArray[np.float32], ...]
    centroid_embedding: NDArray[np.float32]
    accepted_files: tuple[str, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    mean_intra_cosine: float = 1.0
    model_version: str = COMBINED_MODEL_VERSION


@dataclass(frozen=True, slots=True)
class MatchSuggestion:
    """A candidate photo-to-guest suggestion awaiting guest confirmation."""

    photo_id: str
    guest_id: str
    confidence: float
    max_ref_cosine: float
    centroid_cosine: float
    l2_distance: float
    face_index: int
    bbox: tuple[float, float, float, float]
    resolution_source: ResolutionSource
    scoring_strategy: ScoringStrategy
    status: MatchStatus = "PENDING"
    ambiguous_face: bool = False
    model_version: str = COMBINED_MODEL_VERSION


def cosine_similarity(a: NDArray[np.float32], b: NDArray[np.float32]) -> float:
    """Cosine similarity between two L2-normalized embeddings (identical to SFace FR_COSINE)."""
    vec_a = normalize_embedding(a)
    vec_b = normalize_embedding(b)
    return float(np.clip(np.dot(vec_a, vec_b), -1.0, 1.0))


def normalized_l2_distance(a: NDArray[np.float32], b: NDArray[np.float32]) -> float:
    """Euclidean distance between two L2-normalized embeddings (identical to SFace FR_NORM_L2)."""
    vec_a = normalize_embedding(a)
    vec_b = normalize_embedding(b)
    return float(np.linalg.norm(vec_a - vec_b))


def select_reference_face(
    faces: list[DetectedFace],
    filename: str,
    *,
    dominance_ratio: float = 2.0,
) -> ReferencePhotoResult:
    """Select the single unambiguous face from a guest reference photograph.

    Rejects reference photos with 0 faces or multiple similarly sized faces so a
    group photo does not silently corrupt a guest's face profile.
    """
    if len(faces) == 0:
        return ReferencePhotoResult(
            filename=filename,
            accepted=False,
            faces_detected=0,
            reason="No face detected in reference photo.",
        )

    if len(faces) == 1:
        chosen = faces[0]
        return ReferencePhotoResult(
            filename=filename,
            accepted=True,
            faces_detected=1,
            embedding=chosen.embedding,
            detection_score=chosen.detection_score,
        )

    # Sort descending by bounding box area
    ranked = sorted(faces, key=lambda f: f.bbox[2] * f.bbox[3], reverse=True)
    largest_area = ranked[0].bbox[2] * ranked[0].bbox[3]
    second_area = ranked[1].bbox[2] * ranked[1].bbox[3]

    if second_area > 0 and (largest_area / second_area) < dominance_ratio:
        return ReferencePhotoResult(
            filename=filename,
            accepted=False,
            faces_detected=len(faces),
            reason=(
                f"Ambiguous reference photo: {len(faces)} faces detected with no clear "
                "primary subject."
            ),
        )

    chosen = ranked[0]
    return ReferencePhotoResult(
        filename=filename,
        accepted=True,
        faces_detected=len(faces),
        reason=f"Selected dominant primary face out of {len(faces)} detected.",
        embedding=chosen.embedding,
        detection_score=chosen.detection_score,
    )


def build_guest_profile(
    guest_id: str,
    display_name: str,
    reference_results: list[ReferencePhotoResult],
) -> GuestProfile:
    """Combine 1..N accepted reference face embeddings into a GuestProfile.

    Raises:
        ValueError: If zero accepted reference embeddings are available.
    """
    accepted_embs: list[NDArray[np.float32]] = []
    accepted_files: list[str] = []
    warnings: list[str] = []

    for res in reference_results:
        if res.accepted and res.embedding is not None:
            accepted_embs.append(normalize_embedding(res.embedding))
            accepted_files.append(res.filename)
            if res.reason:
                warnings.append(f"{res.filename}: {res.reason}")
        else:
            warnings.append(f"Skipped {res.filename}: {res.reason or 'rejected'}")

    if not accepted_embs:
        raise ValueError(
            f"Cannot build profile for guest '{guest_id}': no valid reference faces accepted."
        )

    if len(accepted_embs) == 1:
        warnings.append("Only 1 reference photo accepted; 2-4 varied photos are recommended.")

    stacked = np.stack(accepted_embs, axis=0)
    centroid = normalize_embedding(np.mean(stacked, axis=0, dtype=np.float32))

    # Measure pairwise consistency across reference images to flag mismatched uploads
    intra_scores: list[float] = []
    for i in range(len(accepted_embs)):
        for j in range(i + 1, len(accepted_embs)):
            intra_scores.append(cosine_similarity(accepted_embs[i], accepted_embs[j]))

    mean_intra = float(np.mean(intra_scores)) if intra_scores else 1.0
    if intra_scores and min(intra_scores) < 0.25:
        warnings.append(
            f"Low similarity ({min(intra_scores):.3f}) between reference photos — "
            "verify all reference selfies belong to the same guest or span large age gap."
        )

    return GuestProfile(
        guest_id=guest_id,
        display_name=display_name,
        reference_embeddings=tuple(accepted_embs),
        centroid_embedding=centroid,
        accepted_files=tuple(accepted_files),
        warnings=tuple(warnings),
        mean_intra_cosine=mean_intra,
    )


def score_face_against_profile(
    face_embedding: NDArray[np.float32],
    profile: GuestProfile,
    strategy: ScoringStrategy = "max_ref",
) -> tuple[float, float, float, float]:
    """Score a detected face against a guest's multi-reference profile.

    Returns:
        ``(combined_confidence, max_ref_cosine, centroid_cosine, best_l2_distance)``
    """
    ref_cosines = [
        cosine_similarity(face_embedding, ref_emb) for ref_emb in profile.reference_embeddings
    ]
    ref_l2s = [
        normalized_l2_distance(face_embedding, ref_emb) for ref_emb in profile.reference_embeddings
    ]
    max_ref_cos = max(ref_cosines)
    min_ref_l2 = min(ref_l2s)
    centroid_cos = cosine_similarity(face_embedding, profile.centroid_embedding)

    if strategy == "max_ref":
        combined = max_ref_cos
    elif strategy == "centroid":
        combined = centroid_cos
    elif strategy == "hybrid":
        combined = 0.7 * max_ref_cos + 0.3 * centroid_cos
    else:  # pragma: no cover - guarded by Literal type
        raise ValueError(f"Unsupported scoring strategy: {strategy}")

    return combined, max_ref_cos, centroid_cos, min_ref_l2


def match_faces_to_guests(
    photo_id: str,
    faces: list[DetectedFace],
    profiles: list[GuestProfile],
    *,
    cosine_threshold: float = DEFAULT_COSINE_THRESHOLD,
    strategy: ScoringStrategy = "max_ref",
) -> list[MatchSuggestion]:
    """Generate PENDING suggestions for all registered guests appearing in a photo.

    Semantics:
    - Many-to-many: a group photo may produce suggestions for multiple guests.
    - Deduplicated per guest: each guest receives at most one suggestion per photo
      (their highest-scoring detected face).
    - Ambiguity tracking: if the same detected face exceeds threshold for 2+ guests,
      those suggestions are flagged ``ambiguous_face=True`` for diagnostic visibility.
    - All returned suggestions carry ``status="PENDING"`` (never auto-published).
    """
    if not faces or not profiles:
        return []

    # Track best matching face per guest above threshold
    best_per_guest: dict[str, MatchSuggestion] = {}
    face_matched_guests: dict[int, set[str]] = {}

    for face in faces:
        for profile in profiles:
            confidence, max_ref_cos, centroid_cos, l2_dist = score_face_against_profile(
                face.embedding, profile, strategy=strategy
            )
            if confidence < cosine_threshold:
                continue

            face_matched_guests.setdefault(face.face_index, set()).add(profile.guest_id)
            existing = best_per_guest.get(profile.guest_id)
            if existing is None or confidence > existing.confidence:
                best_per_guest[profile.guest_id] = MatchSuggestion(
                    photo_id=photo_id,
                    guest_id=profile.guest_id,
                    confidence=confidence,
                    max_ref_cosine=max_ref_cos,
                    centroid_cosine=centroid_cos,
                    l2_distance=l2_dist,
                    face_index=face.face_index,
                    bbox=face.bbox,
                    resolution_source=face.resolution_source,
                    scoring_strategy=strategy,
                    status="PENDING",
                    ambiguous_face=False,
                )

    # Mark suggestions whose matched face collided with another guest above threshold
    suggestions: list[MatchSuggestion] = []
    for sug in best_per_guest.values():
        is_ambiguous = len(face_matched_guests.get(sug.face_index, set())) > 1
        if is_ambiguous:
            suggestions.append(
                MatchSuggestion(
                    photo_id=sug.photo_id,
                    guest_id=sug.guest_id,
                    confidence=sug.confidence,
                    max_ref_cosine=sug.max_ref_cosine,
                    centroid_cosine=sug.centroid_cosine,
                    l2_distance=sug.l2_distance,
                    face_index=sug.face_index,
                    bbox=sug.bbox,
                    resolution_source=sug.resolution_source,
                    scoring_strategy=sug.scoring_strategy,
                    status=sug.status,
                    ambiguous_face=True,
                    model_version=sug.model_version,
                )
            )
        else:
            suggestions.append(sug)

    suggestions.sort(key=lambda item: (-item.confidence, item.guest_id))
    return suggestions
