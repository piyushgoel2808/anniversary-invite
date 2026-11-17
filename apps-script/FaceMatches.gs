/**
 * FaceMatches.gs — Authenticated Guest Personal Album ("My Photos") & Suggestion Review
 *
 * PRIVACY & AUTHORIZATION INVARIANTS:
 * 1. Guest identity (guest_id) is ALWAYS derived server-side from verifyGuestSession_().
 *    A guest can NEVER view, confirm, or reject another guest's matches or suggestions.
 * 2. listMyPhotos() returns ONLY CONFIRMED or ADMIN_CONFIRMED matches for the logged-in guest.
 * 3. getMyPhotoSuggestions() returns ONLY PENDING suggestions for the logged-in guest.
 * 4. Only photos that remain 'active' in PHOTO_DATABASE are returned; Drive file IDs are NEVER exposed.
 * 5. Cache keys are strictly scoped per guest and versioned (MATCHES_<guest_id>_<ver>_<page>).
 *
 * Columns in FACE_MATCHES:
 * 0: photo_id | 1: guest_id | 2: confidence | 3: status | 4: model_version | 5: reviewed_at | 6: reviewed_by
 */

function getFaceMatchesSheet_() {
  var ss = SpreadsheetApp.openById(getRequiredProp_(APP_CONFIG.PROP_KEYS.SPREADSHEET_ID));
  return ensureSheetWithHeaders_(ss, APP_CONFIG.SHEETS.FACE_MATCHES, APP_CONFIG.FACE_MATCH_HEADERS);
}

/**
 * Loads all FACE_MATCHES rows belonging strictly to guestId via a single batch read.
 */
function loadMatchesForGuest_(guestId) {
  var sheet = getFaceMatchesSheet_();
  var lastRow = sheet.getLastRow();
  var result = [];
  if (lastRow <= 1) return result;

  var rows = sheet.getRange(2, 1, lastRow - 1, APP_CONFIG.FACE_MATCH_HEADERS.length).getValues();
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    if (String(r[1] || '').trim() !== guestId) continue;
    var photoId = String(r[0] || '').trim();
    if (!photoId) continue;

    result.push({
      rowNumber: i + 2,
      photo_id: photoId,
      guest_id: guestId,
      confidence: Number(r[2]) || 0,
      status: String(r[3] || 'PENDING').trim().toUpperCase(),
      model_version: String(r[4] || '').trim(),
      reviewed_at: String(r[5] || ''),
      reviewed_by: String(r[6] || '').trim()
    });
  }
  return result;
}

/**
 * Counts active confirmed and pending matches for a single guest.
 */
function countGuestFaceMatches_(guestId) {
  var matches = loadMatchesForGuest_(guestId);
  var photoIndex = loadPhotosIndex_(false);
  var confirmedCount = 0;
  var pendingCount = 0;

  for (var i = 0; i < matches.length; i++) {
    var m = matches[i];
    var photo = photoIndex.byId[m.photo_id];
    if (!photo || photo.status !== 'active') continue;
    if (m.status === 'CONFIRMED' || m.status === 'ADMIN_CONFIRMED') {
      confirmedCount++;
    } else if (m.status === 'PENDING') {
      pendingCount++;
    }
  }

  return {
    confirmedCount: confirmedCount,
    pendingCount: pendingCount
  };
}

/**
 * Internal helper called under ScriptLock when a guest withdraws face consent.
 */
function revokeGuestFaceMatchesLocked_(guestId, nowIso) {
  var sheet = getFaceMatchesSheet_();
  var lastRow = sheet.getLastRow();
  if (lastRow <= 1) return 0;

  var rows = sheet.getRange(2, 1, lastRow - 1, APP_CONFIG.FACE_MATCH_HEADERS.length).getValues();
  var revoked = 0;
  var changed = false;

  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i][1] || '').trim() === guestId) {
      var curStatus = String(rows[i][3] || '').trim().toUpperCase();
      if (curStatus !== 'REVOKED') {
        rows[i][3] = 'REVOKED';
        rows[i][5] = nowIso;
        rows[i][6] = guestId;
        revoked++;
        changed = true;
      }
    }
  }

  if (changed) {
    sheet.getRange(2, 1, rows.length, APP_CONFIG.FACE_MATCH_HEADERS.length).setValues(rows);
  }
  return revoked;
}

