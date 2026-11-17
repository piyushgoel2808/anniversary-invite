"""Tests for multi-reference guest profiles and many-to-many suggestion matching."""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from face_indexer.matcher import (
    build_guest_profile,
    cosine_similarity,
    match_faces_to_guests,
    normalized_l2_distance,
    select_reference_face,
)
from face_indexer.recognizer import DetectedFace, normalize_embedding


def _basis_vec(index: int, dim: int = 128) -> NDArray[np.float32]:
    vec = np.zeros(dim, dtype=np.float32)
    vec[index] = 1.0
    return vec


def _make_face(
    face_index: int,
    embedding: NDArray[np.float32],
    bbox: tuple[float, float, float, float] = (10.0, 10.0, 100.0, 100.0),
    score: float = 0.95,
) -> DetectedFace:
    return DetectedFace(
        face_index=face_index,
        bbox=bbox,
        detection_score=score,
        landmarks=((0.0, 0.0),) * 5,
        embedding=normalize_embedding(embedding),
        resolution_source="medium",
    )


def test_cosine_and_l2_identities() -> None:
    a = _basis_vec(0)
    b = _basis_vec(1)
    assert pytest.approx(cosine_similarity(a, a), abs=1e-6) == 1.0
    assert pytest.approx(normalized_l2_distance(a, a), abs=1e-6) == 0.0
    assert pytest.approx(cosine_similarity(a, b), abs=1e-6) == 0.0
    assert pytest.approx(normalized_l2_distance(a, b), abs=1e-6) == float(np.sqrt(2.0))


def test_select_reference_face_rejects_empty_and_ambiguous_group_photos() -> None:
    empty = select_reference_face([], "empty.jpg")
    assert not empty.accepted

    single = select_reference_face([_make_face(0, _basis_vec(0))], "solo.jpg")
    assert single.accepted
    assert single.embedding is not None

    # Two equal-sized faces -> rejected as ambiguous
    group_two = [
        _make_face(0, _basis_vec(0), bbox=(0, 0, 100, 100)),
        _make_face(1, _basis_vec(1), bbox=(150, 0, 95, 95)),
    ]
    ambiguous = select_reference_face(group_two, "couple.jpg")
    assert not ambiguous.accepted
    assert "Ambiguous" in (ambiguous.reason or "")

    # One dominant foreground face + tiny background bystander -> accepted
    foreground_bg = [
        _make_face(0, _basis_vec(0), bbox=(0, 0, 200, 200)),
        _make_face(1, _basis_vec(1), bbox=(300, 10, 40, 40)),
    ]
    dominant = select_reference_face(foreground_bg, "selfie_bg.jpg")
    assert dominant.accepted
    assert dominant.embedding is not None
    assert pytest.approx(cosine_similarity(dominant.embedding, _basis_vec(0)), abs=1e-6) == 1.0


def test_build_guest_profile_and_warnings() -> None:
    v0 = _basis_vec(0)
    v0_noisy = normalize_embedding(0.95 * _basis_vec(0) + 0.31 * _basis_vec(2))
    res1 = select_reference_face([_make_face(0, v0)], "r1.jpg")
    res2 = select_reference_face([_make_face(0, v0_noisy)], "r2.jpg")
    res_bad = select_reference_face([], "r3.jpg")

    profile = build_guest_profile("gst_navneet", "Navneet", [res1, res2, res_bad])
    assert len(profile.reference_embeddings) == 2
    assert profile.accepted_files == ("r1.jpg", "r2.jpg")
    assert profile.mean_intra_cosine > 0.9
    assert any("Skipped r3.jpg" in w for w in profile.warnings)

    # Mismatched reference selfies trigger consistency warning
    mismatch = build_guest_profile(
        "gst_bad",
        "Mismatch",
        [
            select_reference_face([_make_face(0, _basis_vec(0))], "a.jpg"),
            select_reference_face([_make_face(0, _basis_vec(5))], "b.jpg"),
        ],
    )
    assert any("Low similarity" in w for w in mismatch.warnings)

    with pytest.raises(ValueError, match="no valid reference faces"):
        build_guest_profile("gst_empty", "Empty", [res_bad])


def test_many_to_many_matching_deduplication_and_pending_status() -> None:
    navneet_vec = _basis_vec(0)
    ruby_vec = _basis_vec(1)
    stranger_vec = _basis_vec(2)

    p_navneet = build_guest_profile(
        "gst_navneet", "Navneet", [select_reference_face([_make_face(0, navneet_vec)], "n1.jpg")]
    )
    p_ruby = build_guest_profile(
        "gst_ruby", "Ruby", [select_reference_face([_make_face(0, ruby_vec)], "r1.jpg")]
    )

    # Group photo with Navneet (twice, e.g., reflection with lower score), Ruby, and a stranger
    faces = [
        _make_face(0, normalize_embedding(0.75 * navneet_vec + 0.66 * _basis_vec(10))),
        _make_face(1, normalize_embedding(0.96 * navneet_vec + 0.28 * _basis_vec(11))),
        _make_face(2, normalize_embedding(0.92 * ruby_vec + 0.39 * _basis_vec(12))),
        _make_face(3, stranger_vec),
    ]

    suggestions = match_faces_to_guests(
        photo_id="pho_001",
        faces=faces,
        profiles=[p_navneet, p_ruby],
        cosine_threshold=0.363,
        strategy="max_ref",
    )

    assert len(suggestions) == 2
    assert {s.guest_id for s in suggestions} == {"gst_navneet", "gst_ruby"}
    assert all(s.status == "PENDING" for s in suggestions)
    assert all(not s.ambiguous_face for s in suggestions)

    # Navneet's best face (face_index=1) should be selected, not face_index=0
    nav_sug = next(s for s in suggestions if s.guest_id == "gst_navneet")
    assert nav_sug.face_index == 1
    assert nav_sug.confidence > 0.90


def test_ambiguous_face_collision_flagged() -> None:
    # Two near-identical profiles colliding on a single detected face
    shared = _basis_vec(0)
    twin_a = build_guest_profile(
        "gst_a", "Twin A", [select_reference_face([_make_face(0, shared)], "a.jpg")]
    )
    twin_b = build_guest_profile(
        "gst_b",
        "Twin B",
        [select_reference_face([_make_face(0, 0.98 * shared + 0.2 * _basis_vec(1))], "b.jpg")],
    )

    suggestions = match_faces_to_guests(
        photo_id="pho_twin",
        faces=[_make_face(0, shared)],
        profiles=[twin_a, twin_b],
        cosine_threshold=0.363,
        strategy="hybrid",
    )
    assert len(suggestions) == 2
    assert all(s.ambiguous_face for s in suggestions)
