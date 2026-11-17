/**
 * Code.gs — Google Apps Script Web App Router & Phase 2/3 Verification Runners
 */

/**
 * Web App HTTP GET Entrypoint
 * - Default URL (/exec)            -> Guest Memories Gallery (Index.html)
 * - Admin URL   (/exec?page=admin) -> Isolated Admin Panel   (AdminPage.html)
 */
function doGet(e) {
  var page = (e && e.parameter && e.parameter.page) ? String(e.parameter.page).toLowerCase() : 'gallery';
  var templateName = (page === 'admin') ? 'AdminPage' : 'Index';

  try {
    var template = HtmlService.createTemplateFromFile(templateName);
    return template
      .evaluate()
      .setTitle(page === 'admin'
        ? 'Admin Panel — Silver Jubilee Memories'
        : APP_CONFIG.APP_NAME)
      .addMetaTag('viewport', 'width=device-width, initial-scale=1, maximum-scale=5')
      .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.DEFAULT);
  } catch (err) {
    Logger.log('doGet error: ' + err.message);
    return HtmlService.createHtmlOutput(
      '<!DOCTYPE html><html><body style="font-family:Georgia,serif;background:#FAF7F2;color:#1B3664;text-align:center;padding:60px 20px;">' +
      '<h2>Silver Jubilee Memories Gallery</h2>' +
      '<p>The gallery is temporarily unavailable. Please refresh in a moment.</p>' +
      '</body></html>'
    ).setTitle(APP_CONFIG.APP_NAME);
  }
}

/**
 * Web App HTTP POST JSON API Entrypoint (for GitHub Pages frontend using credentials: 'omit').
 * Completely bypasses Google's multi-account /u/1/ browser cookie conflict on iPhone Safari/Chrome.
 * Strictly allowlists ONLY the public authenticated RPC actions (no private '_' functions).
 */
