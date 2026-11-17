/**
 * Guests.gs — GUEST_DATABASE Sheet Operations, Batch Reads, LockService & CSV Importer
 *
 * Columns:
 * 0: guest_id | 1: guest_name | 2: username | 3: mobile_hash | 4: salt | 5: status | 6: upload_permission | 7: created_at
 */

function getGuestsSheet_() {
  var ss = SpreadsheetApp.openById(getRequiredProp_(APP_CONFIG.PROP_KEYS.SPREADSHEET_ID));
  var sheet = ss.getSheetByName(APP_CONFIG.SHEETS.GUESTS);
  if (!sheet) {
    throw new Error('GUEST_DATABASE sheet missing. Run setupInfrastructure_() first.');
  }
  return sheet;
}

/**
 * Loads all guests from CacheService or via a single batch Sheets read.
 * Uses version-checked cache publication so a slow reader that started before an
 * admin deactivation/update can NEVER overwrite the cache with a stale index.
 * Returns { byUsername: {}, byId: {}, list: [] }.
 */
function loadGuestsIndex_(forceRefresh) {
  if (!forceRefresh) {
    var cached = cacheGetJson_(CACHE_KEYS.GUESTS_INDEX);
    if (cached && cached.byUsername) {
      return cached;
    }
  }

  var verBefore = getGuestsCacheVer_();
  var sheet = getGuestsSheet_();
  var lastRow = sheet.getLastRow();
  var index = { byUsername: {}, byId: {}, list: [] };

  if (lastRow <= 1) {
    if (getGuestsCacheVer_() === verBefore) {
      cachePutJson_(CACHE_KEYS.GUESTS_INDEX, index, 300);
    }
    return index;
  }

  var rows = sheet.getRange(2, 1, lastRow - 1, APP_CONFIG.GUEST_HEADERS.length).getValues();
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var guestId = String(row[0] || '').trim();
    var username = normalizeUsername_(String(row[2] || ''));
    if (!guestId || !username) continue;

    var record = {
      rowNumber: i + 2,
      guest_id: guestId,
      guest_name: String(row[1] || '').trim(),
      username: username,
      mobile_hash: String(row[3] || '').trim(),
      salt: String(row[4] || '').trim(),
      status: String(row[5] || 'inactive').trim().toLowerCase(),
      upload_permission: String(row[6]).toLowerCase() === 'true' || row[6] === true,
      created_at: String(row[7] || '')
    };

    index.byUsername[username] = record;
    index.byId[guestId] = record;
    index.list.push(record);
  }

  // Publish to shared cache ONLY if no concurrent guest update occurred during the Sheet read
  if (getGuestsCacheVer_() === verBefore) {
    cachePutJson_(CACHE_KEYS.GUESTS_INDEX, index, 300);
  }
  return index;
}

function findGuestByUsername_(username) {
  var clean = normalizeUsername_(username);
  if (!clean) return null;
  var index = loadGuestsIndex_(false);
  return index.byUsername[clean] || null;
}

function findGuestById_(guestId) {
  if (!guestId) return null;
  var index = loadGuestsIndex_(false);
  return index.byId[String(guestId)] || null;
}

/**
 * Parses standard CSV text (supporting quoted fields and commas inside quotes).
 */
function parseCsvRows_(csvText) {
  var rows = [];
  var currentRow = [];
  var currentVal = '';
  var inQuotes = false;
  var text = String(csvText || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');

  for (var i = 0; i < text.length; i++) {
    var ch = text.charAt(i);
    if (inQuotes) {
      if (ch === '"') {
        if (i + 1 < text.length && text.charAt(i + 1) === '"') {
          currentVal += '"';
          i++;
        } else {
          inQuotes = false;
        }
      } else {
        currentVal += ch;
      }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ',') {
        currentRow.push(currentVal.trim());
        currentVal = '';
      } else if (ch === '\n') {
        currentRow.push(currentVal.trim());
        if (currentRow.some(function (cell) { return cell !== ''; })) {
          rows.push(currentRow);
        }
        currentRow = [];
        currentVal = '';
      } else {
        currentVal += ch;
      }
    }
  }
  currentRow.push(currentVal.trim());
  if (currentRow.some(function (cell) { return cell !== ''; })) {
    rows.push(currentRow);
  }
  return rows;
}

/**
 * Admin-only CSV Guest Importer.
 * Expected CSV header: guest_name,username,mobile[,upload_permission]
 * Normalizes mobile numbers, hashes credentials with random salts + pepper,
 * and discards plaintext mobile numbers immediately.
 */
