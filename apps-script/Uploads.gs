/**
 * Uploads.gs — Private Multi-Image Upload Pipeline (Form-to-Blob Original + WebP Derivatives)
 *
 * SECURITY & INTEGRITY GUARANTEES:
 * 1. Authenticated active guest with upload_permission === true required on every upload.
 * 2. Photo is ALWAYS attributed to session guest_id (never trusts frontend guest_id).
 * 3. Original file size strictly capped at APP_CONFIG.LIMITS.MAX_ORIGINAL_BYTES (10 MB).
 * 4. Magic-byte validation enforced server-side for JPEG, PNG, WebP only.
 * 5. SHA-256 content hash prevents duplicate records on retries.
 * 6. Atomic Drive rollback cleans up partial files if any write fails.
 */

/**
 * Primary Upload Entrypoint (HTML <form> Blob transport via google.script.run.uploadGuestPhotoForm(formEl))
 * Avoids Base64 overhead on the original photo while receiving compact Canvas WebP derivatives.
 */
function uploadGuestPhotoForm(formObject) {
  if (!formObject || typeof formObject !== 'object') {
    return { ok: false, error: 'INVALID_REQUEST: Missing upload form data.' };
  }
  try {
    var sessionToken = String(formObject.sessionToken || '');
    var origBlob = formObject.originalFile;
    if (!origBlob || typeof origBlob.getBytes !== 'function') {
      return { ok: false, error: 'INVALID_FILE: Original photograph blob was not received.' };
    }
    var origBytes = origBlob.getBytes();
    var origName = String(formObject.originalFilename || origBlob.getName() || 'photo.jpg');
    var origDeclaredMime = String(origBlob.getContentType() || 'image/jpeg');

    return processValidatedUpload_({
      sessionToken: sessionToken,
      origBytes: origBytes,
      origName: origName,
      origDeclaredMime: origDeclaredMime,
      thumbBase64: String(formObject.thumbBase64 || ''),
      mediumBase64: String(formObject.mediumBase64 || ''),
      width: Number(formObject.width) || 0,
      height: Number(formObject.height) || 0
    });
  } catch (e) {
    return { ok: false, error: e.message || 'Upload failed. Please retry.' };
  }
}

/**
 * Fallback Upload Entrypoint (Base64 transport when older mobile browsers cannot attach DataTransfer files)
 */
function uploadGuestPhotoBase64(payload) {
  if (!payload || typeof payload !== 'object') {
    return { ok: false, error: 'INVALID_REQUEST: Missing upload payload.' };
  }
  try {
    var rawB64 = String(payload.originalBase64 || '');
    var commaIdx = rawB64.indexOf(',');
    if (commaIdx !== -1 && rawB64.substring(0, commaIdx).indexOf('base64') !== -1) {
      rawB64 = rawB64.substring(commaIdx + 1);
    }
    if (!rawB64) {
      return { ok: false, error: 'INVALID_FILE: Original image data is empty.' };
    }
    // Quick pre-decode ceiling (~13.7M chars for 10 MB binary)
    if (rawB64.length > Math.ceil(APP_CONFIG.LIMITS.MAX_ORIGINAL_BYTES * 1.4)) {
      return { ok: false, error: 'FILE_TOO_LARGE: Maximum photo size is 10 MB.' };
    }
    var origBytes = Utilities.base64Decode(rawB64);

    return processValidatedUpload_({
      sessionToken: String(payload.sessionToken || ''),
      origBytes: origBytes,
      origName: String(payload.originalFilename || 'photo.jpg'),
      origDeclaredMime: String(payload.mimeType || 'image/jpeg'),
      thumbBase64: String(payload.thumbBase64 || ''),
      mediumBase64: String(payload.mediumBase64 || ''),
      width: Number(payload.width) || 0,
      height: Number(payload.height) || 0
    });
  } catch (e) {
    return { ok: false, error: e.message || 'Upload failed. Please retry.' };
  }
}

/**
 * Core server-side upload processor shared by both transport paths.
 */
