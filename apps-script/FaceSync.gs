/**
 * FaceSync.gs — Admin-Authenticated Sync Endpoints for the Local Mac Face Indexer
 *
 * ZERO-GOOGLE-CLOUD ARCHITECTURE:
 * Allows the local MacBook Pro (`face-indexer sync`) to sync directly with the existing
 * Silver Jubilee Apps Script Web App using the family's existing Admin credentials —
 * requiring zero Google Cloud Console project, zero OAuth client setup, and ₹0 forever.
 *
 * SECURITY INVARIANTS:
 * 1. EVERY endpoint enforces verifyAdminSession_(adminToken). Guest tokens receive FORBIDDEN.
 * 2. Reference file download/delete verifies the target file strictly belongs to
 *    ANNIVERSARY_GALLERY/FACE_REFERENCES/<guest_id>/ before reading or trashing.
 * 3. adminSyncPushMatches() NEVER overwrites CONFIRMED, REJECTED, or ADMIN_CONFIRMED reviews
 *    with PENDING.
 * 4. Face embeddings NEVER reach Apps Script — only metadata (photo_id, guest_id, confidence,
 *    status, model_version) is accepted.
 */

function getPhotoProcessingSheet_() {
  var ss = SpreadsheetApp.openById(getRequiredProp_(APP_CONFIG.PROP_KEYS.SPREADSHEET_ID));
  return ensureSheetWithHeaders_(ss, APP_CONFIG.SHEETS.PHOTO_PROCESSING, APP_CONFIG.PHOTO_PROCESSING_HEADERS);
}

/**
 * Helper: verifies that a Drive fileId is a direct non-trashed child of guestFolder.
 */
function findVerifiedGuestRefFile_(guestId, fileId) {
  var guestFolder = getOrCreateGuestRefFolder_(guestId, false);
  if (!guestFolder) {
    throw new Error('NOT_FOUND: Reference folder not found for guest.');
  }
  var file = DriveApp.getFileById(fileId);
  if (file.isTrashed()) {
    throw new Error('NOT_FOUND: Reference file already trashed.');
  }
  var parents = file.getParents();
  var expectedParentId = guestFolder.getId();
  var belongs = false;
  while (parents.hasNext()) {
    if (parents.next().getId() === expectedParentId) {
      belongs = true;
      break;
    }
  }
  if (!belongs) {
    throw new Error('FORBIDDEN: File does not belong to guest reference folder.');
  }
  return file;
}

/**
 * Admin Sync Endpoint 1: Return active photos, face profiles, and existing face matches.
 */
function adminSyncGetState(adminToken) {
  try {
    verifyAdminSession_(adminToken);

    var photoIndex = loadPhotosIndex_(true);
    var guestIndex = loadGuestsIndex_(true);

    var activePhotos = [];
    for (var i = 0; i < photoIndex.list.length; i++) {
      var p = photoIndex.list[i];
      if (p.status !== 'active') continue;
      activePhotos.push({
        photo_id: p.photo_id,
        drive_file_id: p.photo_id, // Keep opaque photo_id as identifier
        medium_image_id: p.medium_image_id ? p.photo_id : '',
        original_filename: p.original_filename || (p.photo_id + '.jpg'),
        file_hash: p.file_hash || '',
        status: 'ACTIVE'
      });
    }

    var profSheet = getFaceProfilesSheet_();
    var profLastRow = profSheet.getLastRow();
    var profiles = [];
    if (profLastRow > 1) {
      var profRows = profSheet.getRange(2, 1, profLastRow - 1, APP_CONFIG.FACE_PROFILE_HEADERS.length).getValues();
      for (var j = 0; j < profRows.length; j++) {
        var r = profRows[j];
        var gid = String(r[0] || '').trim();
        if (!gid) continue;
        var gRec = guestIndex.byId[gid];
        profiles.push({
          guest_id: gid,
          display_name: gRec ? gRec.guest_name : gid,
          status: String(r[1] || 'PENDING_PROCESSING').trim().toUpperCase(),
          consent: String(r[2] || '').trim().toUpperCase(),
          submitted_at: String(r[3] || ''),
          processed_at: String(r[4] || ''),
          profile_version: Number(r[5]) || 1
        });
      }
    }

    var matchSheet = getFaceMatchesSheet_();
    var matchLastRow = matchSheet.getLastRow();
    var matches = [];
    if (matchLastRow > 1) {
      var matchRows = matchSheet.getRange(2, 1, matchLastRow - 1, APP_CONFIG.FACE_MATCH_HEADERS.length).getValues();
      for (var k = 0; k < matchRows.length; k++) {
        var m = matchRows[k];
        var pid = String(m[0] || '').trim();
        var mgid = String(m[1] || '').trim();
        if (!pid || !mgid) continue;
        matches.push({
          photo_id: pid,
          guest_id: mgid,
          confidence: Number(m[2]) || 0,
          status: String(m[3] || 'PENDING').trim().toUpperCase(),
          model_version: String(m[4] || '').trim(),
          reviewed_at: String(m[5] || ''),
          reviewed_by: String(m[6] || '').trim()
        });
      }
    }

    // Ensure PHOTO_PROCESSING & FACE_REVIEW_LOG exist too
    getPhotoProcessingSheet_();
    getFaceReviewLogSheet_();

    return {
      ok: true,
      photos: activePhotos,
      profiles: profiles,
      matches: matches
    };
  } catch (e) {
    return { ok: false, error: e.message || 'Sync state fetch failed.' };
  }
}