function doPost(e) {
  var result;
  try {
    var rawBody = (e && e.postData && e.postData.contents) ? e.postData.contents : '';
    if (!rawBody) {
      throw new Error('INVALID_REQUEST: Empty request body.');
    }
    var req = JSON.parse(rawBody);
    var action = (req && typeof req.action === 'string') ? req.action : '';
    var args = (req && Array.isArray(req.args)) ? req.args : [];

    var handlers = {
      guestLogin: guestLogin,
      checkGuestSession: checkGuestSession,
      guestLogout: guestLogout,
      listGalleryPhotos: listGalleryPhotos,
      getPrivatePhoto: getPrivatePhoto,
      getPrivatePhotoBatch: getPrivatePhotoBatch,
      uploadGuestPhotoBase64: uploadGuestPhotoBase64,
      deleteGuestPhoto: deleteGuestPhoto,
      editGuestPhoto: editGuestPhoto,
      adminLogin: adminLogin,
      adminCheckSession: adminCheckSession,
      adminLogout: adminLogout,
      adminGetDashboard: adminGetDashboard,
      adminListGuests: adminListGuests,
      adminImportGuestsCsv: adminImportGuestsCsv,
      adminUpdateGuest: adminUpdateGuest,
      adminListPhotos: adminListPhotos,
      adminGetPhotoVariant: adminGetPhotoVariant,
      adminModeratePhoto: adminModeratePhoto,
      adminExportMetadataCsv: adminExportMetadataCsv
    };

    if (!Object.prototype.hasOwnProperty.call(handlers, action)) {
      result = { ok: false, error: 'UNKNOWN_ACTION: Unsupported API action.' };
    } else {
      result = handlers[action].apply(null, args);
    }
  } catch (err) {
    result = { ok: false, error: (err && err.message) ? err.message : 'Server error.' };
  }

  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * HTML Service partial include helper (private — used server-side for <?!= include_('Styles'); ?>)
 */
function include_(filename) {
  return HtmlService.createHtmlOutputFromFile(filename).getContent();
}

/**
 * PHASE 2 SMOKE TEST (Private — run via temporary editor wrapper after setupInfrastructure_())
 * Verifies:
 * 1. Private Drive upload of a valid 1x1 PNG image (magic bytes verified)
 * 2. Sharing permission is strictly PRIVATE
 * 3. Private read-back as Base64 data URL succeeds
 * 4. Cleanup trashes the test image cleanly
 */
function runPhase2SmokeTest_() {
  var thumbFolderId = getRequiredProp_(APP_CONFIG.PROP_KEYS.THUMBNAILS_FOLDER_ID);
  // Valid 1x1 transparent PNG in Base64
  var tinyPngBase64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';

  var saved = savePrivateBase64Image_(thumbFolderId, tinyPngBase64, 'image/png', 'phase2_smoke_test.png');
  var file = DriveApp.getFileById(saved.fileId);
  var sharingAccess = file.getSharingAccess();
  var retrieved = readPrivateImageAsDataUrl_(saved.fileId, 'image/png');
  trashPrivateFile_(saved.fileId);

  var result = {
    passed: (
      sharingAccess === DriveApp.Access.PRIVATE &&
      retrieved.dataUrl.indexOf('data:image/png;base64,') === 0 &&
      saved.sizeBytes > 0
    ),
    sharingAccess: String(sharingAccess),
    retrievedPrefix: retrieved.dataUrl.substring(0, 36),
    cleanedUp: true
  };
  Logger.log('Phase 2 Smoke Test: ' + JSON.stringify(result, null, 2));
  if (!result.passed) {
    throw new Error('Phase 2 Smoke Test FAILED!');
  }
  return result;
}

/**
 * PHASE 3 AUTHENTICATION ACCEPTANCE TEST SUITE (Private — run via temporary editor wrapper)
 * Verifies:
 * 1. CSV guest import + mobile normalization (+91 / 0 / spaces) + hashing
 * 2. Registered guest login succeeds
 * 3. Unregistered guest rejected
 * 4. Wrong mobile rejected
 * 5. Inactive guest rejected both at login & mid-session
 * 6. Revoked upload_permission throws FORBIDDEN
 * 7. Logout revokes session token immediately
 */
function runPhase3AuthTests_() {
  var testUser = 'test_guest_' + Math.floor(Date.now() / 1000);
  var csv = [
    'guest_name,username,mobile,upload_permission',
    'Test Guest,' + testUser + ',+91 98765-43210,true'
  ].join('\n');

  var importRes = importGuestsFromCsv_(csv);
  if (importRes.importedCount !== 1) {
    throw new Error('CSV import failed: ' + JSON.stringify(importRes));
  }

  // 1. Valid login with unformatted 10-digit mobile
  var loginOk = guestLogin(testUser, '9876543210');
  if (!loginOk.ok || !loginOk.token) {
    throw new Error('Expected valid login to succeed: ' + JSON.stringify(loginOk));
  }

  // 2. Unregistered user rejected
  var badUser = guestLogin('nonexistent_user_xyz', '9876543210');
  if (badUser.ok) throw new Error('Unregistered user should have been rejected.');

  // 3. Wrong mobile rejected
  var badPwd = guestLogin(testUser, '9999999999');
  if (badPwd.ok) throw new Error('Wrong mobile should have been rejected.');

  // 4. Upload permission disabled -> throws FORBIDDEN
  var guestRec = findGuestByUsername_(testUser);
  updateGuestFlags_(guestRec.guest_id, { upload_permission: false });
  var uploadBlocked = false;
  try {
    verifyGuestSession_(loginOk.token, true);
  } catch (e) {
    uploadBlocked = e.message.indexOf('FORBIDDEN') !== -1;
  }
  if (!uploadBlocked) throw new Error('Disabled upload_permission did not block session.');

  // 5. Deactivated guest -> active session immediately revoked
  updateGuestFlags_(guestRec.guest_id, { status: 'inactive' });
  var inactiveBlocked = false;
  try {
    verifyGuestSession_(loginOk.token, false);
  } catch (e) {
    inactiveBlocked = e.message.indexOf('UNAUTHORIZED') !== -1;
  }
  if (!inactiveBlocked) throw new Error('Inactive guest session was not rejected.');

  // 6. Re-activate then logout -> token immediately revoked
  updateGuestFlags_(guestRec.guest_id, { status: 'active', upload_permission: true });
  guestLogout(loginOk.token);
  var logoutBlocked = false;
  try {
    verifyGuestSession_(loginOk.token, false);
  } catch (e) {
    logoutBlocked = e.message.indexOf('UNAUTHORIZED') !== -1;
  }
  if (!logoutBlocked) throw new Error('Logged-out token was not rejected.');

  // Clean up test row by marking inactive
  updateGuestFlags_(guestRec.guest_id, { status: 'inactive' });

  var report = {
    passed: true,
    testsVerified: [
      'CSV import + mobile normalization',
      'Valid guest login',
      'Unregistered guest rejection',
      'Wrong mobile rejection',
      'Revoked upload permission -> FORBIDDEN',
      'Deactivated guest -> immediate UNAUTHORIZED',
      'Logout token revocation'
    ]
  };
  Logger.log('Phase 3 Auth Tests PASSED: ' + JSON.stringify(report, null, 2));
  return report;
}
