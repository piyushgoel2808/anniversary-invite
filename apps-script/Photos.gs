/**
 * Photos.gs — PHOTO_DATABASE Metadata, Pagination & Strictly Private Image Delivery
 *
 * Columns in PHOTO_DATABASE:
 * 0: photo_id | 1: drive_file_id | 2: thumbnail_id | 3: medium_image_id | 4: guest_id
 * 5: original_filename | 6: upload_timestamp | 7: mime_type | 8: file_size | 9: file_hash
 * 10: width | 11: height | 12: status
 */

function getPhotosSheet_() {
  var ss = SpreadsheetApp.openById(getRequiredProp_(APP_CONFIG.PROP_KEYS.SPREADSHEET_ID));
  var sheet = ss.getSheetByName(APP_CONFIG.SHEETS.PHOTOS);
  if (!sheet) {
    throw new Error('PHOTO_DATABASE sheet missing. Run setupInfrastructure() first.');
  }
  return sheet;
}

/**
 * Loads all photo records via single batch read (cached in CacheService when under 95 KB).
 * Drive IDs stay strictly internal to the server.
 */
function loadPhotosIndex_(forceRefresh) {
  if (!forceRefresh) {
    var cached = cacheGetJson_(CACHE_KEYS.PHOTOS_INDEX);
    if (cached && cached.byId) {
      return cached;
    }
  }

  var sheet = getPhotosSheet_();
  var lastRow = sheet.getLastRow();
  var index = { byId: {}, list: [] };

  if (lastRow <= 1) {
    cachePutJson_(CACHE_KEYS.PHOTOS_INDEX, index, 180);
    return index;
  }

  var rows = sheet.getRange(2, 1, lastRow - 1, APP_CONFIG.PHOTO_HEADERS.length).getValues();
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var photoId = String(r[0] || '').trim();
    if (!photoId) continue;

    var rec = {
      rowNumber: i + 2,
      photo_id: photoId,
      drive_file_id: String(r[1] || '').trim(),
      thumbnail_id: String(r[2] || '').trim(),
      medium_image_id: String(r[3] || '').trim(),
      guest_id: String(r[4] || '').trim(),
      original_filename: String(r[5] || '').trim(),
      upload_timestamp: String(r[6] || ''),
      mime_type: String(r[7] || 'image/jpeg'),
      file_size: Number(r[8]) || 0,
      file_hash: String(r[9] || '').trim(),
      width: Number(r[10]) || 0,
      height: Number(r[11]) || 0,
      status: String(r[12] || 'active').trim().toLowerCase()
    };

    index.byId[photoId] = rec;
    index.list.push(rec);
  }

  // Attempt caching (automatically skipped safely if gallery grows > 95 KB metadata)
  cachePutJson_(CACHE_KEYS.PHOTOS_INDEX, index, 180);
  return index;
}

function findActiveDuplicatePhoto_(guestId, fileHash, forceRefresh) {
  if (!guestId || !fileHash) return null;
  var index = loadPhotosIndex_(Boolean(forceRefresh));
  for (var i = 0; i < index.list.length; i++) {
    var p = index.list[i];
    if (p.status === 'active' && p.guest_id === guestId && p.file_hash === fileHash) {
      return p;
    }
  }
  return null;
}

/**
 * Public Guest Endpoint: Paginated Gallery Metadata (20-24 items per page, newest first).
 * NEVER returns Drive file IDs, Drive URLs, or guest mobile credentials.
 */
function listGalleryPhotos(sessionToken, page, pageSize) {
  try {
    verifyGuestSession_(sessionToken, false);

    var safePage = Math.max(1, Math.floor(Number(page) || 1));
    var safeSize = Math.min(
      APP_CONFIG.LIMITS.PAGE_SIZE_MAX,
      Math.max(1, Math.floor(Number(pageSize) || APP_CONFIG.LIMITS.PAGE_SIZE_DEFAULT))
    );

    var photoIndex = loadPhotosIndex_(false);
    var guestIndex = loadGuestsIndex_(false);

    // Filter strictly active photos and order newest-first
    var active = [];
    for (var i = photoIndex.list.length - 1; i >= 0; i--) {
      if (photoIndex.list[i].status === 'active') {
        active.push(photoIndex.list[i]);
      }
    }

    var totalCount = active.length;
    var start = (safePage - 1) * safeSize;
    var end = Math.min(start + safeSize, totalCount);
    var items = [];

    for (var j = start; j < end; j++) {
      var p = active[j];
      var uploader = guestIndex.byId[p.guest_id];
      items.push({
        photo_id: p.photo_id,
        guest_name: uploader ? uploader.guest_name : 'Invited Guest',
        upload_timestamp: p.upload_timestamp,
        width: p.width,
        height: p.height
      });
    }

    return {
      ok: true,
      page: safePage,
      pageSize: safeSize,
      totalCount: totalCount,
      hasMore: end < totalCount,
      photos: items
    };
  } catch (e) {
    return { ok: false, error: e.message || 'Could not load gallery.' };
  }
}

