/**
 * Cache.gs — Quota-Safe Disposable Cache Wrapper (Apps Script CacheService)
 *
 * IMPORTANT APPS SCRIPT CONSTRAINT:
 * CacheService enforces a strict 100 KB limit per key and 6-hour maximum TTL.
 * This module enforces a 95 KB safety ceiling so cache writes never throw exceptions
 * and treats all cached entries as strictly disposable (Sheets & Drive remain source of truth).
 */

var CACHE_KEYS = {
  GUESTS_INDEX: 'v1_guests_index',
  GUESTS_VER: 'v1_guests_ver',
  PHOTOS_INDEX: 'v1_photos_index',
  THUMB_PREFIX: 'v1_thumb_',
  REVOKED_PREFIX: 'v1_revoked_',
  REVOKED_SHARD_PREFIX: 'v1_rev_sh_',
  REVOKED_VER_PREFIX: 'v1_rev_ver_',
  THROTTLE_PREFIX: 'v1_throttle_',
  DOWNLOAD_THROTTLE_PREFIX: 'v1_dl_',
  PROP_PREFIX: 'v1_prop_',
  FACE_VER_PREFIX: 'v1_face_ver_',
  FACE_MATCHES_PREFIX: 'MATCHES_',
  FACE_SUGGESTIONS_PREFIX: 'SUGGESTIONS_',
  FACE_PROFILE_PREFIX: 'v1_fprof_'
};

function cacheGetString_(key) {
  try {
    return CacheService.getScriptCache().get(key);
  } catch (e) {
    return null;
  }
}

function cachePutString_(key, value, ttlSeconds) {
  if (typeof value !== 'string') return false;
  // Guard against Apps Script 100 KB per-key hard limit
  if (value.length > APP_CONFIG.LIMITS.CACHE_MAX_VALUE_BYTES) {
    return false;
  }
  try {
    var clampedTtl = Math.min(Math.max(Number(ttlSeconds) || 300, 30), 21600);
    CacheService.getScriptCache().put(key, value, clampedTtl);
    return true;
  } catch (e) {
    Logger.log('cachePutString_ skipped for ' + key + ': ' + e.message);
    return false;
  }
}

function cacheRemove_(key) {
  try {
    CacheService.getScriptCache().remove(key);
  } catch (e) {
    // Disposable cache — safe to ignore
  }
}

function cacheGetJson_(key) {
  var raw = cacheGetString_(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

function cachePutJson_(key, obj, ttlSeconds) {
  try {
    var serialized = JSON.stringify(obj);
    return cachePutString_(key, serialized, ttlSeconds);
  } catch (e) {
    return false;
  }
}

function getCachedThumbnail_(photoId) {
  return cacheGetString_(CACHE_KEYS.THUMB_PREFIX + photoId);
}

function putCachedThumbnail_(photoId, dataUrl) {
  // Cache small WebP thumbnails (< 95 KB) for 2 hours to save Drive quota
  return cachePutString_(CACHE_KEYS.THUMB_PREFIX + photoId, dataUrl, 7200);
}

function invalidateThumbnailCache_(photoId) {
  cacheRemove_(CACHE_KEYS.THUMB_PREFIX + photoId);
}

function getGuestsCacheVer_() {
  return cacheGetString_(CACHE_KEYS.GUESTS_VER) || '0';
}

function invalidateGuestsCache_() {
  // Bump monotonic version BEFORE removing index so concurrent readers cannot republish stale data
  cachePutString_(CACHE_KEYS.GUESTS_VER, Utilities.getUuid(), 21600);
  cacheRemove_(CACHE_KEYS.GUESTS_INDEX);
}

function invalidatePhotosCache_() {
  cacheRemove_(CACHE_KEYS.PHOTOS_INDEX);
}

function getGuestFaceCacheVer_(guestId) {
  return cacheGetString_(CACHE_KEYS.FACE_VER_PREFIX + guestId) || '0';
}

function invalidateGuestFaceCache_(guestId) {
  if (!guestId) return;
  cachePutString_(CACHE_KEYS.FACE_VER_PREFIX + guestId, Utilities.getUuid().substring(0, 12), 21600);
  cacheRemove_(CACHE_KEYS.FACE_PROFILE_PREFIX + guestId);
}
