/**
 * Security.gs — Cryptographic Hashing, Signed Session Tokens, Throttling & Magic-Byte Validation
 */

function normalizeUsername_(raw) {
  if (!raw || typeof raw !== 'string') return '';
  return raw.trim().toLowerCase().replace(/\s+/g, '');
}

/**
 * Validates username characters (letters, digits, dot, underscore, hyphen; 3-40 chars).
 */
function isValidUsername_(username) {
  return /^[a-z0-9._-]{3,40}$/.test(username);
}

/**
 * Normalizes mobile numbers consistently for both CSV import and login verification.
 * - Strips spaces, dashes, parentheses, dots, and plus signs.
 * - Normalizes 10-digit Indian numbers (+91XXXXXXXXXX, 91XXXXXXXXXX, 0XXXXXXXXXX -> XXXXXXXXXX).
 * - Preserves valid international numbers (11 to 15 digits).
 */
function normalizeMobile_(raw) {
  if (raw === null || raw === undefined) return '';
  var digits = String(raw).replace(/[^\d]/g, '');
  // Leading 0 on 11-digit Indian number
  if (digits.length === 11 && digits.charAt(0) === '0') {
    digits = digits.substring(1);
  }
  // Indian country code 91 + 10-digit mobile
  if (digits.length === 12 && digits.substring(0, 2) === '91') {
    digits = digits.substring(2);
  }
  // 0091 + 10-digit mobile
  if (digits.length === 14 && digits.substring(0, 4) === '0091') {
    digits = digits.substring(4);
  }
  return digits;
}

function isValidNormalizedMobile_(digits) {
  return /^\d{10,15}$/.test(digits);
}

function generateSalt_() {
  var raw = Utilities.getUuid() + '|' + Utilities.getUuid() + '|' + Date.now();
  var bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, raw, Utilities.Charset.UTF_8);
  return Utilities.base64EncodeWebSafe(bytes);
}

function generateOpaqueId_(prefix) {
  var id = Utilities.getUuid().replace(/-/g, '');
  return (prefix ? prefix + '_' : '') + id;
}

/**
 * Iterated HMAC-SHA-256 with server-side AUTH_PEPPER and per-user random salt.
 * Provides strong offline brute-force resistance for mobile-number credentials inside Apps Script.
 */
function hashCredential_(normalizedSecret, salt) {
  var pepper = getRequiredProp_(APP_CONFIG.PROP_KEYS.AUTH_PEPPER);
  var key = pepper + ':' + salt;
  var current = Utilities.computeHmacSha256Signature(
    normalizedSecret + ':' + salt,
    key,
    Utilities.Charset.UTF_8
  );
  var iterations = APP_CONFIG.LIMITS.HASH_ITERATIONS;
  for (var i = 1; i < iterations; i++) {
    current = Utilities.computeHmacSha256Signature(
      Utilities.base64Encode(current),
      key,
      Utilities.Charset.UTF_8
    );
  }
  return Utilities.base64EncodeWebSafe(current);
}

/**
 * Constant-time string comparison to prevent timing attacks.
 */
function constantTimeEquals_(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string') return false;
  var lenA = a.length;
  var lenB = b.length;
  var diff = lenA ^ lenB;
  var maxLen = Math.max(lenA, lenB);
  for (var i = 0; i < maxLen; i++) {
    var codeA = i < lenA ? a.charCodeAt(i) : 0;
    var codeB = i < lenB ? b.charCodeAt(i) : 0;
    diff |= (codeA ^ codeB);
  }
  return diff === 0;
}

/**
 * Signs a session payload using HMAC-SHA-256 and SESSION_SECRET.
 */
function signToken_(payloadObj) {
  var secret = getRequiredProp_(APP_CONFIG.PROP_KEYS.SESSION_SECRET);
  var payloadJson = JSON.stringify(payloadObj);
  var payloadB64 = Utilities.base64EncodeWebSafe(payloadJson, Utilities.Charset.UTF_8).replace(/=+$/, '');
  var sigBytes = Utilities.computeHmacSha256Signature(payloadB64, secret, Utilities.Charset.UTF_8);
  var sigB64 = Utilities.base64EncodeWebSafe(sigBytes).replace(/=+$/, '');
  return payloadB64 + '.' + sigB64;
}

/**
 * Verifies a signed token's HMAC signature, expiration, and revocation list.
 * Returns decoded payload object or null if invalid/expired/revoked.
 */
