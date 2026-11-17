# Silver Jubilee Private Anniversary Memories Gallery
## Technical Implementation Plan & Phase 0 Baseline Record (Strict ₹0 Budget)

---

## 1. Phase 0 — Baseline Audit & Backup Record

| Item | Recorded Value |
| :--- | :--- |
| **Project** | Navneet & Ruby Goel — Silver Jubilee (25th Wedding Anniversary) Invitation |
| **GitHub Repository** | `https://github.com/piyushgoel2808/anniversary-invite` |
| **Live GitHub Pages URL** | `https://piyushgoel2808.github.io/anniversary-invite/` |
| **Baseline Branch / Commit** | `master` @ `3dd51e8927c7a819b54dbd3afa16ba3f23bb91e7` |
| **Target Feature Branch** | `feature/private-anniversary-gallery` |
| **Legacy Blessings Backup** | Archived untouched in `archive/legacy-blessings-backup/` (`index.html`, `blessings.html`, `server.py`) |
| **Media Assets Preserved** | `couple-origami.mp4`, `couple-video.mp4`, `video.mp4`, `og-preview.png`, `og-preview.svg` |
| **Budget Constraint** | **Strictly ₹0** (No Cloud Run, no paid GCP billing, no paid storage/CDN/DB/domain) |

---

## 2. Approved ₹0 Architecture

| Layer | Technology | Cost |
| :--- | :--- | :--- |
| **Invitation Website** | Existing Vanilla HTML/CSS/JS on **GitHub Pages** (Pages 1–4 untouched; Page 5 links to Memories Gallery) | ₹0 |
| **Gallery & Admin Web App** | **Google Apps Script** (HTML Service + `google.script.run`, deployed as Web App running as owner, accessible to anonymous visitors) | ₹0 |
| **Private Photo Storage** | Owner's **Google Drive** (5 TB Google AI Pro quota, strictly private folders — zero public links) | ₹0 |
| **Guest & Photo Databases** | **Google Sheets** (`GUEST_DATABASE` + `PHOTO_DATABASE`) | ₹0 |
| **Image Compression / Resizing** | Browser **HTML5 Canvas API** (generates WebP thumbnails & medium derivatives; strips EXIF GPS from derivatives; keeps original untouched) | ₹0 |
| **Caching & Concurrency** | Apps Script **`CacheService`** (disposable cache) + **`LockService`** (serialized writes) | ₹0 |

---

## 3. Private Google Drive & Google Sheets Schema

### 3.1 Google Drive Folder Hierarchy (All Strictly Private)
```text
ANNIVERSARY_GALLERY/
├── ORIGINAL_PHOTOS/   # Untouched original uploads (admin-only access)
├── THUMBNAILS/        # 500 px longest-edge WebP (~25–60 KB)
├── MEDIUM_IMAGES/     # 1400 px longest-edge WebP (~150–300 KB)
└── ADMIN_EXPORTS/     # Admin-triggered metadata CSV exports
```
- **Privacy Rule:** Never enable "Anyone with the link" on any folder or file. Never return Drive file IDs or Drive URLs to guest-facing code.

### 3.2 `GUEST_DATABASE` Sheet Columns
`guest_id` | `guest_name` | `username` | `mobile_hash` | `salt` | `status` | `upload_permission` | `created_at`

### 3.3 `PHOTO_DATABASE` Sheet Columns
`photo_id` | `drive_file_id` | `thumbnail_id` | `medium_image_id` | `guest_id` | `original_filename` | `upload_timestamp` | `mime_type` | `file_size` | `file_hash` | `width` | `height` | `status`

---

## 4. Apps Script Module Structure

All `.gs` and `.html` files will be authored in `apps-script/` in this repository for version control and deployed to the standalone Apps Script project **Silver Jubilee Memories Gallery**:

