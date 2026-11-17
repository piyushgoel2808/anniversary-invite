/**
 * Tests.gs — Phase 8 Compulsory Security & Permission Verification Suite
 *
 * Private runner (NEVER exposed via google.script.run).
 * Run via a temporary editor wrapper after setupInfrastructure_() and setAdminCredentials_().
 * Logs a full PASS/FAIL matrix and cleans up its temporary test artifacts automatically.
 */

function runPhase8SecurityTestSuite_() {
  var results = [];
  function assert(name, condition, detail) {
    results.push({ test: name, passed: Boolean(condition), detail: detail || '' });
    if (!condition) {
      Logger.log('FAIL: ' + name + ' -> ' + (detail || ''));
    } else {
      Logger.log('PASS: ' + name);
    }
  }

  // Create isolated test guest
  var testUser = 'sec_test_' + Math.floor(Date.now() / 1000);
  var csv = 'guest_name,username,mobile,upload_permission\nSecurity Tester,' + testUser + ',+91 90000-11111,true';
  var imp = importGuestsFromCsv_(csv);
  assert('1. CSV import normalizes mobile & hashes credential', imp.importedCount === 1);

  var login = guestLogin(testUser, '9000011111');
  assert('2. Registered guest logs in successfully', login.ok && Boolean(login.token));
  var guestToken = login.token;

  // Upload a valid 1x1 PNG via uploadGuestPhotoBase64 (with intentionally spoofed SVG data: prefix on thumb)
  var validPngB64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';
  var upOk = uploadGuestPhotoBase64({
    sessionToken: guestToken,
    originalFilename: 'sec_test.png',
    mimeType: 'image/png',
    originalBase64: validPngB64,
    thumbBase64: 'data:image/svg+xml;base64,' + validPngB64,
    mediumBase64: validPngB64,
    width: 1,
    height: 1
  });
  assert('3. Authenticated guest uploads genuine image', upOk.ok && upOk.photo && upOk.photo.photo_id);
  assert(
    '3b. Spoofed thumbnail data: MIME prefix sanitized to verified PNG',
    upOk.ok && upOk.photo && upOk.photo.thumbDataUrl.indexOf('data:image/png;base64,') === 0
  );
  var photoId = upOk.photo ? upOk.photo.photo_id : '';

  // Duplicate upload prevention
  var dupRes = uploadGuestPhotoBase64({
    sessionToken: guestToken,
    originalFilename: 'sec_test_copy.png',
    mimeType: 'image/png',
    originalBase64: validPngB64,
    thumbBase64: validPngB64,
    mediumBase64: validPngB64,
    width: 1,
    height: 1
  });
  assert('4. Duplicate upload detected without creating duplicate record', dupRes.ok && dupRes.duplicate === true);

  // Valid private thumbnail & medium retrieval
  var thumbRes = getPrivatePhoto(photoId, guestToken, 'thumbnail');
  assert('5. Authenticated guest retrieves private thumbnail data URL', thumbRes.ok && thumbRes.dataUrl.indexOf('data:') === 0);

  // Unauthenticated / copied reference retrieval MUST fail
  var noAuthRes = getPrivatePhoto(photoId, '', 'thumbnail');
  assert('6. Unauthenticated photo retrieval rejected', !noAuthRes.ok && noAuthRes.error.indexOf('UNAUTHORIZED') !== -1);

  // Tampered token MUST fail
  var badTokRes = getPrivatePhoto(photoId, guestToken + 'tampered', 'thumbnail');
  assert('7. Tampered session token rejected', !badTokRes.ok && badTokRes.error.indexOf('UNAUTHORIZED') !== -1);

  // Supplying raw Drive file ID instead of opaque photo_id MUST fail
  var photoRec = loadPhotosIndex_(true).byId[photoId];
  var driveIdAttempt = getPrivatePhoto(photoRec.drive_file_id, guestToken, 'thumbnail');
  assert('8. Arbitrary Drive file ID rejected by photo proxy', !driveIdAttempt.ok);

  // Guest requesting original via display proxy MUST remain FORBIDDEN
  var origAttempt = getPrivatePhoto(photoId, guestToken, 'original');
  assert('9. Guest display proxy blocks original variant (FORBIDDEN)', !origAttempt.ok && origAttempt.error.indexOf('FORBIDDEN') !== -1);

  // Authenticated active guest downloading untouched original via dedicated endpoint succeeds (zero Drive IDs leaked)
  var dlOk = downloadGuestOriginal(photoId, guestToken);
  var dlNoAuth = downloadGuestOriginal(photoId, '');
  var dlRawDriveId = downloadGuestOriginal(photoRec.drive_file_id, guestToken);
  assert(
    '9b. Authenticated guest downloads untouched original with zero Drive IDs exposed',
    dlOk.ok && dlOk.dataUrl.indexOf('data:image/png;base64,') === 0 && !dlOk.drive_file_id
  );
  assert('9c. Unauthenticated original download rejected (UNAUTHORIZED)', !dlNoAuth.ok && dlNoAuth.error.indexOf('UNAUTHORIZED') !== -1);
  assert('9d. Raw Drive file ID rejected by original download endpoint', !dlRawDriveId.ok);

  // Guest attempting delete or edit MUST be FORBIDDEN
  var delAttempt = deleteGuestPhoto(photoId, guestToken);
  var editAttempt = editGuestPhoto(photoId, { status: 'deleted' }, guestToken);
  assert('10. Guest delete photo rejected (403 FORBIDDEN)', !delAttempt.ok && delAttempt.code === 403);
  assert('11. Guest edit photo rejected (403 FORBIDDEN)', !editAttempt.ok && editAttempt.code === 403);

  // Guest token submitted to Admin APIs MUST be FORBIDDEN
  var admDashAttempt = adminGetDashboard(guestToken);
  var admModAttempt = adminModeratePhoto(guestToken, photoId, 'delete');
  var admCsvAttempt = adminImportGuestsCsv(guestToken, csv);
  assert('12. Guest token rejected on admin dashboard (FORBIDDEN)', !admDashAttempt.ok && admDashAttempt.error.indexOf('FORBIDDEN') !== -1);
  assert('13. Guest token rejected on admin photo delete (FORBIDDEN)', !admModAttempt.ok && admModAttempt.error.indexOf('FORBIDDEN') !== -1);
  assert('14. Guest token rejected on admin CSV import (FORBIDDEN)', !admCsvAttempt.ok && admCsvAttempt.error.indexOf('FORBIDDEN') !== -1);

  // Spoofed PDF disguised as .jpg MUST be rejected by magic bytes
  var pdfB64 = Utilities.base64Encode(Utilities.newBlob('%PDF-1.4\n1 0 obj\n<<>>\nendobj\n').getBytes());
  var pdfAttempt = uploadGuestPhotoBase64({
    sessionToken: guestToken,
    originalFilename: 'malicious.jpg',
    mimeType: 'image/jpeg',
    originalBase64: pdfB64,
    thumbBase64: validPngB64,
    mediumBase64: validPngB64,
    width: 100,
    height: 100
  });
  assert('15. Spoofed PDF disguised as JPG rejected by magic bytes', !pdfAttempt.ok && pdfAttempt.error.indexOf('UNSUPPORTED_FILE') !== -1);

  // Spoofed Windows EXE disguised as .jpg MUST be rejected
  var exeB64 = Utilities.base64Encode(Utilities.newBlob('MZ\x90\x00\x03\x00\x00\x00FAKEEXECUTABLE').getBytes());
  var exeAttempt = uploadGuestPhotoBase64({
    sessionToken: guestToken,
    originalFilename: 'virus.jpg',
    mimeType: 'image/jpeg',
    originalBase64: exeB64,
    thumbBase64: validPngB64,
    mediumBase64: validPngB64,
    width: 100,
    height: 100
  });
  assert('16. Fake JPG executable rejected by magic bytes', !exeAttempt.ok && exeAttempt.error.indexOf('UNSUPPORTED_FILE') !== -1);

  // Verify Drive files remain strictly PRIVATE
  var origFile = DriveApp.getFileById(photoRec.drive_file_id);
  assert('17. Original Drive file sharing is strictly PRIVATE', origFile.getSharingAccess() === DriveApp.Access.PRIVATE);

  // Revoking upload_permission blocks uploads (FORBIDDEN) while keeping view access
  var guestRec = findGuestByUsername_(testUser);
  updateGuestFlags_(guestRec.guest_id, { upload_permission: false });
  var noUploadAttempt = uploadGuestPhotoBase64({
    sessionToken: guestToken,
    originalFilename: 'blocked.png',
    mimeType: 'image/png',
    originalBase64: validPngB64,
    thumbBase64: validPngB64,
    mediumBase64: validPngB64,
    width: 1,
    height: 1
  });
  var stillCanView = getPrivatePhoto(photoId, guestToken, 'thumbnail');
  assert('18. Guest without upload_permission blocked from uploading (FORBIDDEN)', !noUploadAttempt.ok && noUploadAttempt.error.indexOf('FORBIDDEN') !== -1);
  assert('19. Guest without upload_permission can still view photos', stillCanView.ok);

  // Deactivating guest immediately revokes active token access
  updateGuestFlags_(guestRec.guest_id, { status: 'inactive' });
  var revokedAttempt = getPrivatePhoto(photoId, guestToken, 'thumbnail');
  assert('20. Deactivated guest immediately loses access', !revokedAttempt.ok && revokedAttempt.error.indexOf('UNAUTHORIZED') !== -1);

  // Durable logout revocation survives CacheService eviction
  updateGuestFlags_(guestRec.guest_id, { status: 'active', upload_permission: true });
  var logoutPayload = verifyToken_(guestToken);
  guestLogout(guestToken);
  if (logoutPayload && logoutPayload.jti) {
    cacheRemove_(CACHE_KEYS.REVOKED_PREFIX + logoutPayload.jti);
    cacheRemove_(CACHE_KEYS.REVOKED_SHARD_PREFIX + revokedShardForJti_(logoutPayload.jti));
  }
  var afterCacheEvict = getPrivatePhoto(photoId, guestToken, 'thumbnail');
  assert('21. Durable logout revocation survives CacheService eviction', !afterCacheEvict.ok && afterCacheEvict.error.indexOf('UNAUTHORIZED') !== -1);
  updateGuestFlags_(guestRec.guest_id, { status: 'inactive' });

  // Cleanup: trash test Drive files & mark test photo deleted
  trashPrivateFile_(photoRec.drive_file_id);
  trashPrivateFile_(photoRec.thumbnail_id);
  trashPrivateFile_(photoRec.medium_image_id);
  var photoSheet = getPhotosSheet_();
  var statusCol = APP_CONFIG.PHOTO_HEADERS.indexOf('status') + 1;
  photoSheet.getRange(photoRec.rowNumber, statusCol).setValue('deleted');
  SpreadsheetApp.flush();
  invalidatePhotosCache_();
  invalidateThumbnailCache_(photoId);

  var failed = results.filter(function (r) { return !r.passed; });
  var summary = {
    totalTests: results.length,
    passedCount: results.length - failed.length,
    failedCount: failed.length,
    allPassed: failed.length === 0,
    results: results
  };
  Logger.log('PHASE 8 SECURITY REPORT:\n' + JSON.stringify(summary, null, 2));
  if (!summary.allPassed) {
    throw new Error('Phase 8 Security Test Suite had ' + failed.length + ' failure(s). Check Logs.');
  }
  return summary;
}

