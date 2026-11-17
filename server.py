#!/usr/bin/env python3
"""
Silver Jubilee (25th Wedding Anniversary) Invitation Server
Navneet Goel & Ruby Goel | 25 November 2026
Zero-dependency Python + SQLite3 server providing:
- Mobile-first scrollable invitation website (GET /)
- Real SQLite database storage for guest blessings (POST /api/blessings)
- Password-protected private family dashboard for reading blessings (GET /blessings, POST /api/admin/blessings)
"""

import hashlib
import hmac
import html
import json
import mimetypes
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.path.join(BASE_DIR, "blessings.db")

UPLOADED_VIDEO_PATH = "/Users/piyushgoel/.gemini/antigravity/brain/3beffddf-63cb-48c1-a4c4-2f5c06fbac73/.user_uploaded/media_1794912736318_965aa237.mp4"
STATIC_VIDEO_PATH = os.path.join(STATIC_DIR, "couple-video.mp4")

UPLOADED_PHOTO_PATH = "/Users/piyushgoel/.gemini/antigravity/brain/3beffddf-63cb-48c1-a4c4-2f5c06fbac73/.user_uploaded/media_1794915867520_d9d5ece1.mp4"
STATIC_PHOTO_VIDEO = os.path.join(STATIC_DIR, "couple-origami.mp4")
ROOT_PHOTO_VIDEO = os.path.join(BASE_DIR, "couple-origami.mp4")


def sync_video_asset():
    """Auto-copies the uploaded assets to static and root folders on server launch."""
    try:
        # Preserve original celebration video (do not delete)
        if os.path.exists(UPLOADED_VIDEO_PATH):
            if not os.path.exists(STATIC_VIDEO_PATH):
                shutil.copy2(UPLOADED_VIDEO_PATH, STATIC_VIDEO_PATH)

        # Sync new couple origami live photo/video asset to root and static
        if os.path.exists(UPLOADED_PHOTO_PATH):
            for target in (STATIC_PHOTO_VIDEO, ROOT_PHOTO_VIDEO):
                if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(UPLOADED_PHOTO_PATH):
                    shutil.copy2(UPLOADED_PHOTO_PATH, target)
                    print(f"🖼️ Synced couple portrait asset to {target}")

            # Try generating static PNG thumbnail via macOS built-in qlmanage
            thumb_path = os.path.join(BASE_DIR, "couple-portrait.png")
            static_thumb = os.path.join(STATIC_DIR, "couple-portrait.png")
            if not os.path.exists(thumb_path):
                import subprocess
                subprocess.run(["qlmanage", "-t", "-s", "1080", "-o", BASE_DIR, ROOT_PHOTO_VIDEO], capture_output=True)
                gen = os.path.join(BASE_DIR, "couple-origami.mp4.png")
                if os.path.exists(gen):
                    os.rename(gen, thumb_path)
                    shutil.copy2(thumb_path, static_thumb)
                    print(f"📸 Generated high-res portrait image: {thumb_path}")
    except Exception as e:
        print(f"Asset sync note: {e}")

# Default family password (can be overridden via ADMIN_PASSWORD env var)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "goel25")
SECRET_SALT = "navneet-ruby-silver-jubilee-2026"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS blessings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guest_name TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Check if table is empty; seed one warm welcome blessing from the family if empty
            cur = conn.execute("SELECT COUNT(*) FROM blessings")
            count = cur.fetchone()[0]
            if count == 0:
                conn.execute(
                    "INSERT INTO blessings (guest_name, message, created_at) VALUES (?, ?, ?)",
                    (
                        "Piyush Goel",
                        "Happy 25th Wedding Anniversary Mummy & Papa! Wishing you endless love, health, and happiness on your Silver Jubilee.",
                        datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC"),
                    ),
                )
    finally:
        conn.close()