/**
 * Public Guest Endpoint: Paginated Confirmed Personal Album ("My Photos").
 * Returns ONLY active photographs confirmed for the authenticated guest.
 */
function listMyPhotos(sessionToken, page, pageSize) {
  try {
    var ctx = verifyGuestSession_(sessionToken, false);
    var guestId = ctx.guest.guest_id;

    var safePage = Math.max(1, Math.floor(Number(page) || 1));
    var safeSize = Math.min(
      APP_CONFIG.LIMITS.PAGE_SIZE_MAX,
      Math.max(1, Math.floor(Number(pageSize) || APP_CONFIG.LIMITS.PAGE_SIZE_DEFAULT))
    );

    var ver = getGuestFaceCacheVer_(guestId);
    var cacheKey = CACHE_KEYS.FACE_MATCHES_PREFIX + guestId + '_' + ver + '_' + safePage + '_' + safeSize;
    var cached = cacheGetJson_(cacheKey);
    if (cached && cached.ok) {
      return cached;
    }

    var matches = loadMatchesForGuest_(guestId);
    var confirmedMap = {};
    for (var i = 0; i < matches.length; i++) {
      var st = matches[i].status;
      if (st === 'CONFIRMED' || st === 'ADMIN_CONFIRMED') {
        confirmedMap[matches[i].photo_id] = matches[i];
      }
    }

    var photoIndex = loadPhotosIndex_(false);
    var guestIndex = loadGuestsIndex_(false);

    // Order newest-first using PHOTO_DATABASE order
    var activeConfirmed = [];
    for (var k = photoIndex.list.length - 1; k >= 0; k--) {
      var p = photoIndex.list[k];
      if (p.status === 'active' && confirmedMap[p.photo_id]) {
        activeConfirmed.push(p);
      }
    }

    var totalCount = activeConfirmed.length;
    var start = (safePage - 1) * safeSize;
    var end = Math.min(start + safeSize, totalCount);
    var items = [];

    for (var j = start; j < end; j++) {
      var photo = activeConfirmed[j];
      var uploader = guestIndex.byId[photo.guest_id];
      items.push({
        photo_id: photo.photo_id,
        guest_name: uploader ? uploader.guest_name : 'Invited Guest',
        upload_timestamp: photo.upload_timestamp,
        width: photo.width,
        height: photo.height,
        match_status: confirmedMap[photo.photo_id].status
      });
    }

    var response = {
      ok: true,
      page: safePage,
      pageSize: safeSize,
      totalCount: totalCount,
      hasMore: end < totalCount,
      photos: items
    };
    cachePutJson_(cacheKey, response, 120);
    return response;
  } catch (e) {
    return { ok: false, error: e.message || 'Could not load your personal photographs.' };
  }
}

/**
 * Public Guest Endpoint: Paginated Pending Suggestions awaiting this guest's confirmation.
 * Ordered highest-confidence first.
 */
function getMyPhotoSuggestions(sessionToken, page, pageSize) {
  try {
    var ctx = verifyGuestSession_(sessionToken, false);
    var guestId = ctx.guest.guest_id;

    var safePage = Math.max(1, Math.floor(Number(page) || 1));
    var safeSize = Math.min(
      APP_CONFIG.LIMITS.PAGE_SIZE_MAX,
      Math.max(1, Math.floor(Number(pageSize) || 12))
    );

    var ver = getGuestFaceCacheVer_(guestId);
    var cacheKey = CACHE_KEYS.FACE_SUGGESTIONS_PREFIX + guestId + '_' + ver + '_' + safePage + '_' + safeSize;
    var cached = cacheGetJson_(cacheKey);
    if (cached && cached.ok) {
      return cached;
    }

    var matches = loadMatchesForGuest_(guestId);
    var photoIndex = loadPhotosIndex_(false);
    var guestIndex = loadGuestsIndex_(false);

    var pending = [];
    for (var i = 0; i < matches.length; i++) {
      var m = matches[i];
      if (m.status !== 'PENDING') continue;
      var photo = photoIndex.byId[m.photo_id];
      if (!photo || photo.status !== 'active') continue;
      pending.push({ match: m, photo: photo });
    }

    // Sort highest confidence first
    pending.sort(function (a, b) {
      return b.match.confidence - a.match.confidence;
    });

    var totalCount = pending.length;
    var start = (safePage - 1) * safeSize;
    var end = Math.min(start + safeSize, totalCount);
    var items = [];

    for (var j = start; j < end; j++) {
      var entry = pending[j];
      var uploader = guestIndex.byId[entry.photo.guest_id];
      items.push({
        photo_id: entry.photo.photo_id,
        guest_name: uploader ? uploader.guest_name : 'Invited Guest',
        upload_timestamp: entry.photo.upload_timestamp,
        width: entry.photo.width,
        height: entry.photo.height,
        confidence: Math.round(entry.match.confidence * 1000) / 1000,
        status: 'PENDING'
      });
    }

    var response = {
      ok: true,
      page: safePage,
      pageSize: safeSize,
      totalCount: totalCount,
      hasMore: end < totalCount,
      suggestions: items
    };
    cachePutJson_(cacheKey, response, 120);
    return response;
  } catch (e) {
    return { ok: false, error: e.message || 'Could not load photo suggestions.' };
  }
}