/**
 * FACE RECOGNITION SECURITY & ISOLATION VERIFICATION SUITE (Private — Editor only)
 * Verifies:
 * 1. Explicit consent is required before reference upload
 * 2. Reference photos stored strictly PRIVATE in FACE_REFERENCES/<guest_id>/
 * 3. Pending suggestions appear only in getMyPhotoSuggestions(), never in listMyPhotos()
 * 4. Cross-guest isolation: Guest B cannot see, confirm, or reject Guest A's matches
 * 5. Confirming moves suggestion into listMyPhotos()
 * 6. Withdrawing consent trashes Drive refs, marks profile WITHDRAWN, and revokes matches
 */
function runFaceRecognitionSecurityTests_() {
  var results = [];
  function assert(name, condition, detail) {
    results.push({ test: name, passed: Boolean(condition), detail: detail || '' });
    Logger.log((condition ? 'PASS: ' : 'FAIL: ') + name + (detail ? ' -> ' + detail : ''));
  }

  var ts = Math.floor(Date.now() / 1000);
  var userA = 'face_a_' + ts;
  var userB = 'face_b_' + ts;
  var csv = [
    'guest_name,username,mobile,upload_permission',
    'Face Tester A,' + userA + ',+91 90001-11111,true',
    'Face Tester B,' + userB + ',+91 90002-22222,true'
  ].join('\n');
  importGuestsFromCsv_(csv);

  var tokA = guestLogin(userA, '9000111111').token;
  var tokB = guestLogin(userB, '9000222222').token;
  var guestA = findGuestByUsername_(userA);
  var guestB = findGuestByUsername_(userB);

  var tinyPng = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';

  // Upload a test photo to PHOTO_DATABASE
  var upRes = uploadGuestPhotoBase64({
    sessionToken: tokA,
    originalFilename: 'face_test_album.png',
    mimeType: 'image/png',
    originalBase64: tinyPng,
    thumbBase64: tinyPng,
    mediumBase64: tinyPng,
    width: 1,
    height: 1
  });
  var photoId = upRes.photo.photo_id;

  // 1. Missing consent MUST fail
  var noConsent = registerFaceProfile({
    sessionToken: tokA,
    consentGranted: false,
    images: [{ filename: 'selfie.png', mimeType: 'image/png', base64: tinyPng }]
  });
  assert('F1. Missing consent rejected', !noConsent.ok && noConsent.error.indexOf('CONSENT_REQUIRED') !== -1);

  // 2. Valid face registration with consent succeeds
  var regOk = registerFaceProfile({
    sessionToken: tokA,
    consentGranted: true,
    images: [{ filename: 'selfie.png', mimeType: 'image/png', base64: tinyPng }]
  });
  assert('F2. Valid registration stores reference privately & sets PENDING_PROCESSING', regOk.ok && regOk.status === 'PENDING_PROCESSING' && regOk.savedCount === 1);

  // Simulate Mac indexer writing one PENDING suggestion for Guest A
  var matchSheet = getFaceMatchesSheet_();
  matchSheet.appendRow([photoId, guestA.guest_id, 0.72, 'PENDING', 'yunet_2023mar+sface_2021dec', '', '']);
  SpreadsheetApp.flush();
  invalidateGuestFaceCache_(guestA.guest_id);

  // 3. Guest A sees suggestion in getMyPhotoSuggestions(), NOT in listMyPhotos()
  var sugA = getMyPhotoSuggestions(tokA, 1, 12);
  var myA1 = listMyPhotos(tokA, 1, 24);
  assert('F3. PENDING suggestion visible in getMyPhotoSuggestions()', sugA.ok && sugA.totalCount === 1 && sugA.suggestions[0].photo_id === photoId);
  assert('F4. PENDING suggestion NOT auto-published in listMyPhotos()', myA1.ok && myA1.totalCount === 0);

  // 4. Cross-guest isolation: Guest B cannot see or confirm Guest A's suggestion
  var sugB = getMyPhotoSuggestions(tokB, 1, 12);
  var hijackConfirm = confirmPhotoSuggestion(photoId, tokB);
  assert('F5. Guest B sees zero suggestions belonging to Guest A', sugB.ok && sugB.totalCount === 0);
  assert('F6. Guest B cannot confirm Guest A suggestion', !hijackConfirm.ok && hijackConfirm.error.indexOf('NOT_FOUND') !== -1);

  // 5. Guest A confirms suggestion -> moves to listMyPhotos()
  var confA = confirmPhotoSuggestion(photoId, tokA);
  var sugA2 = getMyPhotoSuggestions(tokA, 1, 12);
  var myA2 = listMyPhotos(tokA, 1, 24);
  assert('F7. Guest A confirms own suggestion', confA.ok && confA.status === 'CONFIRMED');
  assert('F8. Confirmed suggestion leaves pending queue & enters My Photos', sugA2.totalCount === 0 && myA2.totalCount === 1 && myA2.photos[0].photo_id === photoId);

  // 6. Consent withdrawal trashes Drive refs, revokes matches & clears My Photos
  var wdRes = withdrawFaceConsent(tokA);
  var myA3 = listMyPhotos(tokA, 1, 24);
  var profAfter = getMyFaceProfileStatus(tokA);
  assert('F9. Consent withdrawal succeeds & revokes matches', wdRes.ok && wdRes.status === 'WITHDRAWN' && wdRes.revokedMatches >= 1);
  assert('F10. My Photos empty & profile WITHDRAWN after consent withdrawal', myA3.ok && myA3.totalCount === 0 && profAfter.profile.status === 'WITHDRAWN');

  // Cleanup test photo & guests
  var photoRec = loadPhotosIndex_(true).byId[photoId];
  trashPrivateFile_(photoRec.drive_file_id);
  trashPrivateFile_(photoRec.thumbnail_id);
  trashPrivateFile_(photoRec.medium_image_id);
  getPhotosSheet_().getRange(photoRec.rowNumber, APP_CONFIG.PHOTO_HEADERS.indexOf('status') + 1).setValue('deleted');
  updateGuestFlags_(guestA.guest_id, { status: 'inactive' });
  updateGuestFlags_(guestB.guest_id, { status: 'inactive' });
  invalidatePhotosCache_();

  var failed = results.filter(function (r) { return !r.passed; });
  var summary = {
    totalTests: results.length,
    passedCount: results.length - failed.length,
    failedCount: failed.length,
    allPassed: failed.length === 0,
    results: results
  };
  Logger.log('FACE RECOGNITION SECURITY REPORT:\n' + JSON.stringify(summary, null, 2));
  if (!summary.allPassed) {
    throw new Error('Face Recognition Security Tests had ' + failed.length + ' failure(s).');
  }
  return summary;
}