/**
 * Admin Sync Endpoint 2: Download a single active album photo (original or medium) as Base64.
 */
function adminSyncDownloadPhoto(photoId, adminToken, variant) {
  try {
    verifyAdminSession_(adminToken);
    var cleanId = String(photoId || '').trim();
    if (!cleanId || cleanId.indexOf('pho_') !== 0) {
      throw new Error('NOT_FOUND: Invalid photo_id.');
    }
    var photo = loadPhotosIndex_(false).byId[cleanId];
    if (!photo || photo.status !== 'active') {
      throw new Error('NOT_FOUND: Active photo not found.');
    }
    var useMedium = String(variant || 'original').toLowerCase() === 'medium';
    var targetFileId = (useMedium && photo.medium_image_id) ? photo.medium_image_id : photo.drive_file_id;
    var data = readPrivateImageAsDataUrl_(targetFileId, photo.mime_type);
    return {
      ok: true,
      photo_id: cleanId,
      mimeType: data.mimeType,
      sizeBytes: data.sizeBytes,
      dataUrl: data.dataUrl
    };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not download photo for sync.' };
  }
}

/**
 * Admin Sync Endpoint 3: List reference photo files uploaded by a specific guest.
 */
function adminSyncListGuestRefs(guestId, adminToken) {
  try {
    verifyAdminSession_(adminToken);
    var cleanGid = String(guestId || '').trim();
    if (!cleanGid) throw new Error('INVALID_REQUEST: Missing guest_id.');

    var folder = getOrCreateGuestRefFolder_(cleanGid, false);
    if (!folder) {
      return { ok: true, files: [] };
    }

    var files = [];
    var iter = folder.getFiles();
    while (iter.hasNext()) {
      var f = iter.next();
      if (f.isTrashed()) continue;
      files.push({
        file_id: f.getId(),
        filename: f.getName(),
        mime_type: f.getMimeType() || 'image/jpeg'
      });
    }
    return { ok: true, files: files };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not list guest reference files.' };
  }
}

/**
 * Admin Sync Endpoint 4: Download a verified guest reference photo as Base64.
 */
function adminSyncDownloadRef(guestId, fileId, adminToken) {
  try {
    verifyAdminSession_(adminToken);
    var cleanGid = String(guestId || '').trim();
    var cleanFileId = String(fileId || '').trim();
    var file = findVerifiedGuestRefFile_(cleanGid, cleanFileId);
    var data = readPrivateImageAsDataUrl_(file.getId(), file.getMimeType());
    return {
      ok: true,
      file_id: cleanFileId,
      filename: file.getName(),
      mimeType: data.mimeType,
      dataUrl: data.dataUrl
    };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not download reference image.' };
  }
}

/**
 * Admin Sync Endpoint 5: Move a processed guest reference photo to Trash for privacy.
 */
function adminSyncDeleteRef(guestId, fileId, adminToken) {
  try {
    verifyAdminSession_(adminToken);
    var cleanGid = String(guestId || '').trim();
    var cleanFileId = String(fileId || '').trim();
    var file = findVerifiedGuestRefFile_(cleanGid, cleanFileId);
    file.setTrashed(true);
    return { ok: true, deleted: true, file_id: cleanFileId };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not delete reference image.' };
  }
}

/**
 * Admin Sync Endpoint 6: Push new PENDING (or REVOKED) match metadata into FACE_MATCHES.
 * Strictly preserves guest CONFIRMED / REJECTED / ADMIN_CONFIRMED decisions.
 */
