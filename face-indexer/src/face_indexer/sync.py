"""sync.py — Google Drive & Google Sheets incremental sync engine for local Mac indexer.

Architecture invariants:
- Face detection, SFace embedding extraction, and matching run 100% locally on the Mac.
- Face embeddings NEVER leave the Mac (stored only in local encrypted SQLite).
- Only metadata (photo_id, guest_id, confidence, status, model_version) is written to Sheets.
- All newly generated matches start as PENDING; guest review decisions (CONFIRMED / REJECTED)
  are never overwritten by the Mac indexer.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests

from face_indexer.imaging import load_image_bgr
from face_indexer.indexer import (
    CALIBRATED_COSINE_THRESHOLD,
    compute_file_sha256,
    register_guest_folder,
    run_matching,
)
from face_indexer.matcher import ScoringStrategy
from face_indexer.models import COMBINED_MODEL_VERSION
from face_indexer.recognizer import FaceRecognitionEngine
from face_indexer.storage import SQLiteFaceStore

DEFAULT_APPS_SCRIPT_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbzwdnvh5-p_inJpdrlkHzm6QL_JRoBnXZTzJuaJdSLdLkrdUvoeO4hSBe61EhPo938m/exec"
)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

SHEET_NAMES = {
    "GUESTS": "GUEST_DATABASE",
    "PHOTOS": "PHOTO_DATABASE",
    "FACE_PROFILES": "FACE_PROFILES",
    "FACE_MATCHES": "FACE_MATCHES",
    "FACE_REVIEW_LOG": "FACE_REVIEW_LOG",
    "PHOTO_PROCESSING": "PHOTO_PROCESSING",
}

SHEET_HEADERS: dict[str, list[str]] = {
    "FACE_PROFILES": [
        "guest_id",
        "status",
        "consent",
        "submitted_at",
        "processed_at",
        "profile_version",
    ],
    "FACE_MATCHES": [
        "photo_id",
        "guest_id",
        "confidence",
        "status",
        "model_version",
        "reviewed_at",
        "reviewed_by",
    ],
    "FACE_REVIEW_LOG": [
        "event_id",
        "photo_id",
        "guest_id",
        "action",
        "timestamp",
    ],
    "PHOTO_PROCESSING": [
        "photo_id",
        "processing_status",
        "processed_at",
        "model_version",
        "error",
    ],
}

PROTECTED_REVIEW_STATUSES: frozenset[str] = frozenset(
    {"CONFIRMED", "REJECTED", "ADMIN_CONFIRMED"}
)


@dataclass(frozen=True)
class RemotePhoto:
    photo_id: str
    drive_file_id: str
    original_filename: str
    file_hash: str
    status: str
    medium_image_id: str = ""


@dataclass(frozen=True)
class RemoteFaceProfile:
    guest_id: str
    display_name: str
    status: str
    consent: str
    submitted_at: str = ""
    processed_at: str = ""
    profile_version: int = 1


@dataclass(frozen=True)
class RemoteFaceMatch:
    photo_id: str
    guest_id: str
    confidence: float
    status: str
    model_version: str = COMBINED_MODEL_VERSION
    reviewed_at: str = ""
    reviewed_by: str = ""


@dataclass(frozen=True)
class RemoteReferenceFile:
    file_id: str
    filename: str
    mime_type: str = "image/jpeg"
    guest_id: str = ""


@dataclass(frozen=True)
class PhotoProcessingRecord:
    photo_id: str
    processing_status: str
    processed_at: str
    model_version: str = COMBINED_MODEL_VERSION
    error: str = ""


@dataclass(frozen=True)
class SyncReport:
    reviews_pulled: int = 0
    profiles_processed: int = 0
    profiles_revoked: int = 0
    photos_skipped_cached: int = 0
    photos_newly_indexed: int = 0
    photos_failed: int = 0
    new_suggestions_created: int = 0
    suggestions_pushed: int = 0
    elapsed_ms: float = 0.0
    errors: tuple[str, ...] = field(default_factory=tuple)


class GalleryBackend(Protocol):
    """Abstract interface for remote gallery metadata (Sheets) and binary storage (Drive)."""

    def ensure_face_sheets(self) -> None: ...

    def list_active_photos(self) -> list[RemotePhoto]: ...

    def list_face_profiles(self) -> list[RemoteFaceProfile]: ...

    def list_face_matches(self) -> list[RemoteFaceMatch]: ...

    def download_photo(self, remote_photo: RemotePhoto, dest_path: Path) -> Path: ...

    def list_guest_reference_files(self, guest_id: str) -> list[RemoteReferenceFile]: ...

    def download_reference_file(self, ref_file: RemoteReferenceFile, dest_path: Path) -> Path: ...

    def delete_reference_file(self, ref_file: RemoteReferenceFile) -> None: ...

    def upsert_face_matches(self, matches: list[RemoteFaceMatch]) -> int: ...

    def update_face_profile_status(
        self, guest_id: str, status: str, processed_at: str
    ) -> None: ...

    def upsert_photo_processing(self, records: list[PhotoProcessingRecord]) -> None: ...


def _decode_data_url_to_file(data_url: str, dest_path: Path) -> Path:
    """Decode a Base64 data URL (or raw Base64) and write bytes to dest_path."""
    payload = data_url.strip()
    if "," in payload and "base64" in payload.split(",", 1)[0].lower():
        payload = payload.split(",", 1)[1].strip()
    raw_bytes = base64.b64decode(payload)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(raw_bytes)
    return dest_path


class AppsScriptBackend:
    """Zero-Google-Cloud backend syncing directly with the Apps Script Web App.

    Uses the family's existing Admin login (adminLogin -> signed 4-hour token -> adminLogout).
    Requires ZERO Google Cloud Console project, ZERO OAuth client JSON, and ₹0 forever.
    """

    def __init__(
        self,
        *,
        apps_script_url: str,
        admin_username: str,
        admin_password: str,
        photo_variant: str = "original",
        timeout_seconds: int = 120,
        session: requests.Session | None = None,
    ) -> None:
        self.apps_script_url = apps_script_url
        self.photo_variant = photo_variant
        self.timeout_seconds = timeout_seconds
        self._http = session or requests.Session()
        self._state_cache: dict[str, Any] | None = None
        self._token = self._login(admin_username, admin_password)

    def __enter__(self) -> AppsScriptBackend:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._token:
            with contextlib.suppress(Exception):
                self._call("adminLogout", [self._token])
            self._token = ""

    def _call(self, action: str, args: list[Any]) -> dict[str, Any]:
        resp = self._http.post(
            self.apps_script_url,
            data=json.dumps({"action": action, "args": args}),
            headers={"Content-Type": "text/plain;charset=utf-8"},
            timeout=self.timeout_seconds,
            allow_redirects=True,
        )
        resp.raise_for_status()
        data: Any = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected response from Apps Script action '{action}'.")
        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or f"Apps Script '{action}' failed."))
        return data

    def _login(self, username: str, password: str) -> str:
        res = self._call("adminLogin", [username, password])
        token = str(res.get("token") or "")
        if not token:
            raise RuntimeError("Admin login succeeded but returned no session token.")
        return token

    def _get_state(self, *, force_refresh: bool = False) -> dict[str, Any]:
        if self._state_cache is None or force_refresh:
            self._state_cache = self._call("adminSyncGetState", [self._token])
        return self._state_cache

    def ensure_face_sheets(self) -> None:
        # adminSyncGetState ensures all 4 face sheets exist and fetches current state in 1 call
        self._get_state(force_refresh=True)

    def list_active_photos(self) -> list[RemotePhoto]:
        state = self._get_state()
        return [
            RemotePhoto(
                photo_id=str(p["photo_id"]),
                drive_file_id=str(p.get("drive_file_id", p["photo_id"])),
                medium_image_id=str(p.get("medium_image_id", "")),
                original_filename=str(p.get("original_filename", f"{p['photo_id']}.jpg")),
                file_hash=str(p.get("file_hash", "")),
                status=str(p.get("status", "ACTIVE")),
            )
            for p in state.get("photos", [])
        ]

    def list_face_profiles(self) -> list[RemoteFaceProfile]:
        state = self._get_state()
        return [
            RemoteFaceProfile(
                guest_id=str(p["guest_id"]),
                display_name=str(p.get("display_name", p["guest_id"])),
                status=str(p.get("status", "PENDING_PROCESSING")).upper(),
                consent=str(p.get("consent", "")).upper(),
                submitted_at=str(p.get("submitted_at", "")),
                processed_at=str(p.get("processed_at", "")),
                profile_version=int(p.get("profile_version", 1) or 1),
            )
            for p in state.get("profiles", [])
        ]

    def list_face_matches(self) -> list[RemoteFaceMatch]:
        state = self._get_state()
        return [
            RemoteFaceMatch(
                photo_id=str(m["photo_id"]),
                guest_id=str(m["guest_id"]),
                confidence=float(m.get("confidence", 0.0) or 0.0),
                status=str(m.get("status", "PENDING")).upper(),
                model_version=str(m.get("model_version", COMBINED_MODEL_VERSION)),
                reviewed_at=str(m.get("reviewed_at", "")),
                reviewed_by=str(m.get("reviewed_by", "")),
            )
            for m in state.get("matches", [])
        ]

    def download_photo(self, remote_photo: RemotePhoto, dest_path: Path) -> Path:
        res = self._call(
            "adminSyncDownloadPhoto",
            [remote_photo.photo_id, self._token, self.photo_variant],
        )
        return _decode_data_url_to_file(str(res["dataUrl"]), dest_path)

    def list_guest_reference_files(self, guest_id: str) -> list[RemoteReferenceFile]:
        res = self._call("adminSyncListGuestRefs", [guest_id, self._token])
        return [
            RemoteReferenceFile(
                file_id=str(f["file_id"]),
                filename=str(f["filename"]),
                mime_type=str(f.get("mime_type", "image/jpeg")),
                guest_id=guest_id,
            )
            for f in res.get("files", [])
        ]

    def download_reference_file(self, ref_file: RemoteReferenceFile, dest_path: Path) -> Path:
        res = self._call(
            "adminSyncDownloadRef",
            [ref_file.guest_id, ref_file.file_id, self._token],
        )
        return _decode_data_url_to_file(str(res["dataUrl"]), dest_path)

    def delete_reference_file(self, ref_file: RemoteReferenceFile) -> None:
        self._call("adminSyncDeleteRef", [ref_file.guest_id, ref_file.file_id, self._token])

    def upsert_face_matches(self, matches: list[RemoteFaceMatch]) -> int:
        if not matches:
            return 0
        payload = [
            {
                "photo_id": m.photo_id,
                "guest_id": m.guest_id,
                "confidence": round(m.confidence, 4),
                "status": m.status,
                "model_version": m.model_version,
                "reviewed_at": m.reviewed_at,
                "reviewed_by": m.reviewed_by,
            }
            for m in matches
        ]
        res = self._call("adminSyncPushMatches", [payload, self._token])
        return int(res.get("pushed", 0))

    def update_face_profile_status(
        self, guest_id: str, status: str, processed_at: str
    ) -> None:
        self._call(
            "adminSyncUpdateProfileStatus",
            [guest_id, status, processed_at, self._token],
        )

    def upsert_photo_processing(self, records: list[PhotoProcessingRecord]) -> None:
        if not records:
            return
        payload = [
            {
                "photo_id": r.photo_id,
                "processing_status": r.processing_status,
                "processed_at": r.processed_at,
                "model_version": r.model_version,
                "error": r.error,
            }
            for r in records
        ]
        self._call("adminSyncLogPhotoProcessing", [payload, self._token])


class GoogleWorkspaceBackend:
    """Live Google Drive v3 + Google Sheets v4 backend using OAuth Desktop or Service Account."""

    def __init__(
        self,
        *,
        spreadsheet_id: str,
        credentials_path: Path,
        token_path: Path,
        references_folder_id: str | None = None,
        root_folder_name: str = "ANNIVERSARY_GALLERY",
        references_folder_name: str = "FACE_REFERENCES",
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self._references_folder_id = references_folder_id
        self._root_folder_name = root_folder_name
        self._references_folder_name = references_folder_name

        creds = self._authenticate()
        from googleapiclient.discovery import build

        self._sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _authenticate(self) -> Any:
        if not self.credentials_path.is_file():
            raise FileNotFoundError(
                f"Google credentials JSON not found at '{self.credentials_path}'. "
                "Download Desktop OAuth client secrets or Service Account JSON from "
                "Google Cloud Console."
            )

        raw = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        if raw.get("type") == "service_account":
            from google.oauth2 import service_account

            return service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                str(self.credentials_path), scopes=GOOGLE_SCOPES
            )

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds: Any = None
        if self.token_path.is_file():
            creds = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
                str(self.token_path), GOOGLE_SCOPES
            )

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), GOOGLE_SCOPES
                )
                creds = flow.run_local_server(port=0)

            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, creds.to_json().encode("utf-8"))
            finally:
                os.close(fd)

        return creds

    def _read_sheet_rows(self, sheet_name: str) -> list[dict[str, str]]:
        resp = (
            self._sheets.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"{sheet_name}!A1:Z")
            .execute()
        )
        values: list[list[str]] = resp.get("values", [])
        if len(values) < 2:
            return []
        headers = [h.strip() for h in values[0]]
        rows: list[dict[str, str]] = []
        for raw_row in values[1:]:
            row_dict = {
                headers[i]: (raw_row[i].strip() if i < len(raw_row) else "")
                for i in range(len(headers))
            }
            rows.append(row_dict)
        return rows

    def ensure_face_sheets(self) -> None:
        meta = (
            self._sheets.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
        existing_titles = {s["properties"]["title"] for s in meta.get("sheets", [])}

        add_requests: list[dict[str, Any]] = []
        to_initialize: list[str] = []
        for name in SHEET_HEADERS:
            if name not in existing_titles:
                add_requests.append({"addSheet": {"properties": {"title": name}}})
                to_initialize.append(name)

        if add_requests:
            self._sheets.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"requests": add_requests},
            ).execute()

        for name in to_initialize:
            headers = SHEET_HEADERS[name]
            self._sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{name}!A1",
                valueInputOption="RAW",
                body={"values": [headers]},
            ).execute()

    def list_active_photos(self) -> list[RemotePhoto]:
        rows = self._read_sheet_rows(SHEET_NAMES["PHOTOS"])
        photos: list[RemotePhoto] = []
        for row in rows:
            status = row.get("status", "").upper()
            if status and status != "ACTIVE":
                continue
            photo_id = row.get("photo_id", "")
            drive_file_id = row.get("drive_file_id", "")
            if not photo_id or not drive_file_id:
                continue
            photos.append(
                RemotePhoto(
                    photo_id=photo_id,
                    drive_file_id=drive_file_id,
                    medium_image_id=row.get("medium_image_id", ""),
                    original_filename=row.get("original_filename", f"{photo_id}.jpg"),
                    file_hash=row.get("file_hash", ""),
                    status=status or "ACTIVE",
                )
            )
        return photos

    def list_face_profiles(self) -> list[RemoteFaceProfile]:
        guest_rows = self._read_sheet_rows(SHEET_NAMES["GUESTS"])
        guest_names = {
            r.get("guest_id", ""): r.get("guest_name", r.get("guest_id", ""))
            for r in guest_rows
            if r.get("guest_id")
        }

        profile_rows = self._read_sheet_rows(SHEET_NAMES["FACE_PROFILES"])
        profiles: list[RemoteFaceProfile] = []
        for row in profile_rows:
            gid = row.get("guest_id", "")
            if not gid:
                continue
            try:
                version = int(row.get("profile_version", "1") or "1")
            except ValueError:
                version = 1
            profiles.append(
                RemoteFaceProfile(
                    guest_id=gid,
                    display_name=guest_names.get(gid, gid),
                    status=row.get("status", "PENDING_PROCESSING").upper(),
                    consent=row.get("consent", "").upper(),
                    submitted_at=row.get("submitted_at", ""),
                    processed_at=row.get("processed_at", ""),
                    profile_version=version,
                )
            )
        return profiles

    def list_face_matches(self) -> list[RemoteFaceMatch]:
        rows = self._read_sheet_rows(SHEET_NAMES["FACE_MATCHES"])
        matches: list[RemoteFaceMatch] = []
        for row in rows:
            pid = row.get("photo_id", "")
            gid = row.get("guest_id", "")
            if not pid or not gid:
                continue
            try:
                conf = float(row.get("confidence", "0") or "0")
            except ValueError:
                conf = 0.0
            matches.append(
                RemoteFaceMatch(
                    photo_id=pid,
                    guest_id=gid,
                    confidence=conf,
                    status=row.get("status", "PENDING").upper(),
                    model_version=row.get("model_version", COMBINED_MODEL_VERSION),
                    reviewed_at=row.get("reviewed_at", ""),
                    reviewed_by=row.get("reviewed_by", ""),
                )
            )
        return matches

    def _download_drive_file(self, file_id: str, dest_path: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        request = self._drive.files().get_media(fileId=file_id)
        with dest_path.open("wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return dest_path

    def download_photo(self, remote_photo: RemotePhoto, dest_path: Path) -> Path:
        return self._download_drive_file(remote_photo.drive_file_id, dest_path)

    def _resolve_references_folder_id(self) -> str | None:
        if self._references_folder_id:
            return self._references_folder_id

        root_q = (
            f"name = '{self._root_folder_name}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        root_res = self._drive.files().list(q=root_q, fields="files(id)").execute()
        roots = root_res.get("files", [])
        if not roots:
            return None
        root_id = str(roots[0]["id"])

        ref_q = (
            f"name = '{self._references_folder_name}' and "
            f"'{root_id}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        ref_res = self._drive.files().list(q=ref_q, fields="files(id)").execute()
        refs = ref_res.get("files", [])
        if not refs:
            return None
        self._references_folder_id = str(refs[0]["id"])
        return self._references_folder_id

    def list_guest_reference_files(self, guest_id: str) -> list[RemoteReferenceFile]:
        parent_id = self._resolve_references_folder_id()
        if not parent_id:
            return []

        # Look for guest subfolder FACE_REFERENCES/<guest_id>/
        safe_gid = guest_id.replace("'", "\\'")
        folder_q = (
            f"name = '{safe_gid}' and '{parent_id}' in parents and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        folder_res = self._drive.files().list(q=folder_q, fields="files(id)").execute()
        folders = folder_res.get("files", [])
        if not folders:
            return []

        guest_folder_id = str(folders[0]["id"])
        files_q = f"'{guest_folder_id}' in parents and trashed = false"
        files_res = (
            self._drive.files()
            .list(q=files_q, fields="files(id, name, mimeType)", pageSize=50)
            .execute()
        )
        return [
            RemoteReferenceFile(
                file_id=str(f["id"]),
                filename=str(f["name"]),
                mime_type=str(f.get("mimeType", "image/jpeg")),
            )
            for f in files_res.get("files", [])
        ]

    def download_reference_file(self, ref_file: RemoteReferenceFile, dest_path: Path) -> Path:
        return self._download_drive_file(ref_file.file_id, dest_path)

    def delete_reference_file(self, ref_file: RemoteReferenceFile) -> None:
        self._drive.files().update(fileId=ref_file.file_id, body={"trashed": True}).execute()

    def upsert_face_matches(self, matches: list[RemoteFaceMatch]) -> int:
        if not matches:
            return 0

        sheet = SHEET_NAMES["FACE_MATCHES"]
        resp = (
            self._sheets.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"{sheet}!A1:G")
            .execute()
        )
        values: list[list[str]] = resp.get("values", [])
        existing_map: dict[tuple[str, str], tuple[int, str]] = {}
        for row_idx, raw_row in enumerate(values[1:], start=2):
            if len(raw_row) >= 2:
                pid = raw_row[0].strip()
                gid = raw_row[1].strip()
                status = raw_row[3].strip().upper() if len(raw_row) >= 4 else "PENDING"
                existing_map[(pid, gid)] = (row_idx, status)

        rows_to_append: list[list[Any]] = []
        batch_updates: list[dict[str, Any]] = []
        pushed = 0

        for m in matches:
            key = (m.photo_id, m.guest_id)
            if key in existing_map:
                row_num, remote_status = existing_map[key]
                # Never overwrite a guest review decision unless explicitly revoking consent
                if m.status == "REVOKED" and remote_status != "REVOKED":
                    batch_updates.append(
                        {
                            "range": f"{sheet}!D{row_num}",
                            "values": [["REVOKED"]],
                        }
                    )
                    pushed += 1
                continue

            rows_to_append.append(
                [
                    m.photo_id,
                    m.guest_id,
                    round(m.confidence, 4),
                    m.status,
                    m.model_version,
                    m.reviewed_at,
                    m.reviewed_by,
                ]
            )
            pushed += 1

        if batch_updates:
            self._sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": batch_updates},
            ).execute()

        if rows_to_append:
            self._sheets.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{sheet}!A:G",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows_to_append},
            ).execute()

        return pushed

    def update_face_profile_status(
        self, guest_id: str, status: str, processed_at: str
    ) -> None:
        sheet = SHEET_NAMES["FACE_PROFILES"]
        resp = (
            self._sheets.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"{sheet}!A1:F")
            .execute()
        )
        values: list[list[str]] = resp.get("values", [])
        for row_idx, raw_row in enumerate(values[1:], start=2):
            if raw_row and raw_row[0].strip() == guest_id:
                self._sheets.spreadsheets().values().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={
                        "valueInputOption": "RAW",
                        "data": [
                            {"range": f"{sheet}!B{row_idx}", "values": [[status]]},
                            {"range": f"{sheet}!E{row_idx}", "values": [[processed_at]]},
                        ],
                    },
                ).execute()
                return

    def upsert_photo_processing(self, records: list[PhotoProcessingRecord]) -> None:
        if not records:
            return
        sheet = SHEET_NAMES["PHOTO_PROCESSING"]
        rows = [
            [
                r.photo_id,
                r.processing_status,
                r.processed_at,
                r.model_version,
                r.error,
            ]
            for r in records
        ]
        self._sheets.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"{sheet}!A:E",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()


def run_sync(
    backend: GalleryBackend,
    store: SQLiteFaceStore,
    engine: FaceRecognitionEngine,
    *,
    threshold: float = CALIBRATED_COSINE_THRESHOLD,
    strategy: ScoringStrategy = "max_ref",
    delete_refs_after_processing: bool = False,
    staging_dir: Path | None = None,
    progress_cb: Callable[[str], None] | None = None,
) -> SyncReport:
    """Execute one complete incremental sync cycle between Google Drive/Sheets and local SQLite."""
    t0 = time.perf_counter()
    errors: list[str] = []
    reviews_pulled = 0
    profiles_processed = 0
    profiles_revoked = 0
    photos_skipped = 0
    photos_indexed = 0
    photos_failed = 0

    def _log(msg: str) -> None:
        if progress_cb is not None:
            progress_cb(msg)

    _log("[1/5] Fetching remote gallery state & verifying face sheets...")
    backend.ensure_face_sheets()

    # 1. Pull guest review decisions from FACE_MATCHES into local SQLite
    remote_matches = backend.list_face_matches()
    for rm in remote_matches:
        if rm.status in PROTECTED_REVIEW_STATUSES or rm.status == "REVOKED":
            store.update_suggestion_status(rm.photo_id, rm.guest_id, rm.status)
            reviews_pulled += 1
    _log(f"[1/5] Synced {reviews_pulled} guest review decision(s).")

    with tempfile.TemporaryDirectory(
        prefix="face_indexer_sync_", dir=str(staging_dir) if staging_dir else None
    ) as tmp_root:
        tmp_path = Path(tmp_root)

        # 2. Process FACE_PROFILES (new registrations and consent withdrawals)
        remote_profiles = backend.list_face_profiles()
        _log(f"[2/5] Checking {len(remote_profiles)} guest face profile(s)...")
        for prof in remote_profiles:
            now_iso = datetime.datetime.now(datetime.UTC).isoformat()

            if prof.status == "WITHDRAWN" or prof.consent == "REVOKED":
                _log(f"      Revoking face data for {prof.display_name} ({prof.guest_id})...")
                store.revoke_guest(prof.guest_id)
                try:
                    for ref_file in backend.list_guest_reference_files(prof.guest_id):
                        backend.delete_reference_file(ref_file)
                except Exception as exc:
                    errors.append(f"Cleanup refs for revoked guest {prof.guest_id}: {exc}")
                backend.update_face_profile_status(prof.guest_id, "REVOKED", now_iso)
                profiles_revoked += 1
                continue

            if prof.status == "PENDING_PROCESSING" and prof.consent == "GRANTED":
                _log(f"      Processing reference photos for {prof.display_name}...")
                guest_tmp = tmp_path / "refs" / prof.guest_id
                guest_tmp.mkdir(parents=True, exist_ok=True)
                try:
                    ref_files = backend.list_guest_reference_files(prof.guest_id)
                    for rf in ref_files:
                        backend.download_reference_file(rf, guest_tmp / rf.filename)

                    summary = register_guest_folder(
                        guest_id=prof.guest_id,
                        display_name=prof.display_name,
                        ref_dir=guest_tmp,
                        store=store,
                        engine=engine,
                        profile_version=prof.profile_version,
                    )
                    total_valid = summary.accepted + summary.skipped_cached
                    new_status = "ACTIVE" if total_valid > 0 else "NEEDS_RETRY"
                    backend.update_face_profile_status(prof.guest_id, new_status, now_iso)
                    if new_status == "ACTIVE" and delete_refs_after_processing:
                        for rf in ref_files:
                            backend.delete_reference_file(rf)
                    _log(
                        f"      -> {prof.display_name}: {new_status} "
                        f"({total_valid} valid reference face(s))"
                    )
                    profiles_processed += 1
                except Exception as exc:
                    errors.append(f"Profile {prof.guest_id}: {exc}")

        # 3. Incrementally index newly uploaded active album photos
        remote_photos = backend.list_active_photos()
        _log(f"[3/5] Scanning {len(remote_photos)} active album photo(s)...")
        processing_records: list[PhotoProcessingRecord] = []
        photos_tmp = tmp_path / "photos"
        photos_tmp.mkdir(parents=True, exist_ok=True)

        for idx, rphoto in enumerate(remote_photos, start=1):
            # Fast skip if sheet already supplies matching SHA-256 file_hash
            if rphoto.file_hash and store.is_photo_indexed(
                rphoto.photo_id, rphoto.file_hash, COMBINED_MODEL_VERSION
            ):
                photos_skipped += 1
                continue

            ext = Path(rphoto.original_filename).suffix or ".jpg"
            local_file = photos_tmp / f"{rphoto.photo_id}{ext}"
            now_iso = datetime.datetime.now(datetime.UTC).isoformat()
            _log(
                f"      [{idx}/{len(remote_photos)}] Downloading & analyzing "
                f"{rphoto.original_filename}..."
            )
            try:
                backend.download_photo(rphoto, local_file)
                actual_hash = rphoto.file_hash or compute_file_sha256(local_file)
                if store.is_photo_indexed(rphoto.photo_id, actual_hash, COMBINED_MODEL_VERSION):
                    photos_skipped += 1
                    continue

                bgr = load_image_bgr(local_file)
                orig_h, orig_w = bgr.shape[:2]
                analysis = engine.analyze_image(bgr)
                store.save_photo_analysis(
                    photo_id=rphoto.photo_id,
                    file_hash=actual_hash,
                    filename=rphoto.original_filename,
                    original_width=orig_w,
                    original_height=orig_h,
                    analysis=analysis,
                )
                photos_indexed += 1
                _log(f"      -> Indexed {len(analysis.faces)} face(s) in {analysis.total_ms:.0f}ms")
                processing_records.append(
                    PhotoProcessingRecord(
                        photo_id=rphoto.photo_id,
                        processing_status="PROCESSED",
                        processed_at=now_iso,
                        model_version=COMBINED_MODEL_VERSION,
                    )
                )
            except Exception as exc:
                photos_failed += 1
                errors.append(f"Photo {rphoto.photo_id}: {exc}")
                processing_records.append(
                    PhotoProcessingRecord(
                        photo_id=rphoto.photo_id,
                        processing_status="ERROR",
                        processed_at=now_iso,
                        model_version=COMBINED_MODEL_VERSION,
                        error=str(exc)[:200],
                    )
                )
            finally:
                if local_file.exists():
                    local_file.unlink()

        if processing_records:
            backend.upsert_photo_processing(processing_records)

    # 4. Run local SQLite matching (milliseconds across all cached faces)
    _log(f"[4/5] Matching cached faces locally (threshold={threshold})...")
    match_summary = run_matching(store, threshold=threshold, strategy=strategy)

    # 5. Push unsynced suggestions to FACE_MATCHES (metadata only — never embeddings)
    unsynced = store.get_unsynced_suggestions()
    _log(f"[5/5] Pushing {len(unsynced)} new suggestion(s) to FACE_MATCHES...")
    payload = [
        RemoteFaceMatch(
            photo_id=s.photo_id,
            guest_id=s.guest_id,
            confidence=s.confidence,
            status=s.status,
            model_version=s.model_version,
        )
        for s in unsynced
    ]
    pushed = backend.upsert_face_matches(payload)
    if unsynced:
        store.mark_suggestions_synced([s.id for s in unsynced])

    store.set_sync_state("last_sync_at", datetime.datetime.now(datetime.UTC).isoformat())
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    return SyncReport(
        reviews_pulled=reviews_pulled,
        profiles_processed=profiles_processed,
        profiles_revoked=profiles_revoked,
        photos_skipped_cached=photos_skipped,
        photos_newly_indexed=photos_indexed,
        photos_failed=photos_failed,
        new_suggestions_created=match_summary.new_suggestions,
        suggestions_pushed=pushed,
        elapsed_ms=elapsed_ms,
        errors=tuple(errors),
    )
