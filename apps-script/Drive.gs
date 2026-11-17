/**
 * Drive.gs — Strictly Private Google Drive Operations
 *
 * SECURITY INVARIANTS:
 * 1. Every created file is explicitly locked to DriveApp.Access.PRIVATE / Permission.NONE.
 * 2. Drive file IDs and raw Drive URLs are NEVER returned to guest-facing callers.
 * 3. Only authenticated server functions may invoke readPrivateImageAsDataUrl_().
 */

/**
 * Saves a Blob into a specific private folder and enforces PRIVATE sharing.
 * Returns the internal Drive file ID (for internal PHOTO_DATABASE storage only).
 */
function savePrivateBlob_(folderId, blob, filename) {
  var folder = DriveApp.getFolderById(folderId);
  var safeName = sanitizeFilename_(filename || blob.getName() || 'photo');
  blob.setName(safeName);
  var file = folder.createFile(blob);
  file.setSharing(DriveApp.Access.PRIVATE, DriveApp.Permission.NONE);
  return file.getId();
}

function mimeToExtension_(mime) {
  if (mime === 'image/webp') return '.webp';
  if (mime === 'image/png') return '.png';
  return '.jpg';
}

/**
 * Decodes a Base64 string (with or without data URL prefix), validates image magic bytes,
 * and stores it privately in the target Drive folder.
 * Returns a verified dataUrl constructed strictly from magic-byte-verified MIME type
 * (never preserving an untrusted client-supplied data: prefix).
 */
function savePrivateBase64Image_(folderId, base64Input, declaredMimeType, filenameBase) {
  if (!base64Input || typeof base64Input !== 'string') {
    throw new Error('INVALID_IMAGE_DATA: Empty image payload.');
  }
  var stripped = base64Input.trim();
  var commaIdx = stripped.indexOf(',');
  if (commaIdx !== -1 && stripped.substring(0, commaIdx).indexOf('base64') !== -1) {
    stripped = stripped.substring(commaIdx + 1).trim();
  }
  var bytes = Utilities.base64Decode(stripped);
  var detectedMime = validateImageMagicBytes_(bytes, declaredMimeType);
  var ext = mimeToExtension_(detectedMime);
  var finalName = sanitizeFilename_(filenameBase || 'image');
  if (!/\.(webp|jpg|jpeg|png)$/i.test(finalName)) {
    finalName += ext;
  } else {
    finalName = finalName.replace(/\.(webp|jpg|jpeg|png)$/i, ext);
  }
  var blob = Utilities.newBlob(bytes, detectedMime, finalName);
  var fileId = savePrivateBlob_(folderId, blob, finalName);
  return {
    fileId: fileId,
    mimeType: detectedMime,
    sizeBytes: bytes.length,
    dataUrl: 'data:' + detectedMime + ';base64,' + stripped
  };
}

/**
 * Reads a private Drive image by internal fileId and returns a Base64 data URL.
 * Caller MUST verify authentication and authorization before invoking this helper.
 */
function readPrivateImageAsDataUrl_(fileId, fallbackMimeType) {
  var file = DriveApp.getFileById(fileId);
  if (file.isTrashed()) {
    throw new Error('NOT_FOUND: Image is no longer available.');
  }
  var blob = file.getBlob();
  var mime = blob.getContentType() || fallbackMimeType || 'image/webp';
  var bytes = blob.getBytes();
  var b64 = Utilities.base64Encode(bytes);
  return {
    dataUrl: 'data:' + mime + ';base64,' + b64,
    mimeType: mime,
    sizeBytes: bytes.length
  };
}

/**
 * Moves a private Drive file to trash (used for atomic upload rollback or admin deletion).
 * Returns true if trashed OR if the file is already missing/trashed; returns false on
 * operational Drive errors so callers can retry deletion instead of silently succeeding.
 */
function trashPrivateFile_(fileId) {
  if (!fileId) return true;
  try {
    var file = DriveApp.getFileById(fileId);
    if (!file.isTrashed()) {
      file.setTrashed(true);
    }
    return true;
  } catch (e) {
    var msg = String(e && e.message ? e.message : e);
    if (/no item with the given id|not found|does not exist/i.test(msg)) {
      Logger.log('trashPrivateFile_ already missing for ' + fileId + ': ' + msg);
      return true;
    }
    Logger.log('trashPrivateFile_ operational error for ' + fileId + ': ' + msg);
    return false;
  }
}

/**
 * Saves an admin-only CSV metadata export into ADMIN_EXPORTS/ (strictly private).
 */
function saveAdminExportCsv_(csvContent, filename) {
  var exportsFolderId = getRequiredProp_(APP_CONFIG.PROP_KEYS.EXPORTS_FOLDER_ID);
  var blob = Utilities.newBlob(csvContent, 'text/csv', sanitizeFilename_(filename));
  return savePrivateBlob_(exportsFolderId, blob, filename);
}
