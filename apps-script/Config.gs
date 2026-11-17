/**
 * Config.gs — Configuration, Script Properties, and One-Time Infrastructure Bootstrap
 *
 * All folder IDs, spreadsheet IDs, and cryptographic secrets are stored strictly
 * in Google Apps Script ScriptProperties (never hardcoded in source or exposed to guests).
 */

var APP_CONFIG = {
  APP_NAME: 'Navneet & Ruby — Silver Jubilee Memories',
  ROOT_FOLDER_NAME: 'ANNIVERSARY_GALLERY',
  SUBFOLDERS: {
    ORIGINAL_PHOTOS: 'ORIGINAL_PHOTOS',
    THUMBNAILS: 'THUMBNAILS',
    MEDIUM_IMAGES: 'MEDIUM_IMAGES',
    ADMIN_EXPORTS: 'ADMIN_EXPORTS'
  },
  SPREADSHEET_NAME: 'SILVER_JUBILEE_GALLERY_DB',
  SHEETS: {
    GUESTS: 'GUEST_DATABASE',
    PHOTOS: 'PHOTO_DATABASE'
  },
  GUEST_HEADERS: [
    'guest_id',
    'guest_name',
    'username',
    'mobile_hash',
    'salt',
    'status',
    'upload_permission',
    'created_at'
  ],
  PHOTO_HEADERS: [
    'photo_id',
    'drive_file_id',
    'thumbnail_id',
    'medium_image_id',
    'guest_id',
    'original_filename',
    'upload_timestamp',
    'mime_type',
    'file_size',
    'file_hash',
    'width',
    'height',
    'status'
  ],
  LIMITS: {
    SESSION_TTL_SECONDS: 12 * 60 * 60, // 12 hours
    ADMIN_SESSION_TTL_SECONDS: 4 * 60 * 60, // 4 hours
    MAX_ORIGINAL_BYTES: 10 * 1024 * 1024, // 10 MB initial tested limit
    MAX_THUMB_BASE64_CHARS: 250000, // ~180 KB safety bound
    MAX_MEDIUM_BASE64_CHARS: 1500000, // ~1.1 MB safety bound
    LOGIN_MAX_ATTEMPTS: 5,
    LOGIN_LOCKOUT_SECONDS: 15 * 60, // 15 minutes
    DOWNLOAD_MAX_PER_WINDOW: 60, // Generous per-guest burst limit to protect Drive quota
    DOWNLOAD_WINDOW_SECONDS: 10 * 60, // 10-minute window
    PAGE_SIZE_DEFAULT: 24,
    PAGE_SIZE_MAX: 36,
    BATCH_THUMB_MAX: 6,
    CACHE_MAX_VALUE_BYTES: 95000, // Apps Script CacheService hard cap is 100 KB
    HASH_ITERATIONS: 150, // Balanced for 150-guest CSV import latency + 256-bit server pepper
    ADMIN_HASH_ITERATIONS: 2000, // High work factor for single administrator credential
    REVOKED_SHARD_COUNT: 16, // Sharded durable revocation keys keep each property well under 9 KB
    LOCK_WAIT_MS: 20000
  },
  ALLOWED_MIME_TYPES: [
    'image/jpeg',
    'image/png',
    'image/webp'
  ],
  PROP_KEYS: {
    ROOT_FOLDER_ID: 'ROOT_FOLDER_ID',
    ORIGINAL_FOLDER_ID: 'ORIGINAL_FOLDER_ID',
    THUMBNAILS_FOLDER_ID: 'THUMBNAILS_FOLDER_ID',
    MEDIUM_FOLDER_ID: 'MEDIUM_FOLDER_ID',
    EXPORTS_FOLDER_ID: 'EXPORTS_FOLDER_ID',
    SPREADSHEET_ID: 'SPREADSHEET_ID',
    AUTH_PEPPER: 'AUTH_PEPPER',
    SESSION_SECRET: 'SESSION_SECRET',
    ADMIN_USERNAME: 'ADMIN_USERNAME',
    ADMIN_PASSWORD_HASH: 'ADMIN_PASSWORD_HASH',
    ADMIN_PASSWORD_SALT: 'ADMIN_PASSWORD_SALT',
    REVOKED_SHARD_PREFIX: 'REV_SHARD_'
  }
};

// Per-execution in-memory property cache to avoid repeated PropertiesService quota hits
var PROP_MEM_CACHE_ = {};

var CACHEABLE_PROP_KEYS_ = {
  ROOT_FOLDER_ID: true,
  ORIGINAL_FOLDER_ID: true,
  THUMBNAILS_FOLDER_ID: true,
  MEDIUM_FOLDER_ID: true,
  EXPORTS_FOLDER_ID: true,
  SPREADSHEET_ID: true,
  AUTH_PEPPER: true,
  SESSION_SECRET: true
};

/**
 * Retrieves a required Script Property (using in-memory + short CacheService caching
 * for immutable configuration keys to stay far under the 50,000/day PropertiesService quota).
 */