```text
apps-script/
├── Code.gs        # doGet(e) routing (?page=admin vs guest gallery) + HTML includes
├── Config.gs      # Script Properties keys, Drive/Sheet IDs, limits, setup bootstrapper
├── Security.gs    # Mobile normalization, HMAC-SHA-256 + pepper + salt, constant-time compare, rate limiting
├── Auth.gs        # Guest login, signed expiring session tokens, logout, active status verification
├── Guests.gs      # GUEST_DATABASE batch reads/writes, LockService, CSV bulk importer
├── Drive.gs       # Private DriveApp read/write/trash helpers (never returns Drive URLs/IDs)
├── Cache.gs       # CacheService wrapper with 95 KB per-key safety guard (Apps Script hard limit: 100 KB)
├── Photos.gs      # Paginated metadata list, getPrivatePhoto() & getPrivatePhotoBatch()
├── Uploads.gs     # Form-to-Blob original upload + WebP derivatives + deduplication + rollback
├── Admin.gs       # Separate admin auth, guest toggle, photo hide/delete, original download, CSV export
├── Index.html     # Guest gallery shell (Ivory / Royal Blue / Silver theme)
├── Admin.html     # Isolated admin dashboard
├── Styles.html    # Shared Silver Jubilee design tokens, masonry layout, lightbox styles
└── Scripts.html   # Guest login, Canvas WebP processor, sequential upload queue, masonry & lightbox JS
```

---

## 5. Key Apps Script Technical Guardrails

1. **Authentication & Token Trade-off (Explicitly Acknowledged):**
   - Apps Script HTML Service does not support custom `HttpOnly` cookies.
   - Mitigation: Server-signed, short-lived session tokens (12-hour TTL) stored in `sessionStorage` / JS memory, validated on **every** `google.script.run` call against both cryptographic signature AND live guest `status === 'active'`. Strict DOM text rendering (`textContent`, never `innerHTML` with user data) and zero third-party scripts mitigate XSS risk.
2. **Quota & Execution Limits:**
   - **6-minute** max execution time per call.
   - **30 simultaneous executions** per consumer Google account.
   - **10 concurrent `google.script.run`** calls per browser page.
   - **Mitigation:** Paginate 20–24 photo records per page; load thumbnails in small batches (`getPrivatePhotoBatch` of 4–6 thumbnails per server call) with client concurrency capped at 2 active calls per page; cache thumbnails under **95 KB** in `CacheService` (respecting the strict 100 KB per-key limit).
3. **Upload Transport & Size Ceiling:**
   - Initial per-original limit: **10 MB** (JPEG, PNG, WebP only — validated by browser + server magic-byte checks).
   - Use Apps Script's native HTML `<form>`-to-server `Blob` transport for the original image (avoiding Base64 inflation on 10 MB files) paired with hidden fields for the Canvas-generated WebP thumbnail (~40 KB) and medium (~200 KB) derivatives.
   - Sequential upload queue (1 active upload per guest at a time) with per-file progress, SHA-256 duplicate prevention, atomic rollback if metadata save fails, and one-click retry on failure.

---

## 6. Phased Execution Checklist

- [x] **Phase 0:** Audit baseline, archive legacy blessings files, record GitHub Pages deployment, write technical plan.
- [x] **Phase 1:** Update Page 5 of `index.html` (replace visible blessings card with Anniversary Memories entry card; keep Pages 1–4 untouched).
- [x] **Phase 2:** Build Apps Script project scaffold (`appsscript.json`, `Config.gs`, `Drive.gs`, `Cache.gs`, `Code.gs`) + private Drive/Sheets bootstrapper + `runPhase2SmokeTest()`.
- [x] **Phase 3:** Build `Security.gs`, `Auth.gs`, `Guests.gs` + CSV guest import template + `runPhase3AuthTests()`.
- [x] **Phase 4:** Build `Uploads.gs` + Canvas WebP derivative generator + sequential upload queue & retry.
- [x] **Phase 5:** Build `Photos.gs`, `Cache.gs` + private `getPrivatePhoto` / `getPrivatePhotoBatch` retrieval.
- [x] **Phase 6:** Build responsive Ivory/Royal Blue/Silver masonry gallery & full-screen lightbox (`Index.html`, `Styles.html`, `Scripts.html`).
- [x] **Phase 7:** Build isolated Admin Panel (`Admin.gs`, `Admin.html`) with separate admin secret auth.
- [x] **Phase 8 (Code Ready):** Authored 18-case automated security suite (`Tests.gs`) — pending execution inside live deployed Apps Script project.
- [ ] **Phase 9:** Perform live performance & quota testing (10 accounts, 50 -> 100 -> 500 images, mobile Safari + Android Chrome) on deployed `/exec`.
- [ ] **Phase 10:** Final production `/exec` deployment, wire final URL into `MEMORIES_GALLERY_URL` in `index.html`, and publish via GitHub Pages.