function verifyToken_(token) {
  if (!token || typeof token !== 'string') return null;
  var parts = token.split('.');
  if (parts.length !== 2) return null;

  var payloadB64 = parts[0];
  var providedSig = parts[1];
  var secret = getRequiredProp_(APP_CONFIG.PROP_KEYS.SESSION_SECRET);
  var expectedBytes = Utilities.computeHmacSha256Signature(payloadB64, secret, Utilities.Charset.UTF_8);
  var expectedSig = Utilities.base64EncodeWebSafe(expectedBytes).replace(/=+$/, '');

  if (!constantTimeEquals_(providedSig, expectedSig)) {
    return null;
  }

  try {
    var decodedBytes = Utilities.base64DecodeWebSafe(payloadB64);
    var jsonStr = Utilities.newBlob(decodedBytes).getDataAsString('UTF-8');
    var payload = JSON.parse(jsonStr);
    var nowSec = Math.floor(Date.now() / 1000);
    if (!payload || !payload.exp || nowSec >= payload.exp) {
      return null;
    }
    if (payload.jti && isTokenRevoked_(payload.jti)) {
      return null;
    }
    return payload;
  } catch (e) {
    return null;
  }
}

function revokeToken_(jti, remainingSeconds) {
  if (!jti) return;
  var ttl = Math.min(Math.max(Number(remainingSeconds) || 3600, 60), 21600);
  cachePutString_(CACHE_KEYS.REVOKED_PREFIX + jti, '1', ttl);
}

function isTokenRevoked_(jti) {
  return cacheGetString_(CACHE_KEYS.REVOKED_PREFIX + jti) === '1';
}

/**
 * Login throttling & temporary lockout using CacheService.
 */
function checkLoginThrottle_(identifier) {
  var key = CACHE_KEYS.THROTTLE_PREFIX + identifier;
  var state = cacheGetJson_(key);
  if (!state) return { blocked: false, attempts: 0 };
  var nowSec = Math.floor(Date.now() / 1000);
  if (state.lockedUntil && nowSec < state.lockedUntil) {
    var waitMin = Math.ceil((state.lockedUntil - nowSec) / 60);
    return { blocked: true, waitMinutes: waitMin, attempts: state.attempts };
  }
  return { blocked: false, attempts: state.attempts || 0 };
}

function recordFailedLogin_(identifier) {
  var key = CACHE_KEYS.THROTTLE_PREFIX + identifier;
  var state = cacheGetJson_(key) || { attempts: 0, lockedUntil: 0 };
  state.attempts = (state.attempts || 0) + 1;
  if (state.attempts >= APP_CONFIG.LIMITS.LOGIN_MAX_ATTEMPTS) {
    state.lockedUntil = Math.floor(Date.now() / 1000) + APP_CONFIG.LIMITS.LOGIN_LOCKOUT_SECONDS;
  }
  cachePutJson_(key, state, APP_CONFIG.LIMITS.LOGIN_LOCKOUT_SECONDS);
  return state;
}

function clearLoginThrottle_(identifier) {
  cacheRemove_(CACHE_KEYS.THROTTLE_PREFIX + identifier);
}

/**
 * Validates actual binary image magic bytes (JPEG, PNG, WebP only).
 * Rejects PDFs, executables, SVGs, HTML, and spoofed extensions.
 */
function validateImageMagicBytes_(bytes, declaredMime) {
  if (!bytes || bytes.length < 12) {
    throw new Error('INVALID_FILE: File is empty or corrupted.');
  }
  // Apps Script byte arrays are signed (-128..127); convert to unsigned 0..255
  var b = [];
  for (var i = 0; i < 12; i++) {
    b.push((bytes[i] + 256) % 256);
  }

  // JPEG: FF D8 FF
  if (b[0] === 0xFF && b[1] === 0xD8 && b[2] === 0xFF) {
    return 'image/jpeg';
  }
  // PNG: 89 50 4E 47 0D 0A 1A 0A
  if (
    b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4E && b[3] === 0x47 &&
    b[4] === 0x0D && b[5] === 0x0A && b[6] === 0x1A && b[7] === 0x0A
  ) {
    return 'image/png';
  }
  // WebP: RIFF....WEBP
  if (
    b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 &&
    b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50
  ) {
    return 'image/webp';
  }

  throw new Error('UNSUPPORTED_FILE: Only genuine JPEG, PNG, and WebP photographs are allowed.');
}

/**
 * Computes SHA-256 hex digest of raw bytes for duplicate-upload prevention.
 */
function computeBytesSha256Hex_(bytes) {
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes);
  var hex = [];
  for (var i = 0; i < digest.length; i++) {
    var unsigned = (digest[i] + 256) % 256;
    hex.push((unsigned < 16 ? '0' : '') + unsigned.toString(16));
  }
  return hex.join('');
}

function sanitizeFilename_(name) {
  var cleaned = String(name || 'photo')
    .replace(/[/\\:*?"<>|]/g, '_')
    .replace(/\s+/g, '_')
    .replace(/\.{2,}/g, '.')
    .substring(0, 120);
  return cleaned || 'photo';
}