function getRequiredProp_(key) {
  if (PROP_MEM_CACHE_[key]) {
    return PROP_MEM_CACHE_[key];
  }
  if (CACHEABLE_PROP_KEYS_[key] && typeof cacheGetString_ === 'function') {
    var cached = cacheGetString_(CACHE_KEYS.PROP_PREFIX + key);
    if (cached) {
      PROP_MEM_CACHE_[key] = cached;
      return cached;
    }
  }
  var val = PropertiesService.getScriptProperties().getProperty(key);
  if (!val) {
    throw new Error('Missing Script Property "' + key + '". Run setupInfrastructure_() first.');
  }
  PROP_MEM_CACHE_[key] = val;
  if (CACHEABLE_PROP_KEYS_[key] && typeof cachePutString_ === 'function') {
    cachePutString_(CACHE_KEYS.PROP_PREFIX + key, val, 900);
  }
  return val;
}

function clearPropCache_() {
  PROP_MEM_CACHE_ = {};
  if (typeof cacheRemove_ === 'function') {
    var keys = Object.keys(CACHEABLE_PROP_KEYS_);
    for (var i = 0; i < keys.length; i++) {
      cacheRemove_(CACHE_KEYS.PROP_PREFIX + keys[i]);
    }
  }
}

/**
 * Generates high-entropy secret material using multiple UUIDs + SHA-256.
 */
function generateSecretKey_() {
  var raw = [
    Utilities.getUuid(),
    Utilities.getUuid(),
    Utilities.getUuid(),
    String(Date.now()),
    String(Math.random())
  ].join('|');
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, raw, Utilities.Charset.UTF_8);
  return Utilities.base64EncodeWebSafe(digest);
}

/**
 * Ensures cryptographic secrets (AUTH_PEPPER, SESSION_SECRET) exist in Script Properties.
 */
function ensureCryptoSecrets_() {
  var props = PropertiesService.getScriptProperties();
  if (!props.getProperty(APP_CONFIG.PROP_KEYS.AUTH_PEPPER)) {
    props.setProperty(APP_CONFIG.PROP_KEYS.AUTH_PEPPER, generateSecretKey_());
  }
  if (!props.getProperty(APP_CONFIG.PROP_KEYS.SESSION_SECRET)) {
    props.setProperty(APP_CONFIG.PROP_KEYS.SESSION_SECRET, generateSecretKey_());
  }
}

function isMissingDriveItemError_(err) {
  var msg = String(err && err.message ? err.message : err);
  return /no item with the given id|not found|does not exist|document is missing/i.test(msg);
}

/**
 * Helper to reuse a previously created private folder (by Script Property ID)
 * or create a new strictly private folder.
 * Recreates ONLY if the stored ID is missing/trashed; rethrows transient errors
 * so re-running setup never orphans an existing folder.
 */
function getOrCreatePrivateFolder_(parentFolder, name, propKey) {
  var props = PropertiesService.getScriptProperties();
  var existingId = propKey ? props.getProperty(propKey) : null;
  var folder = null;

  if (existingId) {
    try {
      var candidate = DriveApp.getFolderById(existingId);
      if (candidate && !candidate.isTrashed()) {
        folder = candidate;
      }
    } catch (e) {
      if (isMissingDriveItemError_(e)) {
        folder = null;
      } else {
        throw e;
      }
    }
  }

  if (!folder) {
    folder = parentFolder ? parentFolder.createFolder(name) : DriveApp.createFolder(name);
  }
  // Enforce strictly private sharing (zero public links)
  folder.setSharing(DriveApp.Access.PRIVATE, DriveApp.Permission.NONE);
  return folder;
}

/**
 * ONE-TIME ADMIN SETUP FUNCTION (Private — call from a temporary editor wrapper and delete wrapper)
 *
 * 1. Generates server-side AUTH_PEPPER & SESSION_SECRET.
 * 2. Creates private ANNIVERSARY_GALLERY/ folder tree in your Google Drive.
 * 3. Creates private SILVER_JUBILEE_GALLERY_DB spreadsheet with GUEST_DATABASE & PHOTO_DATABASE.
 * 4. Stores all IDs in Script Properties so runtime never scans Drive folders.
 */
