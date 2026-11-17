/**
 * FaceProfiles.gs — Guest Face Profile Registration, Status & Consent Withdrawal
 *
 * PRIVACY & SECURITY INVARIANTS:
 * 1. Guest identity (guest_id) is ALWAYS derived server-side from verifyGuestSession_().
 *    Browser callers can NEVER supply or override guest_id.
 * 2. Explicit guest consent (consentGranted === true) is mandatory before storing reference photos.
 * 3. Reference images are stored strictly PRIVATE inside ANNIVERSARY_GALLERY/FACE_REFERENCES/<guest_id>/
 *    for the family's local Mac indexer to process — never shared publicly or sent to cloud AI.
 * 4. Withdrawing consent immediately trashes Drive reference images, revokes matches, and signals
 *    the local Mac indexer to delete stored local embeddings on next sync.
 *
 * Columns in FACE_PROFILES:
 * 0: guest_id | 1: status | 2: consent | 3: submitted_at | 4: processed_at | 5: profile_version
 */

function getFaceProfilesSheet_() {
  var ss = SpreadsheetApp.openById(getRequiredProp_(APP_CONFIG.PROP_KEYS.SPREADSHEET_ID));
  return ensureSheetWithHeaders_(ss, APP_CONFIG.SHEETS.FACE_PROFILES, APP_CONFIG.FACE_PROFILE_HEADERS);
}

function getFaceReviewLogSheet_() {
  var ss = SpreadsheetApp.openById(getRequiredProp_(APP_CONFIG.PROP_KEYS.SPREADSHEET_ID));
  return ensureSheetWithHeaders_(ss, APP_CONFIG.SHEETS.FACE_REVIEW_LOG, APP_CONFIG.FACE_REVIEW_LOG_HEADERS);
}

function appendFaceReviewLog_(photoId, guestId, action) {
  var logSheet = getFaceReviewLogSheet_();
  logSheet.appendRow([
    generateOpaqueId_('frev'),
    photoId || '',
    guestId,
    action,
    new Date().toISOString()
  ]);
}

/**
 * Finds or creates private subfolder FACE_REFERENCES/<guest_id>/.
 */
function getOrCreateGuestRefFolder_(guestId, createIfMissing) {
  var rootRefFolderId = getOrInitFaceRefsFolderId_();
  var parent = DriveApp.getFolderById(rootRefFolderId);
  var iter = parent.getFoldersByName(guestId);
  while (iter.hasNext()) {
    var candidate = iter.next();
    if (!candidate.isTrashed()) {
      candidate.setSharing(DriveApp.Access.PRIVATE, DriveApp.Permission.NONE);
      return candidate;
    }
  }
  if (!createIfMissing) return null;
  var created = parent.createFolder(guestId);
  created.setSharing(DriveApp.Access.PRIVATE, DriveApp.Permission.NONE);
  return created;
}

/**
 * Finds the row index (1-based) and parsed record for a guest in FACE_PROFILES.
 */
function findFaceProfileRow_(sheet, guestId) {
  var lastRow = sheet.getLastRow();
  if (lastRow <= 1) return null;
  var rows = sheet.getRange(2, 1, lastRow - 1, APP_CONFIG.FACE_PROFILE_HEADERS.length).getValues();
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i][0] || '').trim() === guestId) {
      return {
        rowNumber: i + 2,
        guest_id: guestId,
        status: String(rows[i][1] || 'NOT_REGISTERED').trim().toUpperCase(),
        consent: String(rows[i][2] || 'NONE').trim().toUpperCase(),
        submitted_at: String(rows[i][3] || ''),
        processed_at: String(rows[i][4] || ''),
        profile_version: Number(rows[i][5]) || 1
      };
    }
  }
  return null;
}

/**
 * Public Guest Endpoint: Fetch authenticated guest's face profile status + photo match counts.
 */
function getMyFaceProfileStatus(sessionToken) {
  try {
    var ctx = verifyGuestSession_(sessionToken, false);
    var guestId = ctx.guest.guest_id;

    var sheet = getFaceProfilesSheet_();
    var rec = findFaceProfileRow_(sheet, guestId);
    var counts = countGuestFaceMatches_(guestId);

    var profile = rec ? {
      status: rec.status,
      consent: rec.consent,
      submitted_at: rec.submitted_at,
      processed_at: rec.processed_at,
      profile_version: rec.profile_version
    } : {
      status: 'NOT_REGISTERED',
      consent: 'NONE',
      submitted_at: '',
      processed_at: '',
      profile_version: 0
    };

    return {
      ok: true,
      profile: profile,
      counts: counts
    };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not load face profile status.' };
  }
}

/**
 * Public Guest Endpoint: Submit 1-4 reference photographs with explicit consent.
 * Payload: { sessionToken, consentGranted: true, images: [{ filename, mimeType, base64 }] }
 */