function processValidatedUpload_(input) {
  // 1. Authenticate & enforce upload_permission
  var ctx = verifyGuestSession_(input.sessionToken, true);
  var guest = ctx.guest;

  // 2. Validate original size & actual binary magic bytes
  var origBytes = input.origBytes;
  if (!origBytes || origBytes.length === 0) {
    throw new Error('INVALID_FILE: Uploaded file is empty.');
  }
  if (origBytes.length > APP_CONFIG.LIMITS.MAX_ORIGINAL_BYTES) {
    throw new Error('FILE_TOO_LARGE: Maximum allowed file size is 10 MB.');
  }
  var verifiedOrigMime = validateImageMagicBytes_(origBytes, input.origDeclaredMime);

  // 3. Validate derivative payload size ceilings
  if (!input.thumbBase64 || input.thumbBase64.length > APP_CONFIG.LIMITS.MAX_THUMB_BASE64_CHARS) {
    throw new Error('INVALID_DERIVATIVE: Thumbnail payload missing or exceeds limit.');
  }
  if (!input.mediumBase64 || input.mediumBase64.length > APP_CONFIG.LIMITS.MAX_MEDIUM_BASE64_CHARS) {
    throw new Error('INVALID_DERIVATIVE: Medium image payload missing or exceeds limit.');
  }

  // 4. Compute SHA-256 content hash for deduplication
  var fileHash = computeBytesSha256Hex_(origBytes);
  var photoId = generateOpaqueId_('pho');
  var safeOriginalName = sanitizeFilename_(input.origName);
  var nowIso = new Date().toISOString();
  var width = Math.max(0, Math.min(Math.round(input.width || 0), 20000));
  var height = Math.max(0, Math.min(Math.round(input.height || 0), 20000));

  // Quick duplicate check before touching Drive
  var existingDup = findActiveDuplicatePhoto_(guest.guest_id, fileHash);
  if (existingDup) {
    return {
      ok: true,
      duplicate: true,
      message: 'This photograph is already in the gallery.',
      photo: {
        photo_id: existingDup.photo_id,
        guest_name: guest.guest_name,
        upload_timestamp: existingDup.upload_timestamp,
        width: existingDup.width,
        height: existingDup.height
      }
    };
  }

  var origFolderId = getRequiredProp_(APP_CONFIG.PROP_KEYS.ORIGINAL_FOLDER_ID);
  var thumbFolderId = getRequiredProp_(APP_CONFIG.PROP_KEYS.THUMBNAILS_FOLDER_ID);
  var mediumFolderId = getRequiredProp_(APP_CONFIG.PROP_KEYS.MEDIUM_FOLDER_ID);

  var createdFileIds = [];
  var writtenPhotoRow = null;
  try {
    // 5. Save untouched original privately in ORIGINAL_PHOTOS/
    var origBlob = Utilities.newBlob(origBytes, verifiedOrigMime, photoId + '_' + safeOriginalName);
    var driveFileId = savePrivateBlob_(origFolderId, origBlob, photoId + '_' + safeOriginalName);
    createdFileIds.push(driveFileId);

    // 6. Save WebP/JPEG thumbnail privately in THUMBNAILS/ (extension matches verified MIME)
    var thumbSaved = savePrivateBase64Image_(
      thumbFolderId,
      input.thumbBase64,
      'image/webp',
      photoId + '_thumb'
    );
    createdFileIds.push(thumbSaved.fileId);

    // 7. Save WebP/JPEG medium derivative privately in MEDIUM_IMAGES/
    var mediumSaved = savePrivateBase64Image_(
      mediumFolderId,
      input.mediumBase64,
      'image/webp',
      photoId + '_medium'
    );
    createdFileIds.push(mediumSaved.fileId);

    // 8. Lock & record metadata atomically in PHOTO_DATABASE
    var lock = LockService.getScriptLock();
    lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
    try {
      // Double-check duplicate inside lock for race-condition safety
      var raceDup = findActiveDuplicatePhoto_(guest.guest_id, fileHash, true);
      if (raceDup) {
        // Clean up duplicate Drive uploads cleanly
        for (var d = 0; d < createdFileIds.length; d++) {
          trashPrivateFile_(createdFileIds[d]);
        }
        return {
          ok: true,
          duplicate: true,
          message: 'This photograph is already in the gallery.',
          photo: {
            photo_id: raceDup.photo_id,
            guest_name: guest.guest_name,
            upload_timestamp: raceDup.upload_timestamp,
            width: raceDup.width,
            height: raceDup.height
          }
        };
      }

      var sheet = getPhotosSheet_();
      var literalFilename = /^[=+\-@]/.test(safeOriginalName) ? ("'" + safeOriginalName) : safeOriginalName;
      var row = [
        photoId,
        driveFileId,
        thumbSaved.fileId,
        mediumSaved.fileId,
        guest.guest_id, // Server-attributed guest_id ONLY
        literalFilename,
        nowIso,
        verifiedOrigMime,
        origBytes.length,
        fileHash,
        width,
        height,
        'active'
      ];
      var nextRow = sheet.getLastRow() + 1;
      var rowRange = sheet.getRange(nextRow, 1, 1, APP_CONFIG.PHOTO_HEADERS.length);
      rowRange.setNumberFormat('@');
      rowRange.setValues([row]);
      writtenPhotoRow = nextRow;
      SpreadsheetApp.flush();
      invalidatePhotosCache_();
    } finally {
      lock.releaseLock();
    }

    // 9. Pre-warm disposable thumbnail cache using strictly verified MIME prefix
    var thumbDataUrl = thumbSaved.dataUrl;
    putCachedThumbnail_(photoId, thumbDataUrl);

    return {
      ok: true,
      duplicate: false,
      photo: {
        photo_id: photoId,
        guest_name: guest.guest_name,
        upload_timestamp: nowIso,
        width: width,
        height: height,
        thumbDataUrl: thumbDataUrl
      }
    };
  } catch (err) {
    // Roll back written metadata row BEFORE trashing Drive files so no active row references trashed files
    var rowRollbackOk = true;
    if (writtenPhotoRow !== null) {
      rowRollbackOk = false;
      var rbLock = LockService.getScriptLock();
      var rbLocked = rbLock.tryLock(10000);
      if (rbLocked) {
        try {
          var rbSheet = getPhotosSheet_();
          var currentLast = rbSheet.getLastRow();
          var foundRow = -1;
          if (currentLast >= 2) {
            // Locate exact row by photoId (safe even if another concurrent rollback shifted row indices)
            var idColValues = rbSheet.getRange(2, 1, currentLast - 1, 1).getValues();
            for (var rIdx = idColValues.length - 1; rIdx >= 0; rIdx--) {
              if (String(idColValues[rIdx][0] || '') === photoId) {
                foundRow = rIdx + 2;
                break;
              }
            }
          }
          if (foundRow !== -1) {
            rbSheet.deleteRow(foundRow);
            SpreadsheetApp.flush();
            invalidatePhotosCache_();
          }
          rowRollbackOk = true;
        } catch (rbErr) {
          Logger.log('Upload rollback warning for ' + photoId + ': ' + rbErr.message);
        } finally {
          rbLock.releaseLock();
        }
      }
    }
    if (rowRollbackOk) {
      for (var i = 0; i < createdFileIds.length; i++) {
        trashPrivateFile_(createdFileIds[i]);
      }
    }
    throw err;
  }
}
