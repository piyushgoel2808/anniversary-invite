/**
 * Tests.gs — Phase 8 Compulsory Security & Permission Verification Suite
 *
 * Run runPhase8SecurityTestSuite() directly inside the Apps Script Editor after
 * setupInfrastructure() and setAdminCredentials() have been executed.
 * Logs a full PASS/FAIL matrix and cleans up its temporary test artifacts automatically.
 */

function runPhase8SecurityTestSuite() {
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

  // Upload a valid 1x1 PNG via uploadGuestPhotoBase64
  var validPngB64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';
  var upOk = uploadGuestPhotoBase64({
    sessionToken: guestToken,
    originalFilename: 'sec_test.png',
    mimeType: 'image/png',
    originalBase64: validPngB64,
    thumbBase64: validPngB64,
    mediumBase64: validPngB64,
    width: 1,
    height: 1
  });
  assert('3. Authenticated guest uploads genuine image', upOk.ok && upOk.photo && upOk.photo.photo_id);
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

  // Guest requesting original variant MUST be FORBIDDEN
  var origAttempt = getPrivatePhoto(photoId, guestToken, 'original');
  assert('9. Guest blocked from accessing original photo (FORBIDDEN)', !origAttempt.ok && origAttempt.error.indexOf('FORBIDDEN') !== -1);

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

  // Deactivating guest immediately revokes active token access
  var guestRec = findGuestByUsername_(testUser);
  updateGuestFlags_(guestRec.guest_id, { status: 'inactive' });
  var revokedAttempt = getPrivatePhoto(photoId, guestToken, 'thumbnail');
  assert('18. Deactivated guest immediately loses access', !revokedAttempt.ok && revokedAttempt.error.indexOf('UNAUTHORIZED') !== -1);

  // Cleanup: trash test Drive files & mark test photo deleted
  trashPrivateFile_(photoRec.drive_file_id);
  trashPrivateFile_(photoRec.thumbnail_id);
  trashPrivateFile_(photoRec.medium_image_id);
  var photoSheet = getPhotosSheet_();
  photoSheet.getRange(photoRec.rowNumber, 13).setValue('deleted');
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
