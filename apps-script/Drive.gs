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

/**
 * Decodes a Base64 string (with or without data URL prefix), validates image magic bytes,
 * and stores it privately in the target Drive folder.
 */
function savePrivateBase64Image_(folderId, base64Input, declaredMimeType, filename) {
  if (!base64Input || typeof base64Input !== 'string') {
    throw new Error('INVALID_IMAGE_DATA: Empty image payload.');
  }
  var stripped = base64Input;
  var commaIdx = stripped.indexOf(',');
  if (commaIdx !== -1 && stripped.substring(0, commaIdx).indexOf('base64') !== -1) {
    stripped = stripped.substring(commaIdx + 1);
  }
  var bytes = Utilities.base64Decode(stripped);
  var detectedMime = validateImageMagicBytes_(bytes, declaredMimeType);
  var blob = Utilities.newBlob(bytes, detectedMime, sanitizeFilename_(filename));
  var fileId = savePrivateBlob_(folderId, blob, filename);
  return {
    fileId: fileId,
    mimeType: detectedMime,
    sizeBytes: bytes.length
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
 * Swallows missing-file errors safely during cleanup.
 */
function trashPrivateFile_(fileId) {
  if (!fileId) return false;
  try {
    var file = DriveApp.getFileById(fileId);
    file.setTrashed(true);
    return true;
  } catch (e) {
    Logger.log('trashPrivateFile_ notice for ' + fileId + ': ' + e.message);
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
