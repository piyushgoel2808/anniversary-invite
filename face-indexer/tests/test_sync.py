"""test_sync.py — Tests for incremental indexing, SQLite matching, and Google Workspace sync."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import cast

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from face_indexer.indexer import (
    compute_file_sha256,
    index_album_folder,
    register_references_root,
    run_matching,
)
from face_indexer.models import COMBINED_MODEL_VERSION
from face_indexer.recognizer import DetectedFace, FaceRecognitionEngine, PhotoAnalysisResult
from face_indexer.storage import SQLiteFaceStore
from face_indexer.sync import (
    PROTECTED_REVIEW_STATUSES,
    PhotoProcessingRecord,
    RemoteFaceMatch,
    RemoteFaceProfile,
    RemotePhoto,
    RemoteReferenceFile,
    run_sync,
)


def _unit_vec(seed: int) -> NDArray[np.float32]:
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(128).astype(np.float32)
    vec /= float(np.linalg.norm(vec))
    return vec


def _make_dummy_jpeg(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (200, 200), color=color)
    img.save(path, format="JPEG")
    return path


class _StubEngine:
    """Deterministic stub engine mapping RGB color channel[0] -> synthetic face embeddings."""

    def __init__(self, embedding_by_red_bucket: dict[int, list[NDArray[np.float32]]]) -> None:
        self._map = embedding_by_red_bucket
        self.calls = 0

    def analyze_image(self, image_bgr: NDArray[np.uint8]) -> PhotoAnalysisResult:
        self.calls += 1
        # OpenCV BGR -> red channel is index 2
        red_val = int(image_bgr[10, 10, 2])
        bucket = round(red_val / 50) * 50
        embs = self._map.get(bucket, [])
        faces = [
            DetectedFace(
                face_index=i,
                bbox=(10.0 + i * 30.0, 10.0, 50.0, 50.0),
                detection_score=0.95,
                landmarks=(),
                embedding=e,
                resolution_source="medium",
            )
            for i, e in enumerate(embs)
        ]
        h, w = image_bgr.shape[:2]
        return PhotoAnalysisResult(
            faces=faces,
            original_size=(w, h),
            analyzed_size=(w, h),
            resolution_source="medium",
            used_original_retry=False,
            retry_reason=None,
            medium_face_count=len(faces),
            detect_ms=5.0,
            embed_ms=2.0,
            total_ms=7.0,
            model_version=COMBINED_MODEL_VERSION,
        )


class _FakeGalleryBackend:
    """In-memory mock of Google Drive + Sheets for deterministic sync tests."""

    def __init__(self, fixtures_dir: Path) -> None:
        self.fixtures_dir = fixtures_dir
        self.sheets_ensured = False
        self.photos: list[RemotePhoto] = []
        self.photo_files: dict[str, Path] = {}
        self.profiles: dict[str, RemoteFaceProfile] = {}
        self.ref_files: dict[str, list[tuple[RemoteReferenceFile, Path]]] = {}
        self.deleted_ref_ids: list[str] = []
        self.matches: dict[tuple[str, str], RemoteFaceMatch] = {}
        self.processing_log: list[PhotoProcessingRecord] = []

    def ensure_face_sheets(self) -> None:
        self.sheets_ensured = True

    def list_active_photos(self) -> list[RemotePhoto]:
        return [p for p in self.photos if p.status == "ACTIVE"]

    def list_face_profiles(self) -> list[RemoteFaceProfile]:
        return list(self.profiles.values())

    def list_face_matches(self) -> list[RemoteFaceMatch]:
        return list(self.matches.values())

    def download_photo(self, remote_photo: RemotePhoto, dest_path: Path) -> Path:
        src = self.photo_files[remote_photo.photo_id]
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_path)
        return dest_path

    def list_guest_reference_files(self, guest_id: str) -> list[RemoteReferenceFile]:
        return [rf for rf, _ in self.ref_files.get(guest_id, [])]

    def download_reference_file(self, ref_file: RemoteReferenceFile, dest_path: Path) -> Path:
        for items in self.ref_files.values():
            for rf, src in items:
                if rf.file_id == ref_file.file_id:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest_path)
                    return dest_path
        raise FileNotFoundError(ref_file.file_id)

    def delete_reference_file(self, ref_file: RemoteReferenceFile) -> None:
        self.deleted_ref_ids.append(ref_file.file_id)
        for gid in list(self.ref_files.keys()):
            self.ref_files[gid] = [
                (rf, p) for rf, p in self.ref_files[gid] if rf.file_id != ref_file.file_id
            ]

    def upsert_face_matches(self, matches: list[RemoteFaceMatch]) -> int:
        pushed = 0
        for m in matches:
            key = (m.photo_id, m.guest_id)
            if key in self.matches:
                existing = self.matches[key]
                if m.status == "REVOKED" and existing.status != "REVOKED":
                    self.matches[key] = RemoteFaceMatch(
                        photo_id=existing.photo_id,
                        guest_id=existing.guest_id,
                        confidence=existing.confidence,
                        status="REVOKED",
                        model_version=existing.model_version,
                    )
                    pushed += 1
                elif existing.status in PROTECTED_REVIEW_STATUSES:
                    continue
                continue
            self.matches[key] = m
            pushed += 1
        return pushed

    def update_face_profile_status(
        self, guest_id: str, status: str, processed_at: str
    ) -> None:
        old = self.profiles[guest_id]
        self.profiles[guest_id] = RemoteFaceProfile(
            guest_id=old.guest_id,
            display_name=old.display_name,
            status=status,
            consent=old.consent,
            submitted_at=old.submitted_at,
            processed_at=processed_at,
            profile_version=old.profile_version,
        )

    def upsert_photo_processing(self, records: list[PhotoProcessingRecord]) -> None:
        self.processing_log.extend(records)


def test_incremental_local_index_and_match(tmp_path: Path) -> None:
    emb_navneet = _unit_vec(1)
    emb_other = _unit_vec(2)
    stub = _StubEngine({50: [emb_navneet], 150: [emb_other]})
    engine = cast(FaceRecognitionEngine, stub)

    refs_dir = tmp_path / "refs"
    _make_dummy_jpeg(refs_dir / "navneet" / "ref1.jpg", (50, 10, 10))

    album_dir = tmp_path / "album"
    _make_dummy_jpeg(album_dir / "p1.jpg", (50, 20, 20))
    _make_dummy_jpeg(album_dir / "p2.jpg", (150, 20, 20))

    store = SQLiteFaceStore(tmp_path / "indexer.db")

    # First index pass
    idx1 = index_album_folder(album_dir, store, engine)
    assert idx1.scanned == 2
    assert idx1.newly_indexed == 2
    assert idx1.skipped_cached == 0
    assert stub.calls == 2

    # Second index pass must skip both cached photos
    idx2 = index_album_folder(album_dir, store, engine)
    assert idx2.newly_indexed == 0
    assert idx2.skipped_cached == 2
    assert stub.calls == 2

    # Register guest and match
    regs = register_references_root(refs_dir, store, engine)
    assert len(regs) == 1
    assert regs[0].accepted == 1

    match_res = run_matching(store, threshold=0.50)
    assert match_res.photos_checked == 2
    assert match_res.guests_checked == 1
    assert match_res.new_suggestions == 1
    assert match_res.total_pending == 1

    pending = store.get_pending_suggestions("navneet")
    assert len(pending) == 1
    assert pending[0].photo_id == "p1.jpg"


def test_full_sync_cycle_review_preservation_and_withdrawal(tmp_path: Path) -> None:
    emb_ruby = _unit_vec(10)
    emb_stranger = _unit_vec(20)
    stub = _StubEngine({100: [emb_ruby], 200: [emb_stranger]})
    engine = cast(FaceRecognitionEngine, stub)

    fixtures = tmp_path / "fixtures"
    ref_img = _make_dummy_jpeg(fixtures / "ruby_ref.jpg", (100, 10, 10))
    p1_img = _make_dummy_jpeg(fixtures / "p1.jpg", (100, 50, 50))
    p2_img = _make_dummy_jpeg(fixtures / "p2.jpg", (200, 50, 50))

    backend = _FakeGalleryBackend(fixtures)
    backend.profiles["ruby"] = RemoteFaceProfile(
        guest_id="ruby",
        display_name="Ruby",
        status="PENDING_PROCESSING",
        consent="GRANTED",
    )
    backend.ref_files["ruby"] = [
        (RemoteReferenceFile(file_id="drv_ref_1", filename="ruby_ref.jpg"), ref_img)
    ]
    backend.photos = [
        RemotePhoto(
            photo_id="PHOTO_001",
            drive_file_id="drv_p1",
            original_filename="p1.jpg",
            file_hash=compute_file_sha256(p1_img),
            status="ACTIVE",
        ),
        RemotePhoto(
            photo_id="PHOTO_002",
            drive_file_id="drv_p2",
            original_filename="p2.jpg",
            file_hash=compute_file_sha256(p2_img),
            status="ACTIVE",
        ),
    ]
    backend.photo_files = {"PHOTO_001": p1_img, "PHOTO_002": p2_img}

    store = SQLiteFaceStore(tmp_path / "sync.db")

    # Sync #1: registers Ruby, deletes raw Drive ref, indexes 2 photos, pushes 1 PENDING match
    report1 = run_sync(
        backend, store, engine, threshold=0.50, delete_refs_after_processing=True
    )
    assert backend.sheets_ensured
    assert report1.profiles_processed == 1
    assert report1.photos_newly_indexed == 2
    assert report1.new_suggestions_created == 1
    assert report1.suggestions_pushed == 1
    assert backend.profiles["ruby"].status == "ACTIVE"
    assert backend.deleted_ref_ids == ["drv_ref_1"]
    assert ("PHOTO_001", "ruby") in backend.matches
    assert backend.matches[("PHOTO_001", "ruby")].status == "PENDING"
    assert len(backend.processing_log) == 2

    # Guest confirms PHOTO_001 on the website (Sheets)
    backend.matches[("PHOTO_001", "ruby")] = RemoteFaceMatch(
        photo_id="PHOTO_001",
        guest_id="ruby",
        confidence=0.99,
        status="CONFIRMED",
        reviewed_by="ruby",
    )

    # Sync #2: pulls CONFIRMED review, skips cached photos, never overwrites CONFIRMED
    report2 = run_sync(backend, store, engine, threshold=0.50)
    assert report2.reviews_pulled == 1
    assert report2.photos_skipped_cached == 2
    assert report2.photos_newly_indexed == 0
    assert report2.suggestions_pushed == 0
    assert backend.matches[("PHOTO_001", "ruby")].status == "CONFIRMED"
    assert store.get_stats()["suggestions_confirmed"] == 1

    # Sync #3: Guest withdraws consent -> local embeddings deleted + remote marked REVOKED
    backend.profiles["ruby"] = RemoteFaceProfile(
        guest_id="ruby",
        display_name="Ruby",
        status="WITHDRAWN",
        consent="REVOKED",
    )
    report3 = run_sync(backend, store, engine, threshold=0.50)
    assert report3.profiles_revoked == 1
    assert backend.profiles["ruby"].status == "REVOKED"
    assert backend.matches[("PHOTO_001", "ruby")].status == "REVOKED"
    assert len(store.get_guest_profiles()) == 0