function importGuestsFromCsv_(csvText) {
  var parsed = parseCsvRows_(csvText);
  if (parsed.length < 2) {
    throw new Error('CSV must contain a header row and at least one guest row.');
  }

  var header = parsed[0].map(function (h) {
    return h.replace(/^\uFEFF/, '').toLowerCase().replace(/\s+/g, '_');
  });
  var nameIdx = header.indexOf('guest_name');
  var userIdx = header.indexOf('username');
  var mobileIdx = header.indexOf('mobile');
  var uploadIdx = header.indexOf('upload_permission');

  if (nameIdx === -1 || userIdx === -1 || mobileIdx === -1) {
    throw new Error('CSV header must include columns: guest_name, username, mobile');
  }

  var lock = LockService.getScriptLock();
  lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
  try {
    var existing = loadGuestsIndex_(true);
    var seenInBatch = {};
    var rowsToAppend = [];
    var skipped = [];
    var errors = [];
    var nowIso = new Date().toISOString();

    for (var i = 1; i < parsed.length; i++) {
      var cols = parsed[i];
      var rowNum = i + 1;
      var guestName = String(cols[nameIdx] || '').trim();
      var username = normalizeUsername_(cols[userIdx] || '');
      var normalizedMobile = normalizeMobile_(cols[mobileIdx] || '');
      var canUpload = true;
      if (uploadIdx !== -1 && cols[uploadIdx] !== undefined && cols[uploadIdx] !== '') {
        var flagStr = String(cols[uploadIdx]).trim().toLowerCase();
        canUpload = !(flagStr === 'false' || flagStr === '0' || flagStr === 'no');
      }

      if (!guestName) {
        errors.push('Row ' + rowNum + ': Missing guest_name.');
        continue;
      }
      if (!isValidUsername_(username)) {
        errors.push('Row ' + rowNum + ': Invalid username "' + username + '" (use 3-40 alphanumeric/._- chars).');
        continue;
      }
      if (!isValidNormalizedMobile_(normalizedMobile)) {
        errors.push('Row ' + rowNum + ': Invalid mobile number for "' + username + '".');
        continue;
      }
      if (existing.byUsername[username] || seenInBatch[username]) {
        skipped.push(username);
        continue;
      }

      seenInBatch[username] = true;
      var salt = generateSalt_();
      var mobileHash = hashCredential_(normalizedMobile, salt);
      // Plaintext mobile is never written to storage
      rowsToAppend.push([
        generateOpaqueId_('gst'),
        guestName,
        username,
        mobileHash,
        salt,
        'active',
        canUpload ? 'true' : 'false',
        nowIso
      ]);
    }

    if (rowsToAppend.length > 0) {
      var sheet = getGuestsSheet_();
      var startRow = sheet.getLastRow() + 1;
      var targetRange = sheet.getRange(startRow, 1, rowsToAppend.length, APP_CONFIG.GUEST_HEADERS.length);
      // Plain-text format prevents Sheets from coercing leading '-' hashes, numeric usernames ('00123'/'1e5'), or ISO dates
      targetRange.setNumberFormat('@');
      targetRange.setValues(rowsToAppend);
      SpreadsheetApp.flush();
      invalidateGuestsCache_();
    }

    return {
      ok: true,
      importedCount: rowsToAppend.length,
      skippedDuplicates: skipped,
      errors: errors
    };
  } finally {
    lock.releaseLock();
  }
}

/**
 * Admin-only helper to update a guest's active status or upload permission.
 * Immediately invalidates cache so revoked guests lose access on next call.
 */
function updateGuestFlags_(guestId, updates) {
  var lock = LockService.getScriptLock();
  lock.waitLock(APP_CONFIG.LIMITS.LOCK_WAIT_MS);
  try {
    var index = loadGuestsIndex_(true);
    var guest = index.byId[guestId];
    if (!guest) {
      throw new Error('NOT_FOUND: Guest account not found.');
    }
    var sheet = getGuestsSheet_();
    if (updates.status === 'active' || updates.status === 'inactive') {
      sheet.getRange(guest.rowNumber, 6).setValue(updates.status);
    }
    if (typeof updates.upload_permission === 'boolean') {
      sheet.getRange(guest.rowNumber, 7).setValue(updates.upload_permission ? 'true' : 'false');
    }
    SpreadsheetApp.flush();
    invalidateGuestsCache_();
    return { ok: true, guest_id: guestId };
  } finally {
    lock.releaseLock();
  }
}

/**
 * Returns sanitized guest records for Admin UI (NEVER exposes mobile_hash or salt).
 */
function listGuestsForAdmin_(searchQuery) {
  var index = loadGuestsIndex_(false);
  var q = String(searchQuery || '').trim().toLowerCase();
  var out = [];
  for (var i = 0; i < index.list.length; i++) {
    var g = index.list[i];
    if (q && g.guest_name.toLowerCase().indexOf(q) === -1 && g.username.toLowerCase().indexOf(q) === -1) {
      continue;
    }
    out.push({
      guest_id: g.guest_id,
      guest_name: g.guest_name,
      username: g.username,
      status: g.status,
      upload_permission: g.upload_permission,
      created_at: g.created_at
    });
  }
  return out;
}
