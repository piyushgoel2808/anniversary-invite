# Silver Jubilee Memories Gallery — Google Apps Script Deployment & Operations Guide (₹0 Budget)

This folder contains the complete **₹0-cost Google Apps Script backend & frontend** for the private anniversary photo-sharing gallery.

---

## Step 1 — Create the Standalone Apps Script Project

1. Open **[https://script.google.com](https://script.google.com)** signed into the Google account that owns your 5 TB Google AI Pro Drive storage.
2. Click **New project** and rename it to **`Silver Jubilee Memories Gallery`**.
3. In **Project Settings** (gear icon on the left sidebar), check **"Show `appsscript.json` manifest file in editor"**.

---

## Step 2 — Add All Files (or Push via `clasp`)

### Option A: Browser Editor (Zero Local Tools Needed)
Create each file below in the Apps Script editor and paste the matching file from `apps-script/`:

- **Manifest:** `appsscript.json`
- **Script files (`.gs`):** `Code`, `Config`, `Security`, `Cache`, `Drive`, `Guests`, `Auth`, `Uploads`, `Photos`, `Admin`, `Tests`
- **HTML files (`.html`):** `Index`, `AdminPage`, `Styles`, `Scripts`

*(Remove any unused default code in `Code.gs` before pasting.)*

### Option B: Using Google's Free `clasp` CLI (Fastest)
```bash
npm install -g @google/clasp
clasp login
cd /Users/piyushgoel/Downloads/Projects/invite/apps-script
clasp create --type standalone --title "Silver Jubilee Memories Gallery"
clasp push -f
```

---

## Step 3 — Run One-Time Setup & Security Verification

> **Security Note:** All setup and test helpers (`setupInfrastructure_`, `setAdminCredentials_`, `runPhase2SmokeTest_`, `runPhase3AuthTests_`, `runPhase8SecurityTestSuite_`) intentionally end with a trailing underscore (`_`) so Apps Script **never** exposes them to anonymous web visitors via `google.script.run`.

1. In the Apps Script editor, create a temporary file or paste this temporary wrapper at the bottom of `Code.gs`:
   ```javascript
   function runOneTimeBootstrap() {
     setupInfrastructure_();
     setAdminCredentials_('piyushadmin', 'ReplaceWithStrongPassphrase2026!');
   }

   function runAllVerificationTests() {
     runPhase2SmokeTest_();
     runPhase3AuthTests_();
     runPhase8SecurityTestSuite_();
   }
   ```
2. Select **`runOneTimeBootstrap`** from the editor toolbar dropdown and click **Run**.
   - Approve the Google OAuth permissions prompt for your own account (guests will **never** see this or need Google accounts).
   - Verify in your Google Drive that the private folder `ANNIVERSARY_GALLERY/` was created containing:
     - `ORIGINAL_PHOTOS/`, `THUMBNAILS/`, `MEDIUM_IMAGES/`, `ADMIN_EXPORTS/`, and `SILVER_JUBILEE_GALLERY_DB`.
3. Select **`runAllVerificationTests`** and click **Run**.
   - Confirm **`allPassed: true`** across all **22 automated security, magic-byte, MIME-sanitization, durable-logout, permission, and privacy assertions** in the Execution Log.
4. **IMPORTANT:** Delete `runOneTimeBootstrap` and `runAllVerificationTests` completely before deploying so your admin passphrase never sits in code and no setup/test function is exposed on the Web App RPC surface.

---

## Step 4 — Deploy as Production Web App (`/exec`)

1. Click **Deploy** $\rightarrow$ **New deployment**.
2. Click the gear icon $\rightarrow$ select **Web app**.
3. Configure:
   - **Description:** `v1 Silver Jubilee Memories Gallery`
   - **Execute as:** `Me (your Google account)`
   - **Who has access:** `Anyone` (allows invited guests without Google accounts to open the login page and sign in with their guest username + registered mobile number).
4. Click **Deploy** and copy the **`Web app URL`** ending in **`/exec`** (never use `/dev`).

---

## Step 5 — Import Your Guest List & Verify Live Privacy

1. Open your Admin Panel at:
   ```text
   https://script.google.com/macros/s/<YOUR_DEPLOYMENT_ID>/exec?page=admin
   ```
2. Sign in with your Admin username & password.
3. Go to **Guest Management & CSV Import** and upload/paste your guest list following [../templates_csv/guest_import_template.csv](../templates_csv/guest_import_template.csv).
4. Open the Guest Gallery URL in an **Incognito window** (signed out of Google):
   - Confirm the login screen appears (no photos visible).
   - Sign in as a test guest, upload a real phone photo (iPhone Safari + Android Chrome), view it in the masonry grid and full-screen lightbox.
   - Confirm no delete/edit buttons exist and no Google Drive URLs appear in DevTools Network/DOM.

---

## Step 6 — Connect to Your GitHub Pages Invitation

Once verified, paste your `/exec` URL into `MEMORIES_GALLERY_URL` in [../index.html](../index.html) (and [../static/index.html](../static/index.html)), commit, and push to GitHub Pages!

---

## Quota & Operational Safeguards (Consumer Google Account)

| Constraint | Official Limit | Built-In Mitigation |
| :--- | :--- | :--- |
| Concurrent Executions | 30 simultaneous per owner | Batched thumbnails (`getPrivatePhotoBatch`, 5/call), max 2 concurrent calls/client, sequential 1-by-1 uploads |
| `google.script.run` / Page | 10 concurrent per page | Capped at 2 active thumbnail batches per browser |
| `CacheService` Per Key | 100 KB max | Enforced 95 KB safety ceiling (`Cache.gs`); caches small WebP thumbnails only |
| Execution Timeout | 6 minutes | Single-photo uploads finish in ~2–5s; pagination loads 24 records/page |
| Max Upload Size | 10 MB initial limit | Validated in browser + server magic bytes before Drive write |
| Total Extra Cost | **₹0** | Zero Cloud Run, zero GCP billing, zero paid APIs |
