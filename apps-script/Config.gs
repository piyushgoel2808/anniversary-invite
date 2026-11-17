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
    PAGE_SIZE_DEFAULT: 24,
    PAGE_SIZE_MAX: 36,
    BATCH_THUMB_MAX: 6,
    CACHE_MAX_VALUE_BYTES: 95000, // Apps Script CacheService hard cap is 100 KB
    HASH_ITERATIONS: 2000,
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
    ADMIN_PASSWORD_SALT: 'ADMIN_PASSWORD_SALT'
  }
};

/**
 * Retrieves a required Script Property or throws an actionable setup error.
 */
function getRequiredProp_(key) {
  var val = PropertiesService.getScriptProperties().getProperty(key);
  if (!val) {
    throw new Error('Missing Script Property "' + key + '". Run setupInfrastructure() first.');
  }
  return val;
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

/**
 * Helper to find or create a strictly private subfolder inside a parent folder.
 */
function getOrCreatePrivateFolder_(parentFolder, name) {
  var iter = parentFolder ? parentFolder.getFoldersByName(name) : DriveApp.getFoldersByName(name);
  var folder;
  if (iter.hasNext()) {
    folder = iter.next();
  } else {
    folder = parentFolder ? parentFolder.createFolder(name) : DriveApp.createFolder(name);
  }
  // Enforce strictly private sharing (zero public links)
  folder.setSharing(DriveApp.Access.PRIVATE, DriveApp.Permission.NONE);
  return folder;
}

/**
 * ONE-TIME ADMIN SETUP FUNCTION (Run manually from Apps Script Editor)
 *
 * 1. Generates server-side AUTH_PEPPER & SESSION_SECRET.
 * 2. Creates private ANNIVERSARY_GALLERY/ folder tree in your Google Drive.
 * 3. Creates private SILVER_JUBILEE_GALLERY_DB spreadsheet with GUEST_DATABASE & PHOTO_DATABASE.
 * 4. Stores all IDs in Script Properties so runtime never scans Drive folders.
 */
function setupInfrastructure() {
  ensureCryptoSecrets_();
  var props = PropertiesService.getScriptProperties();

  // 1. Root & 4 subfolders (all strictly private)
  var rootFolder = getOrCreatePrivateFolder_(null, APP_CONFIG.ROOT_FOLDER_NAME);
  var originalFolder = getOrCreatePrivateFolder_(rootFolder, APP_CONFIG.SUBFOLDERS.ORIGINAL_PHOTOS);
  var thumbFolder = getOrCreatePrivateFolder_(rootFolder, APP_CONFIG.SUBFOLDERS.THUMBNAILS);
  var mediumFolder = getOrCreatePrivateFolder_(rootFolder, APP_CONFIG.SUBFOLDERS.MEDIUM_IMAGES);
  var exportsFolder = getOrCreatePrivateFolder_(rootFolder, APP_CONFIG.SUBFOLDERS.ADMIN_EXPORTS);

  // 2. Database Spreadsheet inside ANNIVERSARY_GALLERY (strictly private)
  var existingSheetId = props.getProperty(APP_CONFIG.PROP_KEYS.SPREADSHEET_ID);
  var ss;
  if (existingSheetId) {
    try {
      ss = SpreadsheetApp.openById(existingSheetId);
    } catch (e) {
      ss = null;
    }
  }
  if (!ss) {
    var files = rootFolder.getFilesByName(APP_CONFIG.SPREADSHEET_NAME);
    if (files.hasNext()) {
      ss = SpreadsheetApp.openById(files.next().getId());
    } else {
      ss = SpreadsheetApp.create(APP_CONFIG.SPREADSHEET_NAME);
      var ssFile = DriveApp.getFileById(ss.getId());
      ssFile.moveTo(rootFolder);
      ssFile.setSharing(DriveApp.Access.PRIVATE, DriveApp.Permission.NONE);
    }
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

  // 3. Persist IDs in Script Properties
  props.setProperties({
    ROOT_FOLDER_ID: rootFolder.getId(),
    ORIGINAL_FOLDER_ID: originalFolder.getId(),
    THUMBNAILS_FOLDER_ID: thumbFolder.getId(),
    MEDIUM_FOLDER_ID: mediumFolder.getId(),
    EXPORTS_FOLDER_ID: exportsFolder.getId(),
    SPREADSHEET_ID: ss.getId()
  });

  var summary = {
    status: 'OK',
    rootFolder: rootFolder.getName(),
    spreadsheetName: ss.getName(),
    foldersInitialized: Object.keys(APP_CONFIG.SUBFOLDERS),
    sheetsInitialized: [APP_CONFIG.SHEETS.GUESTS, APP_CONFIG.SHEETS.PHOTOS],
    note: 'All Drive folders and Sheets are set to PRIVATE. Next: run setAdminCredentials(username, strongPassword).'
  };
  Logger.log(JSON.stringify(summary, null, 2));
  return summary;
}

/**
 * ONE-TIME ADMIN CREDENTIAL SETUP (Run from Apps Script Editor)
 * Example: setAdminCredentials('admin', 'YourStrongUniquePassphrase2026!')
 * Stores only salted + peppered HMAC-SHA-256 hash in Script Properties.
 */
function setAdminCredentials(username, plainPassword) {
  ensureCryptoSecrets_();
  var cleanUser = normalizeUsername_(username);
  if (!cleanUser || cleanUser.length < 3) {
    throw new Error('Admin username must be at least 3 valid characters.');
  }
  if (!plainPassword || String(plainPassword).length < 10) {
    throw new Error('Admin password must be at least 10 characters long.');
  }
  var salt = generateSalt_();
  var hash = hashCredential_(String(plainPassword), salt);
  PropertiesService.getScriptProperties().setProperties({
    ADMIN_USERNAME: cleanUser,
    ADMIN_PASSWORD_SALT: salt,
    ADMIN_PASSWORD_HASH: hash
  });
  Logger.log('Admin credentials configured securely for username: ' + cleanUser);
  return { ok: true, adminUsername: cleanUser };
}
