#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
unset PYTHONPATH

BIN="$SCRIPT_DIR/.venv/bin/face-indexer"
if [ ! -x "$BIN" ]; then
  osascript -e 'display alert "Face Sync Not Installed" message "Virtual environment not found. Run: cd face-indexer && uv sync" as critical'
  exit 1
fi

show_alert() {
  local title="$1"
  local msg="$2"
  ALERT_TITLE="$title" ALERT_MSG="$msg" osascript -e '
    display dialog (system attribute "ALERT_MSG") with title (system attribute "ALERT_TITLE") buttons {"OK"} default button "OK"
  ' >/dev/null
}

read_status_field() {
  local json_str="$1"
  local expr="$2"
  JSON_PAYLOAD="$json_str" "$SCRIPT_DIR/.venv/bin/python" -c "
import datetime, json, os
d = json.loads(os.environ['JSON_PAYLOAD'])
$expr
"
}

STATUS_JSON="$("$BIN" autosync status --json)"
KEYCHAIN_READY="$(read_status_field "$STATUS_JSON" "print('1' if d.get('keychain_ready') else '0')")"

# First-time GUI Keychain Setup (zero Terminal typing needed)
if [ "$KEYCHAIN_READY" != "1" ]; then
  ADMIN_USER="$(osascript -e '
    try
      set r to display dialog "One-Time Setup — Silver Jubilee Face Sync\n\nEnter your Gallery Admin username (stored securely in macOS Keychain, ₹0, zero Google Cloud):" default answer "piyush" with title "Silver Jubilee Face Sync" buttons {"Cancel", "Next"} default button "Next"
      return text returned of r
    on error
      return ""
    end try
  ' || true)"

  if [ -z "$ADMIN_USER" ]; then
    exit 0
  fi

  ADMIN_PASS="$(ADMIN_USER_VAL="$ADMIN_USER" osascript -e '
    try
      set u to system attribute "ADMIN_USER_VAL"
      set r to display dialog ("Enter Gallery Admin password for \"" & u & "\":\n\n(Saved securely in macOS Keychain — never stored in plain text.)") default answer "" with hidden answer with title "Silver Jubilee Face Sync" buttons {"Cancel", "Verify & Save"} default button "Verify & Save"
      return text returned of r
    on error
      return ""
    end try
  ' || true)"

  if [ -z "$ADMIN_PASS" ]; then
    exit 0
  fi

  SETUP_OUT="$(printf '%s\n' "$ADMIN_PASS" | "$BIN" autosync setup --admin-user "$ADMIN_USER" --interval 15 --password-stdin 2>&1)" || {
    show_alert "Setup Failed" "$SETUP_OUT"
    exit 1
  }
  unset ADMIN_PASS
  STATUS_JSON="$("$BIN" autosync status --json)"
fi

ENABLED="$(read_status_field "$STATUS_JSON" "print('1' if d.get('enabled') else '0')")"
SUMMARY_TEXT="$(read_status_field "$STATUS_JSON" "
state = f\"ON (every {d['interval_minutes']} min)\" if d.get('enabled') else 'OFF'
last_raw = d.get('last_sync_at')
if last_raw:
    try:
        dt = datetime.datetime.fromisoformat(last_raw).astimezone()
        last_str = dt.strftime('%d %b %Y, %I:%M %p')
    except Exception:
        last_str = str(last_raw)
else:
    last_str = 'Never'
print(
    f\"Auto-Sync:  {state}\\n\"
    f\"Admin:      {d.get('admin_user') or 'Not set'} (macOS Keychain)\\n\"
    f\"Indexed:    {d.get('photos', 0)} photo(s) · {d.get('faces', 0)} face(s)\\n\"
    f\"Guests:     {d.get('active_guests', 0)} active · {d.get('suggestions_pending', 0)} pending review\\n\"
    f\"Last Sync:  {last_str}\"
)
")"

if [ "$ENABLED" = "1" ]; then
  PRIMARY_BTN="Turn OFF"
else
  PRIMARY_BTN="Turn ON (15m)"
fi

CHOICE="$(DLG_MSG="$SUMMARY_TEXT" BTN_PRIMARY="$PRIMARY_BTN" osascript -e '
  set msg to system attribute "DLG_MSG"
  set pbtn to system attribute "BTN_PRIMARY"
  try
    set res to display dialog msg with title "Silver Jubilee Face Sync Control" buttons {"Close", "Sync Now", pbtn} default button pbtn cancel button "Close"
    return button returned of res
  on error
    return "Close"
  end try
' || echo "Close")"

case "$CHOICE" in
  "Turn ON (15m)")
    OUT="$("$BIN" autosync on --interval 15 2>&1)" || {
      show_alert "Could Not Enable Auto-Sync" "$OUT"
      exit 1
    }
    show_alert "Auto-Sync Enabled" "Auto-Sync is now ON (every 15 minutes while your Mac is awake).\n\nA sync has started in the background. Double-click this app anytime to turn it OFF."
    ;;
  "Turn OFF")
    "$BIN" autosync off >/dev/null 2>&1 || true
    show_alert "Auto-Sync Stopped" "Auto-Sync is now OFF.\n\nNo background sync will run (even after restarting your Mac) until you turn it back ON."
    ;;
  "Sync Now")
    echo "Running immediate sync..."
    OUT="$("$BIN" autosync run-now 2>&1)" || {
      show_alert "Sync Notice" "$OUT"
      exit 1
    }
    LAST_LINE="$(printf '%s\n' "$OUT" | tail -n 3)"
    show_alert "Sync Complete" "$LAST_LINE"
    ;;
  *)
    exit 0
    ;;
esac
