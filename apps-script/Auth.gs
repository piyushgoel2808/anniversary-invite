/**
 * Auth.gs — Guest Authentication, Signed Expiring Sessions & Permission Guards
 *
 * NOTE ON APPS SCRIPT SESSION TRANSPORT:
 * Apps Script HTML Service does not allow custom HttpOnly response cookies.
 * Instead, this system issues a server-signed expiring token stored in client
 * sessionStorage/memory and verified cryptographically AND against live
 * GUEST_DATABASE status on every protected server function call.
 */

var GENERIC_AUTH_ERROR = 'Invalid username or mobile number.';

/**
 * Public server endpoint called via google.script.run.guestLogin(username, mobile)
 */
function guestLogin(rawUsername, rawMobile) {
  var username = normalizeUsername_(rawUsername);
  var normalizedMobile = normalizeMobile_(rawMobile);

  if (!username || !normalizedMobile) {
    return { ok: false, error: GENERIC_AUTH_ERROR };
  }

  var throttleKey = 'guest_' + username;
  var throttle = checkLoginThrottle_(throttleKey);
  if (throttle.blocked) {
    return {
      ok: false,
      error: 'Too many failed login attempts. Please try again in ' + throttle.waitMinutes + ' minutes.'
    };
  }

  var guest = findGuestByUsername_(username);

  // Constant-time timing mitigation: always compute hash even when user is absent
  var dummySalt = 'dummy_salt_for_timing_protection_only';
  var computedHash = hashCredential_(normalizedMobile, guest ? guest.salt : dummySalt);
  var validPassword = guest ? constantTimeEquals_(computedHash, guest.mobile_hash) : false;

  if (!guest || !validPassword) {
    recordFailedLogin_(throttleKey);
    return { ok: false, error: GENERIC_AUTH_ERROR };
  }

  if (guest.status !== 'active') {
    return {
      ok: false,
      error: 'This guest account is currently inactive. Please contact the Goel family.'
    };
  }

  clearLoginThrottle_(throttleKey);

  var nowSec = Math.floor(Date.now() / 1000);
  var payload = {
    typ: 'guest',
    gid: guest.guest_id,
    usr: guest.username,
    iat: nowSec,
    exp: nowSec + APP_CONFIG.LIMITS.SESSION_TTL_SECONDS,
    jti: generateOpaqueId_('ses')
  };
  var token = signToken_(payload);

  return {
    ok: true,
    token: token,
    expiresAt: payload.exp * 1000,
    guest: {
      name: guest.guest_name,
      username: guest.username,
      canUpload: Boolean(guest.upload_permission)
    }
  };
}

/**
 * Internal guard called at the start of EVERY protected guest server function.
 * Validates:
 * 1. Token cryptographic signature & expiration & non-revocation
 * 2. Live guest record exists & status === 'active'
 * 3. Optional upload_permission check (throws FORBIDDEN if revoked)
 */
function verifyGuestSession_(sessionToken, requireUploadPermission) {
  var payload = verifyToken_(sessionToken);
  if (!payload || payload.typ !== 'guest' || !payload.gid) {
    throw new Error('UNAUTHORIZED: Your session is invalid or expired. Please sign in again.');
  }

  var guest = findGuestById_(payload.gid);
  if (!guest || guest.status !== 'active') {
    throw new Error('UNAUTHORIZED: Your guest account is inactive. Please contact the family.');
  }

  if (requireUploadPermission && !guest.upload_permission) {
    throw new Error('FORBIDDEN: Upload permission is disabled for your account.');
  }

  return {
    guest: guest,
    tokenPayload: payload
  };
}

/**
 * Public endpoint to validate an existing browser session token on page reload.
 */
function checkGuestSession(sessionToken) {
  try {
    var ctx = verifyGuestSession_(sessionToken, false);
    return {
      ok: true,
      guest: {
        name: ctx.guest.guest_name,
        username: ctx.guest.username,
        canUpload: Boolean(ctx.guest.upload_permission)
      }
    };
  } catch (e) {
    return { ok: false, error: 'Session expired.' };
  }
}

/**
 * Public endpoint to invalidate a guest session token immediately.
 */
function guestLogout(sessionToken) {
  var payload = verifyToken_(sessionToken);
  if (payload && payload.jti) {
    var remaining = payload.exp - Math.floor(Date.now() / 1000);
    revokeToken_(payload.jti, remaining);
  }
  return { ok: true };
}
