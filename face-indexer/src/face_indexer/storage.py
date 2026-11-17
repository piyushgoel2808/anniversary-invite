"""storage.py — SQLite storage layer for local face embeddings and indexer state.

Stores photo metadata, detected face bounding boxes, 128-float SFace embeddings (as 512-byte blobs),
guest reference profiles, and photo-to-guest suggestions.

All embeddings are kept strictly locally on the Mac in SQLite and never transmitted over the wire.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
from cryptography.fernet import Fernet
from numpy.typing import NDArray

from face_indexer.matcher import GuestProfile, MatchSuggestion
from face_indexer.models import COMBINED_MODEL_VERSION
from face_indexer.recognizer import PhotoAnalysisResult, ResolutionSource

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS photos (
    photo_id TEXT PRIMARY KEY,
    file_hash TEXT NOT NULL,
    filename TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    analyzed_width INTEGER NOT NULL,
    analyzed_height INTEGER NOT NULL,
    resolution_source TEXT NOT NULL,
    face_count INTEGER NOT NULL,
    used_retry INTEGER NOT NULL,
    detect_ms REAL NOT NULL,
    embed_ms REAL NOT NULL,
    model_version TEXT NOT NULL,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id TEXT NOT NULL,
    face_index INTEGER NOT NULL,
    bbox_x REAL NOT NULL,
    bbox_y REAL NOT NULL,
    bbox_w REAL NOT NULL,
    bbox_h REAL NOT NULL,
    detection_score REAL NOT NULL,
    resolution_source TEXT NOT NULL DEFAULT 'medium',
    embedding BLOB NOT NULL,
    FOREIGN KEY (photo_id) REFERENCES photos(photo_id) ON DELETE CASCADE,
    UNIQUE(photo_id, face_index)
);

CREATE INDEX IF NOT EXISTS idx_faces_photo_id ON faces(photo_id);

CREATE TABLE IF NOT EXISTS guests (
    guest_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    profile_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guest_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guest_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    detection_score REAL NOT NULL,
    embedding BLOB NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (guest_id) REFERENCES guests(guest_id) ON DELETE CASCADE,
    UNIQUE(guest_id, file_hash)
);

CREATE INDEX IF NOT EXISTS idx_guest_refs_guest_id ON guest_references(guest_id);

CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id TEXT NOT NULL,
    guest_id TEXT NOT NULL,
    face_index INTEGER NOT NULL,
    confidence REAL NOT NULL,
    centroid_cosine REAL NOT NULL,
    l2_distance REAL NOT NULL,
    model_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    sync_status TEXT NOT NULL DEFAULT 'LOCAL',
    created_at TEXT NOT NULL,
    synced_at TEXT,
    FOREIGN KEY (photo_id) REFERENCES photos(photo_id) ON DELETE CASCADE,
    FOREIGN KEY (guest_id) REFERENCES guests(guest_id) ON DELETE CASCADE,
    UNIQUE(photo_id, guest_id)
);

CREATE INDEX IF NOT EXISTS idx_suggestions_guest ON suggestions(guest_id, status);
CREATE INDEX IF NOT EXISTS idx_suggestions_photo ON suggestions(photo_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_sync ON suggestions(sync_status);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class FaceRecord:
    id: int
    photo_id: str
    face_index: int
    bbox: tuple[float, float, float, float]
    detection_score: float
    resolution_source: ResolutionSource
    embedding: NDArray[np.float32]


@dataclass(frozen=True)
class SuggestionRecord:
    id: int
    photo_id: str
    guest_id: str
    face_index: int
    confidence: float
    centroid_cosine: float
    l2_distance: float
    model_version: str
    status: str
    sync_status: str
    created_at: str
    synced_at: str | None


def load_or_create_encryption_key(key_path: Path | str) -> bytes:
    """Load a local Fernet encryption key, creating one with 0600 permissions if absent."""
    path = Path(key_path)
    if path.is_file():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


class SQLiteFaceStore:
    """Manages SQLite database for local face indexer state."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        encryption_key: bytes | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet: Fernet | None = Fernet(encryption_key) if encryption_key else None
        self._init_db()

    def _encode_embedding(self, embedding: NDArray[np.float32]) -> bytes:
        raw = embedding.astype(np.float32).tobytes()
        if self._fernet is not None:
            return self._fernet.encrypt(raw)
        return raw

    def _decode_embedding(self, blob: bytes) -> NDArray[np.float32]:
        raw = self._fernet.decrypt(blob) if self._fernet is not None else blob
        return np.frombuffer(raw, dtype=np.float32).copy()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(SCHEMA_SQL)

    # -------------------------------------------------------------------------
    # Photo & Face operations
    # -------------------------------------------------------------------------

    def is_photo_indexed(
        self,
        photo_id: str,
        file_hash: str,
        model_version: str = COMBINED_MODEL_VERSION,
    ) -> bool:
        """Check if a photo with the given ID, file hash, and model version is already indexed."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM photos WHERE photo_id = ? AND file_hash = ? AND model_version = ?",
                (photo_id, file_hash, model_version),
            )
            return cur.fetchone() is not None

    def save_photo_analysis(
        self,
        photo_id: str,
        file_hash: str,
        filename: str,
        original_width: int,
        original_height: int,
        analysis: PhotoAnalysisResult,
    ) -> None:
        """Insert or replace photo metadata and detected face embeddings."""
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self._get_connection() as conn:
            # Upsert photo metadata without cascading delete of existing reviewed suggestions
            conn.execute(
                """
                INSERT INTO photos (
                    photo_id, file_hash, filename, width, height,
                    analyzed_width, analyzed_height, resolution_source,
                    face_count, used_retry, detect_ms, embed_ms, model_version, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(photo_id) DO UPDATE SET
                    file_hash=excluded.file_hash,
                    filename=excluded.filename,
                    width=excluded.width,
                    height=excluded.height,
                    analyzed_width=excluded.analyzed_width,
                    analyzed_height=excluded.analyzed_height,
                    resolution_source=excluded.resolution_source,
                    face_count=excluded.face_count,
                    used_retry=excluded.used_retry,
                    detect_ms=excluded.detect_ms,
                    embed_ms=excluded.embed_ms,
                    model_version=excluded.model_version,
                    processed_at=excluded.processed_at
                """,
                (
                    photo_id,
                    file_hash,
                    filename,
                    original_width,
                    original_height,
                    analysis.analyzed_size[0],
                    analysis.analyzed_size[1],
                    analysis.resolution_source,
                    len(analysis.faces),
                    1 if analysis.used_original_retry else 0,
                    analysis.detect_ms,
                    analysis.embed_ms,
                    analysis.model_version,
                    now,
                ),
            )
            # Replace face embeddings for this photo
            conn.execute("DELETE FROM faces WHERE photo_id = ?", (photo_id,))
            for idx, face in enumerate(analysis.faces):
                if face.embedding is None:
                    continue
                embedding_blob = self._encode_embedding(face.embedding)
                conn.execute(
                    """
                    INSERT INTO faces (
                        photo_id, face_index, bbox_x, bbox_y, bbox_w, bbox_h,
                        detection_score, resolution_source, embedding
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        photo_id,
                        idx,
                        face.bbox[0],
                        face.bbox[1],
                        face.bbox[2],
                        face.bbox[3],
                        face.detection_score,
                        face.resolution_source,
                        embedding_blob,
                    ),
                )

    def get_all_faces(self) -> list[FaceRecord]:
        """Retrieve all detected faces across all indexed photos."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT id, photo_id, face_index, bbox_x, bbox_y, bbox_w, bbox_h, "
                "detection_score, resolution_source, embedding "
                "FROM faces ORDER BY photo_id, face_index"
            )
            records: list[FaceRecord] = []
            for row in cur.fetchall():
                emb = self._decode_embedding(row["embedding"])
                src = cast(Literal["medium", "original"], row["resolution_source"])
                records.append(
                    FaceRecord(
                        id=row["id"],
                        photo_id=row["photo_id"],
                        face_index=row["face_index"],
                        bbox=(row["bbox_x"], row["bbox_y"], row["bbox_w"], row["bbox_h"]),
                        detection_score=row["detection_score"],
                        resolution_source=src,
                        embedding=emb,
                    )
                )
            return records

    def get_photo_count(self) -> int:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM photos")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def get_face_count(self) -> int:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM faces")
            row = cur.fetchone()
            return int(row[0]) if row else 0

    # -------------------------------------------------------------------------
    # Guest Profile & Reference operations
    # -------------------------------------------------------------------------

    def save_guest(
        self,
        guest_id: str,
        display_name: str,
        profile_version: int = 1,
        status: str = "ACTIVE",
    ) -> None:
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO guests (guest_id, display_name, profile_version, status, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guest_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    profile_version=excluded.profile_version,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (guest_id, display_name, profile_version, status, now),
            )

    def revoke_guest(self, guest_id: str) -> None:
        """Consent withdrawal: delete stored reference embeddings and mark guest REVOKED."""
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self._get_connection() as conn:
            conn.execute("DELETE FROM guest_references WHERE guest_id = ?", (guest_id,))
            conn.execute(
                "UPDATE guests SET status = 'REVOKED', updated_at = ? WHERE guest_id = ?",
                (now, guest_id),
            )
            conn.execute(
                "UPDATE suggestions SET status = 'REVOKED', sync_status = 'LOCAL' "
                "WHERE guest_id = ? AND status != 'REVOKED'",
                (guest_id,),
            )

    def has_guest_reference(self, guest_id: str, file_hash: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM guest_references WHERE guest_id = ? AND file_hash = ?",
                (guest_id, file_hash),
            )
            return cur.fetchone() is not None

    def save_guest_reference(
        self,
        guest_id: str,
        filename: str,
        file_hash: str,
        detection_score: float,
        embedding: NDArray[np.float32],
        accepted: bool = True,
    ) -> None:
        embedding_blob = self._encode_embedding(embedding)
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO guest_references (
                    guest_id, filename, file_hash, detection_score, embedding, accepted
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guest_id, file_hash) DO UPDATE SET
                    filename=excluded.filename,
                    detection_score=excluded.detection_score,
                    embedding=excluded.embedding,
                    accepted=excluded.accepted
                """,
                (
                    guest_id,
                    filename,
                    file_hash,
                    detection_score,
                    embedding_blob,
                    1 if accepted else 0,
                ),
            )

    def get_guest_profiles(self) -> list[GuestProfile]:
        """Construct GuestProfile objects from stored active references."""
        with self._get_connection() as conn:
            cur_guests = conn.execute(
                "SELECT guest_id, display_name FROM guests WHERE status = 'ACTIVE'"
            )
            guest_rows = cur_guests.fetchall()
            profiles: list[GuestProfile] = []

            for g in guest_rows:
                gid = g["guest_id"]
                name = g["display_name"]
                cur_refs = conn.execute(
                    "SELECT filename, embedding FROM guest_references "
                    "WHERE guest_id = ? AND accepted = 1 ORDER BY id",
                    (gid,),
                )
                ref_rows = cur_refs.fetchall()
                embeddings: list[NDArray[np.float32]] = []
                files: list[str] = []
                for r in ref_rows:
                    emb = self._decode_embedding(r["embedding"])
                    embeddings.append(emb)
                    files.append(r["filename"])

                if not embeddings:
                    continue

                emb_array = np.vstack(embeddings)
                centroid = np.mean(emb_array, axis=0, dtype=np.float32)
                norm = float(np.linalg.norm(centroid))
                if norm > 0:
                    centroid = centroid / norm

                intra_cosines: list[float] = []
                for i in range(len(embeddings)):
                    for j in range(i + 1, len(embeddings)):
                        c = float(np.dot(embeddings[i], embeddings[j]))
                        intra_cosines.append(c)
                mean_intra = float(np.mean(intra_cosines)) if intra_cosines else 1.0

                profiles.append(
                    GuestProfile(
                        guest_id=gid,
                        display_name=name,
                        reference_embeddings=tuple(embeddings),
                        centroid_embedding=centroid,
                        accepted_files=tuple(files),
                        mean_intra_cosine=mean_intra,
                        warnings=(),
                    )
                )
            return profiles

    # -------------------------------------------------------------------------
    # Suggestions / Matches operations
    # -------------------------------------------------------------------------

    def save_suggestions(self, suggestions: list[MatchSuggestion]) -> int:
        """Insert suggestions, ignoring existing (photo_id, guest_id) pairs.

        Using INSERT OR IGNORE preserves any guest review decisions (CONFIRMED/REJECTED).
        Returns count of newly inserted rows.
        """
        now = datetime.datetime.now(datetime.UTC).isoformat()
        inserted = 0
        with self._get_connection() as conn:
            for s in suggestions:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO suggestions (
                        photo_id, guest_id, face_index, confidence,
                        centroid_cosine, l2_distance, model_version,
                        status, sync_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'LOCAL', ?)
                    """,
                    (
                        s.photo_id,
                        s.guest_id,
                        s.face_index,
                        s.confidence,
                        s.centroid_cosine,
                        s.l2_distance,
                        s.model_version,
                        s.status,
                        now,
                    ),
                )
                if cur.rowcount > 0:
                    inserted += cur.rowcount
        return inserted

    def get_pending_suggestions(self, guest_id: str | None = None) -> list[SuggestionRecord]:
        """Fetch all PENDING suggestions, optionally filtered by guest_id."""
        query = (
            "SELECT id, photo_id, guest_id, face_index, confidence, "
            "centroid_cosine, l2_distance, model_version, status, sync_status, "
            "created_at, synced_at "
            "FROM suggestions WHERE status = 'PENDING'"
        )
        params: tuple[Any, ...] = ()
        if guest_id:
            query += " AND guest_id = ?"
            params = (guest_id,)
        query += " ORDER BY confidence DESC"

        with self._get_connection() as conn:
            cur = conn.execute(query, params)
            return [self._row_to_suggestion(row) for row in cur.fetchall()]

    def get_unsynced_suggestions(self) -> list[SuggestionRecord]:
        """Fetch suggestions that haven't been pushed to Sheets yet."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT id, photo_id, guest_id, face_index, confidence, "
                "centroid_cosine, l2_distance, model_version, status, sync_status, "
                "created_at, synced_at "
                "FROM suggestions WHERE sync_status = 'LOCAL' ORDER BY id"
            )
            return [self._row_to_suggestion(row) for row in cur.fetchall()]

    @staticmethod
    def _row_to_suggestion(row: sqlite3.Row) -> SuggestionRecord:
        return SuggestionRecord(
            id=row["id"],
            photo_id=row["photo_id"],
            guest_id=row["guest_id"],
            face_index=row["face_index"],
            confidence=row["confidence"],
            centroid_cosine=row["centroid_cosine"],
            l2_distance=row["l2_distance"],
            model_version=row["model_version"],
            status=row["status"],
            sync_status=row["sync_status"],
            created_at=row["created_at"],
            synced_at=row["synced_at"],
        )

    def mark_suggestions_synced(self, suggestion_ids: list[int]) -> None:
        """Mark specific suggestions as SYNCED with timestamp."""
        if not suggestion_ids:
            return
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self._get_connection() as conn:
            placeholders = ",".join("?" for _ in suggestion_ids)
            conn.execute(
                f"UPDATE suggestions SET sync_status = 'SYNCED', synced_at = ? "
                f"WHERE id IN ({placeholders})",
                (now, *suggestion_ids),
            )

    def update_suggestion_status(
        self, photo_id: str, guest_id: str, new_status: str
    ) -> None:
        """Update suggestion status (e.g. from Sheets review: CONFIRMED or REJECTED)."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE suggestions SET status = ?, sync_status = 'SYNCED' "
                "WHERE photo_id = ? AND guest_id = ?",
                (new_status, photo_id, guest_id),
            )

    # -------------------------------------------------------------------------
    # Sync State / Key-Value Store
    # -------------------------------------------------------------------------

    def set_sync_state(self, key: str, value: str) -> None:
        now = datetime.datetime.now(datetime.UTC).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, now),
            )

    def get_sync_state(self, key: str) -> str | None:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,))
            row = cur.fetchone()
            return str(row[0]) if row else None

    # -------------------------------------------------------------------------
    # Overall Telemetry & Stats
    # -------------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        with self._get_connection() as conn:
            p_count = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            f_count = conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
            g_count = conn.execute(
                "SELECT COUNT(*) FROM guests WHERE status = 'ACTIVE'"
            ).fetchone()[0]
            r_count = conn.execute("SELECT COUNT(*) FROM guest_references").fetchone()[0]
            s_count = conn.execute("SELECT COUNT(*) FROM suggestions").fetchone()[0]
            s_pending = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE status = 'PENDING'"
            ).fetchone()[0]
            s_confirmed = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE status = 'CONFIRMED'"
            ).fetchone()[0]
            s_rejected = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE status = 'REJECTED'"
            ).fetchone()[0]
            s_unsynced = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE sync_status = 'LOCAL'"
            ).fetchone()[0]
            s_synced = conn.execute(
                "SELECT COUNT(*) FROM suggestions WHERE sync_status = 'SYNCED'"
            ).fetchone()[0]

            return {
                "photos": p_count,
                "faces": f_count,
                "active_guests": g_count,
                "references": r_count,
                "suggestions_total": s_count,
                "suggestions_pending": s_pending,
                "suggestions_confirmed": s_confirmed,
                "suggestions_rejected": s_rejected,
                "suggestions_unsynced": s_unsynced,
                "suggestions_synced": s_synced,
            }
