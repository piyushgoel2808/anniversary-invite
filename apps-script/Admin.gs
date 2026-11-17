/**
 * Admin.gs — Isolated Administrator Authentication & Moderation API
 *
 * SECURITY INVARIANTS:
 * 1. Separate admin credentials stored strictly in Script Properties (ADMIN_USERNAME, ADMIN_PASSWORD_HASH).
 * 2. Issues admin-only signed tokens (typ === 'admin') completely isolated from guest tokens.
 * 3. Guest tokens submitted to ANY admin function are rejected with FORBIDDEN (403).
 */

var GENERIC_ADMIN_AUTH_ERROR = 'Invalid administrator credentials.';

function adminLogin(rawUsername, rawPassword) {
  var username = normalizeUsername_(rawUsername);
  var password = String(rawPassword || '');
  if (!username || !password) {
    return { ok: false, error: GENERIC_ADMIN_AUTH_ERROR };
  }

  var throttleKey = 'admin_' + username;
  var throttle = checkLoginThrottle_(throttleKey);
  if (throttle.blocked) {
    return {
      ok: false,
      error: 'Admin login locked due to failed attempts. Retry in ' + throttle.waitMinutes + ' minutes.'
    };
  }

  var props = PropertiesService.getScriptProperties();
  var storedUser = props.getProperty(APP_CONFIG.PROP_KEYS.ADMIN_USERNAME);
  var storedSalt = props.getProperty(APP_CONFIG.PROP_KEYS.ADMIN_PASSWORD_SALT);
  var storedHash = props.getProperty(APP_CONFIG.PROP_KEYS.ADMIN_PASSWORD_HASH);

  if (!storedUser || !storedSalt || !storedHash) {
    return {
      ok: false,
      error: 'Admin account not configured yet. Run setAdminCredentials() in the Apps Script editor.'
    };
  }

  var computed = hashCredential_(password, storedSalt);
  var userMatch = constantTimeEquals_(username, storedUser);
  var passMatch = constantTimeEquals_(computed, storedHash);

  if (!userMatch || !passMatch) {
    recordFailedLogin_(throttleKey);
    return { ok: false, error: GENERIC_ADMIN_AUTH_ERROR };
  }

  clearLoginThrottle_(throttleKey);

  var nowSec = Math.floor(Date.now() / 1000);
  var payload = {
    typ: 'admin',
    usr: storedUser,
    iat: nowSec,
    exp: nowSec + APP_CONFIG.LIMITS.ADMIN_SESSION_TTL_SECONDS,
    jti: generateOpaqueId_('adm')
  };
  return {
    ok: true,
    token: signToken_(payload),
    adminUsername: storedUser,
    expiresAt: payload.exp * 1000
  };
}

function verifyAdminSession_(adminToken) {
  var payload = verifyToken_(adminToken);
  if (!payload) {
    throw new Error('UNAUTHORIZED: Admin session invalid or expired.');
  }
  if (payload.typ !== 'admin') {
    throw new Error('FORBIDDEN: Administrator privileges required.');
  }
  return payload;
}

