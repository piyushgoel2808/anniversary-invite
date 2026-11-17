# Silver Jubilee Face Indexer

Local, privacy-first face detection, encrypted embedding store, and Google Drive/Sheets sync engine running on Apple Silicon (Python 3.12 + `uv`) using OpenCV **YuNet** (MIT) and **SFace** (Apache-2.0).

No photographs or face embeddings leave your MacBook Pro — only photo-to-guest metadata (`photo_id, guest_id, confidence, status, model_version`) is synced to Google Sheets.

Calibrated cosine similarity threshold: **`0.50`** (verified on real Silver Jubilee album photos).

---

## Setup

```bash
cd face-indexer
uv sync --python 3.12
uv run face-indexer download-models
uv run face-indexer verify-models
```

---

## Running a Phase 1 Calibration Benchmark on Real Album Photos

Prepare a local (gitignored) test directory under `face-indexer/data/`:

```text
face-indexer/data/
├── references/
│   ├── navneet/
│   │   ├── ref1.jpg        # Straight-facing clear photo
│   │   ├── ref2.jpg        # Slightly angled
│   │   └── ref3.jpg        # Different expression / older photo
│   ├── ruby/
│   │   └── ...
│   └── piyush/
│       └── ...
├── album/
│   ├── IMG_0001.jpg        # 50–100 representative anniversary / family photos
│   ├── IMG_0002.heic
│   └── ...
└── ground_truth.csv        # Optional: photo_filename,guest_id
```

Run the benchmark and generate an offline HTML visual review report:

```bash
uv run face-indexer benchmark \
  --references data/references \
  --album data/album \
  --ground-truth data/ground_truth.csv \
  --threshold 0.363 \
  --output-dir reports/run1
```

Open `reports/run1/report.html` in your browser to inspect:
- Per-guest suggested matches (with cropped face previews & cosine similarity scores)
- Precision / Recall / F1 sweep across thresholds (`0.26` to `0.58`) when `ground_truth.csv` is provided
- Medium-resolution (`1400 px`) vs. original-resolution fallback statistics and timing breakdown

---

## Inspecting a Single Photo

```bash
uv run face-indexer check-photo \
  --references data/references \
  --photo data/album/IMG_0001.jpg
```

---

## Running Tests & Quality Checks

```bash
uv run pytest --cov=face_indexer --cov-report=term-missing
uv run mypy src tests
uv run ruff check src tests
```

---

## Phase 2: Encrypted Local SQLite & Zero-Google-Cloud Sync

No Google Cloud Console project, no billing, and no OAuth setup required — `face-indexer sync` authenticates directly with your existing Silver Jubilee Apps Script Web App using your **Admin username & password** (₹0 forever).

### Run Incremental Sync (Manual)
```bash
cd face-indexer
uv run face-indexer sync --delete-refs
```
*(You will be prompted for your existing Gallery Admin username and password. Your password is never stored on disk, and the temporary session token is revoked immediately when sync finishes.)*

### User-Controlled macOS Auto-Sync (15-Minute Interval, Full ON/OFF Control)

**Option A — Clickable macOS App (No Terminal Needed):**
- Double-click **`Face Sync Control.command`** in Finder inside `face-indexer/`.
- On first click, it prompts once for your Gallery Admin username & password (stored securely in **macOS Keychain**), then shows a native control dialog with **Turn ON (15m)**, **Turn OFF**, and **Sync Now**.

**Option B — Terminal Commands:**
```bash
cd face-indexer
uv run face-indexer autosync setup            # One-time Keychain password setup (default: 15 min)
uv run face-indexer autosync on               # Turn ON (runs now & every 15 min while Mac is awake)
uv run face-indexer autosync off              # Turn OFF (stops immediately & stays OFF after reboot)
uv run face-indexer autosync status           # Check ON/OFF state, last sync, and DB counts
uv run face-indexer autosync logs             # View recent scheduled sync log entries
uv run face-indexer autosync run-now          # Trigger one immediate sync using Keychain
uv run face-indexer autosync uninstall        # Turn OFF and remove LaunchAgent + Keychain entry
```

What `sync` does on every run:
1. Pulls guest `CONFIRMED` / `REJECTED` reviews from `FACE_MATCHES` into local SQLite.
2. Processes any `PENDING_PROCESSING` guest face profiles from `FACE_REFERENCES/<guest_id>/` (and handles `WITHDRAWN` consent deletions).
3. Incrementally downloads & embeds only **new** active album photos (cached by SHA-256 hash in `data/indexer.db` with Fernet-encrypted embeddings).
4. Matches all cached faces against active guest profiles in milliseconds and pushes **metadata-only** `PENDING` suggestions to `FACE_MATCHES` (never overwriting guest reviews).

### Offline Local Commands
```bash
uv run face-indexer index-album --album data/album
uv run face-indexer register-guests --references data/references
uv run face-indexer match --threshold 0.50
uv run face-indexer status
```