function adminSyncPushMatches(matches, adminToken) {
  var lock = LockService.getScriptLock();
  var lockAcquired = false;

  try {
    verifyAdminSession_(adminToken);
    if (!Array.isArray(matches) || matches.length === 0) {
      return { ok: true, pushed: 0 };
    }

    lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
    lockAcquired = true;

    var sheet = getFaceMatchesSheet_();
    var lastRow = sheet.getLastRow();
    var existingMap = {};
    if (lastRow > 1) {
      var rows = sheet.getRange(2, 1, lastRow - 1, APP_CONFIG.FACE_MATCH_HEADERS.length).getValues();
      for (var i = 0; i < rows.length; i++) {
        var key = String(rows[i][0] || '').trim() + '|' + String(rows[i][1] || '').trim();
        existingMap[key] = {
          rowNumber: i + 2,
          status: String(rows[i][3] || 'PENDING').trim().toUpperCase()
        };
      }
    }

    var toAppend = [];
    var affectedGuests = {};
    var pushed = 0;

    for (var j = 0; j < matches.length; j++) {
      var m = matches[j];
      var pid = String(m.photo_id || '').trim();
      var gid = String(m.guest_id || '').trim();
      var status = String(m.status || 'PENDING').trim().toUpperCase();
      if (!pid || !gid) continue;

      var k = pid + '|' + gid;
      if (existingMap[k]) {
        // Never overwrite guest review decisions unless explicitly revoking consent
        if (status === 'REVOKED' && existingMap[k].status !== 'REVOKED') {
          sheet.getRange(existingMap[k].rowNumber, 4).setValue('REVOKED');
          affectedGuests[gid] = true;
          pushed++;
        }
        continue;
      }

      toAppend.push([
        pid,
        gid,
        Math.round((Number(m.confidence) || 0) * 10000) / 10000,
        status,
        String(m.model_version || ''),
        String(m.reviewed_at || ''),
        String(m.reviewed_by || '')
      ]);
      affectedGuests[gid] = true;
      pushed++;
    }

    if (toAppend.length > 0) {
      sheet.getRange(sheet.getLastRow() + 1, 1, toAppend.length, APP_CONFIG.FACE_MATCH_HEADERS.length)
        .setValues(toAppend);
    }

    Object.keys(affectedGuests).forEach(function (g) {
      invalidateGuestFaceCache_(g);
    });

    return { ok: true, pushed: pushed };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not push face matches.' };
  } finally {
    if (lockAcquired) lock.releaseLock();
  }
}

/**
 * Admin Sync Endpoint 7: Update a guest's FACE_PROFILES status (ACTIVE / NEEDS_RETRY / REVOKED).
 */
function adminSyncUpdateProfileStatus(guestId, status, processedAt, adminToken) {
  var lock = LockService.getScriptLock();
  var lockAcquired = false;

  try {
    verifyAdminSession_(adminToken);
    var cleanGid = String(guestId || '').trim();
    var cleanStatus = String(status || '').trim().toUpperCase();
    if (!cleanGid || !cleanStatus) {
      throw new Error('INVALID_REQUEST: guestId and status are required.');
    }

    lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
    lockAcquired = true;

    var sheet = getFaceProfilesSheet_();
    var rec = findFaceProfileRow_(sheet, cleanGid);
    if (!rec) {
      throw new Error('NOT_FOUND: Guest profile row not found.');
    }

    sheet.getRange(rec.rowNumber, 2).setValue(cleanStatus);
    sheet.getRange(rec.rowNumber, 5).setValue(String(processedAt || new Date().toISOString()));
    invalidateGuestFaceCache_(cleanGid);

    return { ok: true, guest_id: cleanGid, status: cleanStatus };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not update profile status.' };
  } finally {
    if (lockAcquired) lock.releaseLock();
  }
}

/**
 * Admin Sync Endpoint 8: Append batch processing telemetry rows to PHOTO_PROCESSING.
 */
function adminSyncLogPhotoProcessing(records, adminToken) {
  var lock = LockService.getScriptLock();
  var lockAcquired = false;

  try {
    verifyAdminSession_(adminToken);
    if (!Array.isArray(records) || records.length === 0) {
      return { ok: true, logged: 0 };
    }

    lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
    lockAcquired = true;

    var sheet = getPhotoProcessingSheet_();
    var rows = [];
    for (var i = 0; i < records.length; i++) {
      var r = records[i];
      rows.push([
        String(r.photo_id || ''),
        String(r.processing_status || 'PROCESSED'),
        String(r.processed_at || new Date().toISOString()),
        String(r.model_version || ''),
        String(r.error || '')
      ]);
    }

    sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, APP_CONFIG.PHOTO_PROCESSING_HEADERS.length)
      .setValues(rows);

    return { ok: true, logged: rows.length };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not log photo processing.' };
  } finally {
    if (lockAcquired) lock.releaseLock();
  }
}