def make_token(password: str) -> str:
    return hmac.new(
        SECRET_SALT.encode("utf-8"), password.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class InviteRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, format, *args):
        # Clean console logging
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()

        if route in ("/couple-video.mp4", "/static/couple-video.mp4", "/couple-origami.mp4", "/static/couple-origami.mp4"):
            if "origami" in route:
                video_file = STATIC_PHOTO_VIDEO if os.path.exists(STATIC_PHOTO_VIDEO) else (
                    ROOT_PHOTO_VIDEO if os.path.exists(ROOT_PHOTO_VIDEO) else UPLOADED_PHOTO_PATH
                )
            else:
                video_file = STATIC_VIDEO_PATH if os.path.exists(STATIC_VIDEO_PATH) else UPLOADED_VIDEO_PATH
            if os.path.exists(video_file):
                file_size = os.path.getsize(video_file)
                range_header = self.headers.get("Range")
                if range_header:
                    range_match = range_header.strip().replace("bytes=", "").split("-")
                    start = int(range_match[0]) if range_match[0] else 0
                    end = int(range_match[1]) if len(range_match) > 1 and range_match[1] else file_size - 1
                    end = min(end, file_size - 1)
                    length = end - start + 1

                    self.send_response(206)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    with open(video_file, "rb") as f:
                        f.seek(start)
                        self.wfile.write(f.read(length))
                    return
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(file_size))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    with open(video_file, "rb") as f:
                        self.wfile.write(f.read())
                    return

        if route in ("/blessings", "/blessings/", "/admin"):
            self.path = "/blessings.html"
            return super().do_GET()

        if route == "/api/health":
            return self._send_json(200, {"status": "ok", "occasion": "25th Wedding Anniversary - Navneet & Ruby Goel"})

        if route == "/api/admin/blessings":
            # Verify password header or query param
            qs = parse_qs(parsed.query)
            pwd = self.headers.get("X-Admin-Password", "") or (qs.get("password", [""])[0])
            token = self.headers.get("X-Admin-Token", "")
            expected_token = make_token(ADMIN_PASSWORD)

            if pwd != ADMIN_PASSWORD and token != expected_token:
                return self._send_json(401, {"error": "Invalid password. Please enter the family password."})

            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, guest_name, message, created_at FROM blessings ORDER BY id DESC"
                ).fetchall()
                items = [
                    {
                        "id": r["id"],
                        "name": r["guest_name"],
                        "message": r["message"],
                        "created_at": r["created_at"],
                    }
                    for r in rows
                ]
                return self._send_json(
                    200,
                    {
                        "count": len(items),
                        "blessings": items,
                        "token": expected_token,
                    },
                )
            finally:
                conn.close()

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length > 65536:
            return self._send_json(413, {"error": "Payload too large."})

        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return self._send_json(400, {"error": "Invalid JSON format."})

        if route == "/api/blessings":
            name = (data.get("name") or "").strip()
            message = (data.get("message") or "").strip()

            if not name:
                return self._send_json(400, {"error": "Please enter your name."})
            if len(name) > 80:
                return self._send_json(400, {"error": "Name must be 80 characters or fewer."})
            if not message:
                return self._send_json(400, {"error": "Please enter a blessing message before sending."})
            if len(message) > 500:
                return self._send_json(
                    400,
                    {"error": f"Blessing message must be 500 characters or fewer (currently {len(message)})."},
                )

            timestamp = datetime.now().strftime("%d %b %Y, %I:%M %p")
            conn = sqlite3.connect(DB_PATH)
            try:
                with conn:
                    cur = conn.execute(
                        "INSERT INTO blessings (guest_name, message, created_at) VALUES (?, ?, ?)",
                        (name, message, timestamp),
                    )
                    new_id = cur.lastrowid
                return self._send_json(
                    201,
                    {
                        "status": "Blessing sent",
                        "id": new_id,
                        "name": name,
                        "message": message,
                        "created_at": timestamp,
                    },
                )
            finally:
                conn.close()

        if route == "/api/admin/login":
            pwd = (data.get("password") or "").strip()
            if pwd != ADMIN_PASSWORD:
                return self._send_json(401, {"error": "Incorrect password."})
            return self._send_json(200, {"status": "authenticated", "token": make_token(ADMIN_PASSWORD)})

        if route == "/api/admin/delete":
            pwd = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if pwd != ADMIN_PASSWORD and token != make_token(ADMIN_PASSWORD):
                return self._send_json(401, {"error": "Unauthorized."})
            blessing_id = data.get("id")
            if not blessing_id:
                return self._send_json(400, {"error": "Missing blessing id."})
            conn = sqlite3.connect(DB_PATH)
            try:
                with conn:
                    conn.execute("DELETE FROM blessings WHERE id = ?", (blessing_id,))
                return self._send_json(200, {"status": "deleted", "id": blessing_id})
            finally:
                conn.close()

        return self._send_json(404, {"error": "Endpoint not found."})


def run_server():
    sync_video_asset()
    init_db()
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), InviteRequestHandler)
    print(f"✨ Silver Jubilee Invitation Website running at http://localhost:{port}")
    print(f"🔒 Private Family Blessings Page at http://localhost:{port}/blessings (Password: {ADMIN_PASSWORD})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    run_server()