/**
 * Public Guest Endpoint: Confirm or Reject a suggested (or confirmed) match for the logged-in guest.
 * Decision MUST be 'CONFIRMED' or 'REJECTED'.
 */
function reviewPhotoSuggestion(photoId, decision, sessionToken) {
  var lock = LockService.getScriptLock();
  var lockAcquired = false;

  try {
    var ctx = verifyGuestSession_(sessionToken, false);
    var guestId = ctx.guest.guest_id;

    var cleanPhotoId = String(photoId || '').trim();
    if (!cleanPhotoId || cleanPhotoId.indexOf('pho_') !== 0) {
      throw new Error('NOT_FOUND: Invalid photo reference.');
    }

    var cleanDecision = String(decision || '').trim().toUpperCase();
    if (cleanDecision !== 'CONFIRMED' && cleanDecision !== 'REJECTED') {
      throw new Error('INVALID_DECISION: Decision must be CONFIRMED or REJECTED.');
    }

    lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
    lockAcquired = true;

    var sheet = getFaceMatchesSheet_();
    var lastRow = sheet.getLastRow();
    if (lastRow <= 1) {
      throw new Error('NOT_FOUND: No matching suggestion found for your account.');
    }

    var rows = sheet.getRange(2, 1, lastRow - 1, APP_CONFIG.FACE_MATCH_HEADERS.length).getValues();
    var targetRowNumber = -1;
    var currentStatus = '';

    for (var i = 0; i < rows.length; i++) {
      var rPhotoId = String(rows[i][0] || '').trim();
      var rGuestId = String(rows[i][1] || '').trim();
      if (rPhotoId === cleanPhotoId && rGuestId === guestId) {
        targetRowNumber = i + 2;
        currentStatus = String(rows[i][3] || '').trim().toUpperCase();
        break;
      }
    }

    if (targetRowNumber === -1) {
      throw new Error('NOT_FOUND: No matching suggestion found for your account.');
    }

    if (currentStatus === 'REVOKED') {
      throw new Error('FORBIDDEN: This match was revoked after consent withdrawal.');
    }

    var nowIso = new Date().toISOString();
    sheet.getRange(targetRowNumber, 4, 1, 1).setValue(cleanDecision);
    sheet.getRange(targetRowNumber, 6, 1, 2).setValues([[nowIso, guestId]]);

    appendFaceReviewLog_(cleanPhotoId, guestId, cleanDecision);
    invalidateGuestFaceCache_(guestId);

    return {
      ok: true,
      photo_id: cleanPhotoId,
      status: cleanDecision,
      reviewed_at: nowIso
    };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not update photo review.' };
  } finally {
    if (lockAcquired) {
      lock.releaseLock();
    }
  }
}

/**
 * Convenience public aliases matching architecture specification.
 */
function confirmPhotoSuggestion(photoId, sessionToken) {
  return reviewPhotoSuggestion(photoId, 'CONFIRMED', sessionToken);
}

function rejectPhotoSuggestion(photoId, sessionToken) {
  return reviewPhotoSuggestion(photoId, 'REJECTED', sessionToken);
}
