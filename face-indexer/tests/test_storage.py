"""test_storage.py — Unit tests for SQLiteFaceStore."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from cryptography.fernet import InvalidToken

from face_indexer.matcher import MatchSuggestion
from face_indexer.recognizer import DetectedFace, PhotoAnalysisResult
from face_indexer.storage import SQLiteFaceStore, load_or_create_encryption_key


@pytest.fixture
def store(tmp_path: Path) -> SQLiteFaceStore:
    db_file = tmp_path / "test_indexer.db"
    return SQLiteFaceStore(db_file)


def test_photo_and_face_storage_roundtrip(store: SQLiteFaceStore) -> None:
    assert store.get_photo_count() == 0
    assert store.get_face_count() == 0
    assert not store.is_photo_indexed("photo_1.jpg", "hash123")

    # Create dummy embeddings
    emb1 = np.ones((128,), dtype=np.float32) * 0.1
    emb1 /= np.linalg.norm(emb1)

    face1 = DetectedFace(
        face_index=0,
        bbox=(10.0, 20.0, 50.0, 60.0),
        detection_score=0.95,
        landmarks=(),
        embedding=emb1,
        resolution_source="medium",
    )

    analysis = PhotoAnalysisResult(
        faces=[face1],
        original_size=(1000, 800),
        analyzed_size=(500, 400),
        resolution_source="medium",
        used_original_retry=False,
        retry_reason=None,
        medium_face_count=1,
        detect_ms=25.0,
        embed_ms=10.0,
        total_ms=35.0,
        model_version="yunet_2023mar+sface_2021dec",
    )

    store.save_photo_analysis(
        photo_id="photo_1.jpg",
        file_hash="hash123",
        filename="photo_1.jpg",
        original_width=1000,
        original_height=800,
        analysis=analysis,
    )

    assert store.get_photo_count() == 1
    assert store.get_face_count() == 1
    assert store.is_photo_indexed("photo_1.jpg", "hash123")
    assert not store.is_photo_indexed("photo_1.jpg", "other_hash")

    faces = store.get_all_faces()
    assert len(faces) == 1
    record = faces[0]
    assert record.photo_id == "photo_1.jpg"
    assert record.face_index == 0
    assert pytest.approx(record.detection_score, 0.001) == 0.95
    assert record.bbox == (10.0, 20.0, 50.0, 60.0)
    assert np.allclose(record.embedding, emb1, atol=1e-5)


def test_guest_profiles_and_references(store: SQLiteFaceStore) -> None:
    store.save_guest("guest_navneet", "Navneet", profile_version=1)

    # Add 2 reference embeddings
    emb1 = np.zeros((128,), dtype=np.float32)
    emb1[0] = 1.0
    emb2 = np.zeros((128,), dtype=np.float32)
    emb2[0] = 0.9
    emb2[1] = 0.1
    emb2 /= np.linalg.norm(emb2)

    store.save_guest_reference("guest_navneet", "ref1.jpg", "hash_ref1", 0.98, emb1, accepted=True)
    store.save_guest_reference("guest_navneet", "ref2.jpg", "hash_ref2", 0.96, emb2, accepted=True)

    profiles = store.get_guest_profiles()
    assert len(profiles) == 1
    prof = profiles[0]
    assert prof.guest_id == "guest_navneet"
    assert prof.display_name == "Navneet"
    assert len(prof.accepted_files) == 2
    assert prof.centroid_embedding is not None
    assert prof.mean_intra_cosine > 0.8


def test_suggestions_lifecycle(store: SQLiteFaceStore) -> None:
    store.save_guest("guest_ruby", "Ruby")

    # Insert dummy photos to satisfy foreign key constraint
    dummy_analysis = PhotoAnalysisResult(
        faces=[],
        original_size=(500, 500),
        analyzed_size=(500, 500),
        resolution_source="medium",
        used_original_retry=False,
        retry_reason=None,
        medium_face_count=0,
        detect_ms=10.0,
        embed_ms=0.0,
        total_ms=10.0,
        model_version="yunet_2023mar+sface_2021dec",
    )
    store.save_photo_analysis("img_1.jpg", "hash1", "img_1.jpg", 500, 500, dummy_analysis)
    store.save_photo_analysis("img_2.jpg", "hash2", "img_2.jpg", 500, 500, dummy_analysis)

    s1 = MatchSuggestion(
        photo_id="img_1.jpg",
        guest_id="guest_ruby",
        confidence=0.68,
        max_ref_cosine=0.68,
        centroid_cosine=0.69,
        l2_distance=0.85,
        face_index=0,
        bbox=(100.0, 100.0, 200.0, 200.0),
        resolution_source="medium",
        scoring_strategy="max_ref",
        status="PENDING",
    )
    s2 = MatchSuggestion(
        photo_id="img_2.jpg",
        guest_id="guest_ruby",
        confidence=0.55,
        max_ref_cosine=0.55,
        centroid_cosine=0.56,
        l2_distance=0.95,
        face_index=1,
        bbox=(50.0, 50.0, 150.0, 150.0),
        resolution_source="medium",
        scoring_strategy="max_ref",
        status="PENDING",
    )

    inserted = store.save_suggestions([s1, s2])
    assert inserted == 2

    # Duplicate should not re-insert
    inserted_dup = store.save_suggestions([s1])
    assert inserted_dup == 0

    pending = store.get_pending_suggestions("guest_ruby")
    assert len(pending) == 2
    assert pending[0].confidence >= pending[1].confidence  # Ordered DESC

    unsynced = store.get_unsynced_suggestions()
    assert len(unsynced) == 2

    # Mark s1 as synced
    store.mark_suggestions_synced([pending[0].id])
    unsynced_now = store.get_unsynced_suggestions()
    assert len(unsynced_now) == 1
    assert unsynced_now[0].id == pending[1].id

    # Update suggestion status to CONFIRMED
    store.update_suggestion_status("img_1.jpg", "guest_ruby", "CONFIRMED")
    pending_after = store.get_pending_suggestions("guest_ruby")
    assert len(pending_after) == 1
    assert pending_after[0].photo_id == "img_2.jpg"


def test_sync_state_and_stats(store: SQLiteFaceStore) -> None:
    store.set_sync_state("last_sync_timestamp", "2026-11-17T20:00:00Z")
    assert store.get_sync_state("last_sync_timestamp") == "2026-11-17T20:00:00Z"
    assert store.get_sync_state("non_existent") is None

    stats = store.get_stats()
    assert stats["photos"] == 0
    assert stats["faces"] == 0


def test_revoke_guest_deletes_references_and_revokes_suggestions(store: SQLiteFaceStore) -> None:
    store.save_guest("guest_test", "Test Guest")
    emb = np.ones((128,), dtype=np.float32)
    emb /= np.linalg.norm(emb)
    store.save_guest_reference("guest_test", "ref.jpg", "ref_hash", 0.99, emb)
    assert store.has_guest_reference("guest_test", "ref_hash")
    assert len(store.get_guest_profiles()) == 1

    dummy_analysis = PhotoAnalysisResult(
        faces=[],
        original_size=(500, 500),
        analyzed_size=(500, 500),
        resolution_source="medium",
        used_original_retry=False,
        retry_reason=None,
        medium_face_count=0,
        detect_ms=10.0,
        embed_ms=0.0,
        total_ms=10.0,
        model_version="yunet_2023mar+sface_2021dec",
    )
    store.save_photo_analysis("p1.jpg", "h1", "p1.jpg", 500, 500, dummy_analysis)
    store.save_suggestions(
        [
            MatchSuggestion(
                photo_id="p1.jpg",
                guest_id="guest_test",
                confidence=0.72,
                max_ref_cosine=0.72,
                centroid_cosine=0.72,
                l2_distance=0.75,
                face_index=0,
                bbox=(10.0, 10.0, 40.0, 40.0),
                resolution_source="medium",
                scoring_strategy="max_ref",
                status="PENDING",
            )
        ]
    )

    store.revoke_guest("guest_test")

    assert not store.has_guest_reference("guest_test", "ref_hash")
    assert len(store.get_guest_profiles()) == 0
    assert len(store.get_pending_suggestions("guest_test")) == 0
    unsynced = store.get_unsynced_suggestions()
    assert len(unsynced) == 1
    assert unsynced[0].status == "REVOKED"


def test_encrypted_embedding_roundtrip(tmp_path: Path) -> None:
    import sqlite3

    key_path = tmp_path / ".embedding_key"
    key = load_or_create_encryption_key(key_path)
    assert key_path.is_file()
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert load_or_create_encryption_key(key_path) == key

    db_path = tmp_path / "encrypted.db"
    enc_store = SQLiteFaceStore(db_path, encryption_key=key)

    emb = np.linspace(0.0, 1.0, 128, dtype=np.float32)
    emb /= np.linalg.norm(emb)

    enc_store.save_guest("g1", "Guest One")
    enc_store.save_guest_reference("g1", "ref.jpg", "h_ref", 0.97, emb)

    # Raw blob stored in SQLite must NOT equal plaintext float32 bytes
    conn = sqlite3.connect(str(db_path))
    raw_blob = conn.execute("SELECT embedding FROM guest_references").fetchone()[0]
    conn.close()
    assert raw_blob != emb.tobytes()

    # Right key decrypts accurately
    profiles = enc_store.get_guest_profiles()
    assert len(profiles) == 1
    assert np.allclose(profiles[0].reference_embeddings[0], emb, atol=1e-6)

    # Wrong key fails decryption
    wrong_key = load_or_create_encryption_key(tmp_path / ".wrong_key")
    bad_store = SQLiteFaceStore(db_path, encryption_key=wrong_key)
    with pytest.raises(InvalidToken):
        bad_store.get_guest_profiles()


