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

## Phase 2: Encrypted Local SQLite & Google Drive/Sheets Sync

### One-Time Google Cloud Desktop OAuth Setup (₹0 / Free)
1. Open Google Cloud Console -> create/select a project -> enable **Google Drive API** and **Google Sheets API**.
2. Go to **APIs & Services -> Credentials -> Create Credentials -> OAuth client ID -> Desktop app**.
3. Download the JSON file and save it as `face-indexer/data/auth/credentials.json` (gitignored).

### Run Incremental Sync
```bash
cd face-indexer
uv run face-indexer sync \
  --spreadsheet-id <SILVER_JUBILEE_GALLERY_DB_SPREADSHEET_ID> \
  --threshold 0.50 \
  --delete-refs
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