function adminCheckSession(adminToken) {
  try {
    var payload = verifyAdminSession_(adminToken);
    return { ok: true, adminUsername: payload.usr };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function adminLogout(adminToken) {
  var payload = verifyToken_(adminToken);
  if (payload && payload.typ === 'admin' && payload.jti) {
    var remaining = payload.exp - Math.floor(Date.now() / 1000);
    revokeToken_(payload.jti, remaining);
  }
  return { ok: true };
}

/**
 * Dashboard summary stats + recent upload activity.
 */
function adminGetDashboard(adminToken) {
  try {
    verifyAdminSession_(adminToken);
    var guests = loadGuestsIndex_(true);
    var photos = loadPhotosIndex_(true);

    var activeGuests = 0;
    var inactiveGuests = 0;
    for (var i = 0; i < guests.list.length; i++) {
      if (guests.list[i].status === 'active') activeGuests++;
      else inactiveGuests++;
    }

    var activePhotos = 0;
    var hiddenPhotos = 0;
    var deletedPhotos = 0;
    var totalBytes = 0;
    var recentUploads = [];

    for (var j = photos.list.length - 1; j >= 0; j--) {
      var p = photos.list[j];
      if (p.status === 'active') {
        activePhotos++;
        totalBytes += p.file_size;
      } else if (p.status === 'hidden') {
        hiddenPhotos++;
        totalBytes += p.file_size;
      } else {
        deletedPhotos++;
      }

      if (recentUploads.length < 15 && p.status !== 'deleted') {
        var g = guests.byId[p.guest_id];
        recentUploads.push({
          photo_id: p.photo_id,
          guest_name: g ? g.guest_name : 'Unknown',
          original_filename: p.original_filename,
          file_size: p.file_size,
          upload_timestamp: p.upload_timestamp,
          status: p.status
        });
      }
    }

    return {
      ok: true,
      stats: {
        totalGuests: guests.list.length,
        activeGuests: activeGuests,
        inactiveGuests: inactiveGuests,
        totalPhotos: activePhotos + hiddenPhotos,
        activePhotos: activePhotos,
        hiddenPhotos: hiddenPhotos,
        deletedPhotos: deletedPhotos,
        totalOriginalBytes: totalBytes
      },
      recentUploads: recentUploads
    };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function adminListGuests(adminToken, searchQuery) {
  try {
    verifyAdminSession_(adminToken);
    return { ok: true, guests: listGuestsForAdmin_(searchQuery) };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function adminImportGuestsCsv(adminToken, csvText) {
  try {
    verifyAdminSession_(adminToken);
    return importGuestsFromCsv_(csvText);
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function adminUpdateGuest(adminToken, guestId, status, uploadPermission) {
  try {
    verifyAdminSession_(adminToken);
    var updates = {};
    if (status === 'active' || status === 'inactive') {
      updates.status = status;
    }
    if (typeof uploadPermission === 'boolean') {
      updates.upload_permission = uploadPermission;
    }
    return updateGuestFlags_(guestId, updates);
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

/**
 * Lists photos for admin moderation with search by guest name/username & status filter.
 */
function adminListPhotos(adminToken, guestSearch, statusFilter, page, pageSize) {
  try {
    verifyAdminSession_(adminToken);
    var photoIndex = loadPhotosIndex_(false);
    var guestIndex = loadGuestsIndex_(false);
    var q = String(guestSearch || '').trim().toLowerCase();
    var statusF = String(statusFilter || 'all').trim().toLowerCase();
    var safePage = Math.max(1, Math.floor(Number(page) || 1));
    var safeSize = Math.min(40, Math.max(1, Math.floor(Number(pageSize) || 20)));

    var filtered = [];
    for (var i = photoIndex.list.length - 1; i >= 0; i--) {
      var p = photoIndex.list[i];
      if (statusF === 'all' && p.status === 'deleted') continue;
      if (statusF !== 'all' && p.status !== statusF) continue;

      var g = guestIndex.byId[p.guest_id];
      var gName = g ? g.guest_name : 'Unknown';
      var gUser = g ? g.username : '';
      if (q && gName.toLowerCase().indexOf(q) === -1 && gUser.toLowerCase().indexOf(q) === -1 && p.original_filename.toLowerCase().indexOf(q) === -1) {
        continue;
      }

      filtered.push({
        photo_id: p.photo_id,
        guest_id: p.guest_id,
        guest_name: gName,
        username: gUser,
        original_filename: p.original_filename,
        upload_timestamp: p.upload_timestamp,
        mime_type: p.mime_type,
        file_size: p.file_size,
        width: p.width,
        height: p.height,
        status: p.status
      });
    }

    var start = (safePage - 1) * safeSize;
    var end = Math.min(start + safeSize, filtered.length);
    return {
      ok: true,
      page: safePage,
      totalCount: filtered.length,
      hasMore: end < filtered.length,
      photos: filtered.slice(start, end)
    };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

/**
 * Admin-only image retrieval supporting thumbnail, medium, AND untouched original download.
 */
function adminGetPhotoVariant(adminToken, photoId, variant) {
  try {
    verifyAdminSession_(adminToken);
    var photoIndex = loadPhotosIndex_(false);
    var photo = photoIndex.byId[String(photoId || '').trim()];
    if (!photo || photo.status === 'deleted') {
      throw new Error('NOT_FOUND: Photo not found.');
    }

    var v = String(variant || 'thumbnail').toLowerCase();
    var targetFileId = photo.thumbnail_id;
    var fallbackMime = 'image/webp';
    if (v === 'medium') {
      targetFileId = photo.medium_image_id;
    } else if (v === 'original') {
      targetFileId = photo.drive_file_id;
      fallbackMime = photo.mime_type;
    }

    var result = readPrivateImageAsDataUrl_(targetFileId, fallbackMime);
    return {
      ok: true,
      photo_id: photo.photo_id,
      variant: v,
      filename: photo.original_filename,
      mimeType: result.mimeType,
      dataUrl: result.dataUrl
    };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

/**
 * Admin photo moderation: hide, restore, or delete (trashes private Drive files).
 */
function adminModeratePhoto(adminToken, photoId, action) {
  try {
    verifyAdminSession_(adminToken);
    var cleanAction = String(action || '').toLowerCase();
    if (cleanAction !== 'hide' && cleanAction !== 'restore' && cleanAction !== 'delete') {
      throw new Error('INVALID_ACTION: Must be hide, restore, or delete.');
    }

    var lock = LockService.getScriptLock();
    lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
    try {
      var index = loadPhotosIndex_(true);
      var photo = index.byId[String(photoId || '').trim()];
      if (!photo) {
        throw new Error('NOT_FOUND: Photo record not found.');
      }

      var newStatus = cleanAction === 'hide' ? 'hidden' : (cleanAction === 'restore' ? 'active' : 'deleted');
      if (cleanAction === 'delete') {
        trashPrivateFile_(photo.drive_file_id);
        trashPrivateFile_(photo.thumbnail_id);
        trashPrivateFile_(photo.medium_image_id);
      }

      var sheet = getPhotosSheet_();
      sheet.getRange(photo.rowNumber, 13).setValue(newStatus);
      SpreadsheetApp.flush();
      invalidatePhotosCache_();
      invalidateThumbnailCache_(photo.photo_id);

      return { ok: true, photo_id: photo.photo_id, status: newStatus };
    } finally {
      lock.releaseLock();
    }
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

/**
 * Exports gallery metadata as CSV, archives a private copy in ADMIN_EXPORTS/, and returns it.
 */
function adminExportMetadataCsv(adminToken) {
  try {
    verifyAdminSession_(adminToken);
    var photos = loadPhotosIndex_(true);
    var guests = loadGuestsIndex_(true);

    var esc = function (v) {
      var s = String(v === null || v === undefined ? '' : v);
      if (s.indexOf(',') !== -1 || s.indexOf('"') !== -1 || s.indexOf('\n') !== -1) {
        return '"' + s.replace(/"/g, '""') + '"';
      }
      return s;
    };

    var lines = [[
      'photo_id', 'guest_id', 'guest_name', 'username',
      'original_filename', 'upload_timestamp', 'mime_type',
      'file_size_bytes', 'width', 'height', 'status'
    ].join(',')];

    for (var i = 0; i < photos.list.length; i++) {
      var p = photos.list[i];
      var g = guests.byId[p.guest_id];
      lines.push([
        esc(p.photo_id),
        esc(p.guest_id),
        esc(g ? g.guest_name : 'Unknown'),
        esc(g ? g.username : ''),
        esc(p.original_filename),
        esc(p.upload_timestamp),
        esc(p.mime_type),
        esc(p.file_size),
        esc(p.width),
        esc(p.height),
        esc(p.status)
      ].join(','));
    }

    var csvText = lines.join('\n');
    var filename = 'silver_jubilee_metadata_' + Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyyMMdd_HHmmss') + '.csv';
    saveAdminExportCsv_(csvText, filename);

    return { ok: true, filename: filename, csvText: csvText, rowCount: photos.list.length };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}