function registerFaceProfile(payload) {
  var savedFileIds = [];
  var lock = LockService.getScriptLock();
  var lockAcquired = false;

  try {
    if (!payload || typeof payload !== 'object') {
      throw new Error('INVALID_REQUEST: Missing registration payload.');
    }

    var ctx = verifyGuestSession_(payload.sessionToken, false);
    var guestId = ctx.guest.guest_id;

    if (payload.consentGranted !== true) {
      throw new Error('CONSENT_REQUIRED: Explicit consent is required before submitting reference photographs.');
    }

    var images = payload.images;
    if (!Array.isArray(images) || images.length === 0) {
      throw new Error('INVALID_REQUEST: Please select at least 1 clear photograph of yourself.');
    }
    if (images.length > APP_CONFIG.LIMITS.MAX_FACE_REF_PHOTOS) {
      throw new Error('INVALID_REQUEST: Maximum ' + APP_CONFIG.LIMITS.MAX_FACE_REF_PHOTOS + ' reference photographs allowed per submission.');
    }

    // Pre-validate base64 payload size bounds before touching Drive
    for (var i = 0; i < images.length; i++) {
      var img = images[i];
      if (!img || typeof img.base64 !== 'string' || !img.base64.trim()) {
        throw new Error('INVALID_IMAGE_DATA: Reference photograph #' + (i + 1) + ' is empty.');
      }
      if (img.base64.length > APP_CONFIG.LIMITS.MAX_FACE_REF_BASE64_CHARS) {
        throw new Error('FILE_TOO_LARGE: Reference photograph #' + (i + 1) + ' exceeds size limit.');
      }
    }

    var guestFolder = getOrCreateGuestRefFolder_(guestId, true);
    var guestFolderId = guestFolder.getId();
    var tsPrefix = String(Date.now());

    // Validate magic bytes and save each reference photo strictly PRIVATE
    for (var j = 0; j < images.length; j++) {
      var item = images[j];
      var baseName = 'ref_' + tsPrefix + '_' + (j + 1) + '_' + sanitizeFilename_(item.filename || 'selfie.jpg');
      var saved = savePrivateBase64Image_(
        guestFolderId,
        item.base64,
        item.mimeType || 'image/jpeg',
        baseName
      );
      savedFileIds.push(saved.fileId);
    }

    lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
    lockAcquired = true;

    var sheet = getFaceProfilesSheet_();
    var existing = findFaceProfileRow_(sheet, guestId);
    var nowIso = new Date().toISOString();
    var nextVersion = existing ? (existing.profile_version + 1) : 1;

    if (existing) {
      sheet.getRange(existing.rowNumber, 2, 1, 5).setValues([[
        'PENDING_PROCESSING',
        'GRANTED',
        nowIso,
        '',
        nextVersion
      ]]);
    } else {
      sheet.appendRow([
        guestId,
        'PENDING_PROCESSING',
        'GRANTED',
        nowIso,
        '',
        nextVersion
      ]);
    }

    appendFaceReviewLog_('', guestId, 'PROFILE_SUBMITTED_V' + nextVersion);
    invalidateGuestFaceCache_(guestId);

    return {
      ok: true,
      status: 'PENDING_PROCESSING',
      consent: 'GRANTED',
      savedCount: savedFileIds.length,
      profile_version: nextVersion,
      submitted_at: nowIso
    };
  } catch (e) {
    // Atomic rollback of uploaded reference files on failure
    for (var k = 0; k < savedFileIds.length; k++) {
      trashPrivateFile_(savedFileIds[k]);
    }
    return { ok: false, error: e.message || 'Could not submit face profile.' };
  } finally {
    if (lockAcquired) {
      lock.releaseLock();
    }
  }
}

/**
 * Public Guest Endpoint: Withdraw consent and delete reference photos + revoke matches.
 */
function withdrawFaceConsent(sessionToken) {
  var lock = LockService.getScriptLock();
  var lockAcquired = false;

  try {
    var ctx = verifyGuestSession_(sessionToken, false);
    var guestId = ctx.guest.guest_id;

    // Trash any remaining Drive reference images for this guest
    var guestFolder = getOrCreateGuestRefFolder_(guestId, false);
    if (guestFolder) {
      var files = guestFolder.getFiles();
      while (files.hasNext()) {
        var f = files.next();
        if (!f.isTrashed()) {
          f.setTrashed(true);
        }
      }
    }

    lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
    lockAcquired = true;

    var sheet = getFaceProfilesSheet_();
    var existing = findFaceProfileRow_(sheet, guestId);
    var nowIso = new Date().toISOString();

    if (existing) {
      sheet.getRange(existing.rowNumber, 2, 1, 4).setValues([[
        'WITHDRAWN',
        'REVOKED',
        existing.submitted_at,
        nowIso
      ]]);
    } else {
      sheet.appendRow([
        guestId,
        'WITHDRAWN',
        'REVOKED',
        nowIso,
        nowIso,
        1
      ]);
    }

    // Revoke all FACE_MATCHES rows belonging strictly to this guest
    var revokedMatches = revokeGuestFaceMatchesLocked_(guestId, nowIso);
    appendFaceReviewLog_('', guestId, 'CONSENT_WITHDRAWN');
    invalidateGuestFaceCache_(guestId);

    return {
      ok: true,
      status: 'WITHDRAWN',
      consent: 'REVOKED',
      revokedMatches: revokedMatches
    };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not withdraw face consent.' };
  } finally {
    if (lockAcquired) {
      lock.releaseLock();
    }
  }
}