/**
 * Public Guest Endpoint: Private Image Retrieval Proxy
 *
 * STRICT SECURITY RULES:
 * - Requires valid, active guest session token.
 * - Accepts ONLY opaque application photoId (rejects arbitrary Drive IDs).
 * - Guests may ONLY request 'thumbnail' or 'medium'. Requesting 'original' is FORBIDDEN.
 * - Reads private Drive file server-side and returns Base64 data URL (zero Drive URLs/IDs).
 */
function getPrivatePhoto(photoId, sessionToken, variant) {
  try {
    verifyGuestSession_(sessionToken, false);

    var cleanVariant = String(variant || 'thumbnail').toLowerCase();
    if (cleanVariant === 'original') {
      throw new Error('FORBIDDEN: Guests are not permitted to access original files.');
    }
    if (cleanVariant !== 'thumbnail' && cleanVariant !== 'medium') {
      throw new Error('INVALID_VARIANT: Allowed variants are thumbnail and medium.');
    }

    var cleanId = String(photoId || '').trim();
    if (!cleanId || cleanId.indexOf('pho_') !== 0) {
      throw new Error('NOT_FOUND: Invalid photo reference.');
    }

    var photoIndex = loadPhotosIndex_(false);
    var photo = photoIndex.byId[cleanId];
    if (!photo || photo.status !== 'active') {
      throw new Error('NOT_FOUND: Photograph is unavailable.');
    }

    // Serve thumbnail from disposable CacheService when available
    if (cleanVariant === 'thumbnail') {
      var cachedThumb = getCachedThumbnail_(cleanId);
      if (cachedThumb) {
        return { ok: true, photo_id: cleanId, variant: 'thumbnail', dataUrl: cachedThumb, cached: true };
      }
      var thumbData = readPrivateImageAsDataUrl_(photo.thumbnail_id, 'image/webp');
      putCachedThumbnail_(cleanId, thumbData.dataUrl);
      return { ok: true, photo_id: cleanId, variant: 'thumbnail', dataUrl: thumbData.dataUrl, cached: false };
    }

    // Lightbox medium variant (read directly from private Drive)
    var mediumData = readPrivateImageAsDataUrl_(photo.medium_image_id, 'image/webp');
    return { ok: true, photo_id: cleanId, variant: 'medium', dataUrl: mediumData.dataUrl, cached: false };
  } catch (e) {
    return { ok: false, error: e.message || 'Unable to load photograph.' };
  }
}

/**
 * Quota-Optimized Batched Thumbnail Loader (up to 6 thumbnails per single Apps Script execution).
 * Keeps gallery page loads well below the 30-simultaneous-execution consumer quota.
 */
function getPrivatePhotoBatch(photoIds, sessionToken) {
  try {
    verifyGuestSession_(sessionToken, false);

    if (!Array.isArray(photoIds) || photoIds.length === 0) {
      return { ok: true, items: {} };
    }

    var limit = Math.min(photoIds.length, APP_CONFIG.LIMITS.BATCH_THUMB_MAX);
    var photoIndex = loadPhotosIndex_(false);
    var items = {};

    for (var i = 0; i < limit; i++) {
      var id = String(photoIds[i] || '').trim();
      if (!id || id.indexOf('pho_') !== 0) continue;
      var photo = photoIndex.byId[id];
      if (!photo || photo.status !== 'active') continue;

      var cached = getCachedThumbnail_(id);
      if (cached) {
        items[id] = cached;
      } else {
        try {
          var loaded = readPrivateImageAsDataUrl_(photo.thumbnail_id, 'image/webp');
          putCachedThumbnail_(id, loaded.dataUrl);
          items[id] = loaded.dataUrl;
        } catch (imgErr) {
          Logger.log('Batch thumbnail error for ' + id + ': ' + imgErr.message);
        }
      }
    }

    return { ok: true, items: items };
  } catch (e) {
    return { ok: false, error: e.message || 'Batch thumbnail load failed.' };
  }
}

/**
 * Explicit backend permission guards: guests can NEVER delete or modify photos.
 */
function deleteGuestPhoto(photoId, sessionToken) {
  return { ok: false, code: 403, error: 'FORBIDDEN: Guests are not permitted to delete photographs.' };
}

function editGuestPhoto(photoId, updates, sessionToken) {
  return { ok: false, code: 403, error: 'FORBIDDEN: Guests are not permitted to modify photographs.' };
}