function setupInfrastructure_() {
  ensureCryptoSecrets_();
  var props = PropertiesService.getScriptProperties();

  // 1. Root & 4 subfolders (all strictly private)
  var rootFolder = getOrCreatePrivateFolder_(null, APP_CONFIG.ROOT_FOLDER_NAME, APP_CONFIG.PROP_KEYS.ROOT_FOLDER_ID);
  var originalFolder = getOrCreatePrivateFolder_(rootFolder, APP_CONFIG.SUBFOLDERS.ORIGINAL_PHOTOS, APP_CONFIG.PROP_KEYS.ORIGINAL_FOLDER_ID);
  var thumbFolder = getOrCreatePrivateFolder_(rootFolder, APP_CONFIG.SUBFOLDERS.THUMBNAILS, APP_CONFIG.PROP_KEYS.THUMBNAILS_FOLDER_ID);
  var mediumFolder = getOrCreatePrivateFolder_(rootFolder, APP_CONFIG.SUBFOLDERS.MEDIUM_IMAGES, APP_CONFIG.PROP_KEYS.MEDIUM_FOLDER_ID);
  var exportsFolder = getOrCreatePrivateFolder_(rootFolder, APP_CONFIG.SUBFOLDERS.ADMIN_EXPORTS, APP_CONFIG.PROP_KEYS.EXPORTS_FOLDER_ID);

  // 2. Database Spreadsheet inside ANNIVERSARY_GALLERY (strictly private)
  var existingSheetId = props.getProperty(APP_CONFIG.PROP_KEYS.SPREADSHEET_ID);
  var ss = null;
  if (existingSheetId) {
    try {
      var existingFile = DriveApp.getFileById(existingSheetId);
      if (existingFile && !existingFile.isTrashed()) {
        ss = SpreadsheetApp.openById(existingSheetId);
      }
    } catch (e) {
      if (isMissingDriveItemError_(e)) {
        ss = null;
      } else {
        throw e;
      }
    }
  }
  if (!ss) {
    ss = SpreadsheetApp.create(APP_CONFIG.SPREADSHEET_NAME);
    var ssFile = DriveApp.getFileById(ss.getId());
    ssFile.moveTo(rootFolder);
    ssFile.setSharing(DriveApp.Access.PRIVATE, DriveApp.Permission.NONE);
  }

  // Ensure GUEST_DATABASE tab + headers
  var guestSheet = ss.getSheetByName(APP_CONFIG.SHEETS.GUESTS);
  if (!guestSheet) {
    guestSheet = ss.insertSheet(APP_CONFIG.SHEETS.GUESTS);
  }
  if (guestSheet.getLastRow() === 0) {
    guestSheet.appendRow(APP_CONFIG.GUEST_HEADERS);
    guestSheet.setFrozenRows(1);
  }

  // Ensure PHOTO_DATABASE tab + headers
  var photoSheet = ss.getSheetByName(APP_CONFIG.SHEETS.PHOTOS);
  if (!photoSheet) {
    photoSheet = ss.insertSheet(APP_CONFIG.SHEETS.PHOTOS);
  }
  if (photoSheet.getLastRow() === 0) {
    photoSheet.appendRow(APP_CONFIG.PHOTO_HEADERS);
    photoSheet.setFrozenRows(1);
  }

  // Remove default empty Sheet1 if both database tabs exist
  var defaultSheet = ss.getSheetByName('Sheet1');
  if (defaultSheet && ss.getSheets().length > 2 && defaultSheet.getLastRow() === 0) {
    ss.deleteSheet(defaultSheet);
  }

  // 3. Persist IDs in Script Properties and clear cached property entries
  props.setProperties({
    ROOT_FOLDER_ID: rootFolder.getId(),
    ORIGINAL_FOLDER_ID: originalFolder.getId(),
    THUMBNAILS_FOLDER_ID: thumbFolder.getId(),
    MEDIUM_FOLDER_ID: mediumFolder.getId(),
    EXPORTS_FOLDER_ID: exportsFolder.getId(),
    SPREADSHEET_ID: ss.getId()
  });
  clearPropCache_();

  var summary = {
    status: 'OK',
    rootFolder: rootFolder.getName(),
    spreadsheetName: ss.getName(),
    foldersInitialized: Object.keys(APP_CONFIG.SUBFOLDERS),
    sheetsInitialized: [APP_CONFIG.SHEETS.GUESTS, APP_CONFIG.SHEETS.PHOTOS],
    note: 'All Drive folders and Sheets are set to PRIVATE. Next: run setAdminCredentials_(username, strongPassword) from a temporary editor function.'
  };
  Logger.log(JSON.stringify(summary, null, 2));
  return summary;
}

/**
 * ONE-TIME ADMIN CREDENTIAL SETUP (Private — NEVER exposed via google.script.run)
 * Call from a temporary function in the Apps Script Editor, then delete the temporary function:
 *   function initOnce() { setAdminCredentials_('admin', 'YourStrongUniquePassphrase2026!'); }
 * Stores only salted + peppered HMAC-SHA-256 hash in Script Properties.
 */
function setAdminCredentials_(username, plainPassword) {
  ensureCryptoSecrets_();
  var cleanUser = normalizeUsername_(username);
  if (!cleanUser || cleanUser.length < 3) {
    throw new Error('Admin username must be at least 3 valid characters.');
  }
  if (!plainPassword || String(plainPassword).length < 8) {
    throw new Error('Admin password must be at least 8 characters long.');
  }
  var salt = generateSalt_();
  var hash = hashCredential_(String(plainPassword), salt, APP_CONFIG.LIMITS.ADMIN_HASH_ITERATIONS);
  PropertiesService.getScriptProperties().setProperties({
    ADMIN_USERNAME: cleanUser,
    ADMIN_PASSWORD_SALT: salt,
    ADMIN_PASSWORD_HASH: hash
  });
  clearPropCache_();
  Logger.log('Admin credentials configured securely for username: ' + cleanUser);
  return { ok: true, adminUsername: cleanUser };
}
