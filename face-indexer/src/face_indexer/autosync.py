"""autosync.py — User-controlled macOS LaunchAgent + Keychain auto-sync manager.

Features:
- Stores the Gallery Admin password strictly in macOS Keychain (never plain text on disk).
- Generates and loads/unloads ~/Library/LaunchAgents/com.silverjubilee.faceindexer.plist.
- Stays 100% OFF across reboots when turned OFF (removes the LaunchAgent plist cleanly).
- Enforces single-instance overlap lock (fcntl.flock) and automatic log rotation (1 MB cap).
- Zero idle memory/CPU between runs (ProcessType=Background, KeepAlive=False).
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import plistlib
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from face_indexer.indexer import CALIBRATED_COSINE_THRESHOLD
from face_indexer.sync import DEFAULT_APPS_SCRIPT_URL

LAUNCHD_LABEL = "com.silverjubilee.faceindexer"
KEYCHAIN_SERVICE = "silver-jubilee-face-indexer"
DEFAULT_INTERVAL_MINUTES = 15
MAX_LOG_BYTES = 1024 * 1024  # 1 MB automatic log rotation ceiling
KEEP_LOG_BYTES = 200 * 1024  # Keep last 200 KB on rotation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "data" / "autosync_config.json"
DEFAULT_LOCK_PATH = PROJECT_ROOT / "data" / ".sync.lock"
DEFAULT_LOG_PATH = PROJECT_ROOT / "data" / "autosync.log"
DEFAULT_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


@dataclass(frozen=True)
class AutoSyncConfig:
    """Non-secret configuration persisted locally in data/autosync_config.json."""

    admin_user: str
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    delete_refs: bool = True
    threshold: float = CALIBRATED_COSINE_THRESHOLD
    apps_script_url: str = DEFAULT_APPS_SCRIPT_URL

    def to_dict(self) -> dict[str, Any]:
        return {
            "admin_user": self.admin_user,
            "interval_minutes": self.interval_minutes,
            "delete_refs": self.delete_refs,
            "threshold": self.threshold,
            "apps_script_url": self.apps_script_url,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AutoSyncConfig:
        return cls(
            admin_user=str(raw.get("admin_user", "")).strip(),
            interval_minutes=max(1, int(raw.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))),
            delete_refs=bool(raw.get("delete_refs", True)),
            threshold=float(raw.get("threshold", CALIBRATED_COSINE_THRESHOLD)),
            apps_script_url=str(raw.get("apps_script_url", DEFAULT_APPS_SCRIPT_URL)),
        )


@dataclass(frozen=True)
class AutoSyncStatus:
    """Snapshot of the current macOS Auto-Sync state."""

    enabled: bool
    plist_exists: bool
    configured: bool
    keychain_ready: bool
    admin_user: str
    interval_minutes: int
    delete_refs: bool
    threshold: float
    plist_path: str
    log_path: str


# -----------------------------------------------------------------------------
# Config Persistence (Non-Secret)
# -----------------------------------------------------------------------------


def save_autosync_config(
    config: AutoSyncConfig,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> Path:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")
    return config_path


def load_autosync_config(config_path: Path = DEFAULT_CONFIG_PATH) -> AutoSyncConfig | None:
    if not config_path.is_file():
        return None
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        cfg = AutoSyncConfig.from_dict(raw)
        return cfg if cfg.admin_user else None
    except Exception:
        return None


# -----------------------------------------------------------------------------
# macOS Keychain Helpers (Zero Plain-Text Password Storage)
# -----------------------------------------------------------------------------


def save_keychain_password(
    admin_user: str,
    password: str,
    *,
    service: str = KEYCHAIN_SERVICE,
) -> None:
    """Store or update the Gallery Admin password securely in macOS Keychain."""
    if not admin_user or not password:
        raise ValueError("Both admin_user and password are required.")
    res = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            admin_user,
            "-s",
            service,
            "-w",
            password,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to save password in macOS Keychain: {res.stderr.strip()}")


def has_keychain_password(
    admin_user: str,
    *,
    service: str = KEYCHAIN_SERVICE,
) -> bool:
    """Check whether a Keychain entry exists without reading the secret value."""
    if not admin_user:
        return False
    res = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", admin_user, "-s", service],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0


def get_keychain_password(
    admin_user: str,
    *,
    service: str = KEYCHAIN_SERVICE,
) -> str:
    """Retrieve the Gallery Admin password from macOS Keychain into memory."""
    if not admin_user:
        raise ValueError("admin_user is required to query macOS Keychain.")
    res = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", admin_user, "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Admin password for '{admin_user}' not found in macOS Keychain. "
            "Run 'uv run face-indexer autosync setup' first."
        )
    return res.stdout.rstrip("\r\n")


def delete_keychain_password(
    admin_user: str,
    *,
    service: str = KEYCHAIN_SERVICE,
) -> bool:
    """Delete the saved password from macOS Keychain. Returns True if removed."""
    if not admin_user:
        return False
    res = subprocess.run(
        ["/usr/bin/security", "delete-generic-password", "-a", admin_user, "-s", service],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0


# -----------------------------------------------------------------------------
# Overlap Lock & Log Rotation
# -----------------------------------------------------------------------------


@contextlib.contextmanager
def sync_overlap_lock(lock_path: Path = DEFAULT_LOCK_PATH) -> Iterator[bool]:
    """Non-blocking exclusive file lock preventing overlapping scheduled sync runs.

    Yields True if the lock was acquired, or False if another sync is currently running.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def rotate_log_if_needed(
    log_path: Path = DEFAULT_LOG_PATH,
    *,
    max_bytes: int = MAX_LOG_BYTES,
    keep_bytes: int = KEEP_LOG_BYTES,
) -> bool:
    """Truncate log file to its trailing ``keep_bytes`` when it exceeds ``max_bytes``."""
    if not log_path.is_file():
        return False
    size = log_path.stat().st_size
    if size <= max_bytes:
        return False
    raw = log_path.read_bytes()
    tail = raw[-keep_bytes:]
    newline_idx = tail.find(b"\n")
    if newline_idx != -1:
        tail = tail[newline_idx + 1 :]
    banner = (
        f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        "--- Log rotated automatically ---\n"
    ).encode()
    log_path.write_bytes(banner + tail)
    return True


def read_recent_logs(log_path: Path = DEFAULT_LOG_PATH, max_lines: int = 40) -> str:
    """Return the last ``max_lines`` lines of the auto-sync log."""
    if not log_path.is_file():
        return "(No auto-sync log entries yet.)"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return "(No auto-sync log entries yet.)"
    return "\n".join(lines[-max_lines:])


# -----------------------------------------------------------------------------
# LaunchAgent (.plist) Generation & launchctl Control
# -----------------------------------------------------------------------------


def resolve_executable_path(project_root: Path = PROJECT_ROOT) -> Path:
    """Locate the installed face-indexer binary inside the project virtualenv."""
    venv_bin = project_root / ".venv" / "bin" / "face-indexer"
    if venv_bin.is_file():
        return venv_bin
    sibling = Path(sys.executable).parent / "face-indexer"
    if sibling.is_file():
        return sibling
    raise FileNotFoundError(
        f"Could not find face-indexer executable at '{venv_bin}'. Run 'uv sync' first."
    )


def build_launchd_plist(
    config: AutoSyncConfig,
    *,
    executable_path: Path | None = None,
    project_root: Path = PROJECT_ROOT,
    log_path: Path = DEFAULT_LOG_PATH,
    label: str = LAUNCHD_LABEL,
) -> dict[str, Any]:
    """Build the macOS LaunchAgent plist dictionary (zero secrets included)."""
    exe = executable_path or resolve_executable_path(project_root)
    interval_sec = max(60, int(config.interval_minutes) * 60)

    return {
        "Label": label,
        "ProgramArguments": [str(exe), "autosync", "run-now"],
        "WorkingDirectory": str(project_root),
        "StartInterval": interval_sec,
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "PYTHONUNBUFFERED": "1",
        },
    }


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def is_launchd_loaded(label: str = LAUNCHD_LABEL) -> bool:
    """Return True if the LaunchAgent is currently loaded in the user's launchd domain."""
    res = subprocess.run(
        ["/bin/launchctl", "print", f"{_launchctl_domain()}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode == 0


def enable_autosync(
    config: AutoSyncConfig,
    *,
    plist_path: Path = DEFAULT_PLIST_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    log_path: Path = DEFAULT_LOG_PATH,
    project_root: Path = PROJECT_ROOT,
    label: str = LAUNCHD_LABEL,
) -> Path:
    """Save config, write LaunchAgent plist, and load it into macOS launchd."""
    save_autosync_config(config, config_path)
    plist_data = build_launchd_plist(
        config,
        project_root=project_root,
        log_path=log_path,
        label=label,
    )
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with plist_path.open("wb") as fh:
        plistlib.dump(plist_data, fh, fmt=plistlib.FMT_XML)

    domain = _launchctl_domain()
    # Unload any previous instance quietly before bootstrapping the updated plist
    subprocess.run(
        ["/bin/launchctl", "bootout", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    boot = subprocess.run(
        ["/bin/launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if boot.returncode != 0:
        raise RuntimeError(
            f"launchctl bootstrap failed ({boot.returncode}): {boot.stderr.strip()}"
        )
    subprocess.run(
        ["/bin/launchctl", "enable", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return plist_path


def disable_autosync(
    *,
    plist_path: Path = DEFAULT_PLIST_PATH,
    label: str = LAUNCHD_LABEL,
) -> bool:
    """Unload LaunchAgent from macOS launchd and remove the plist so it stays OFF across reboots."""
    domain = _launchctl_domain()
    was_loaded = is_launchd_loaded(label)
    subprocess.run(
        ["/bin/launchctl", "bootout", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if plist_path.exists():
        plist_path.unlink()
    return was_loaded


def uninstall_autosync(
    *,
    plist_path: Path = DEFAULT_PLIST_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    label: str = LAUNCHD_LABEL,
    service: str = KEYCHAIN_SERVICE,
) -> dict[str, bool]:
    """Turn OFF auto-sync, delete plist, remove config, and purge macOS Keychain entry."""
    cfg = load_autosync_config(config_path)
    was_loaded = disable_autosync(plist_path=plist_path, label=label)
    keychain_removed = False
    if cfg and cfg.admin_user:
        keychain_removed = delete_keychain_password(cfg.admin_user, service=service)
    config_removed = False
    if config_path.exists():
        config_path.unlink()
        config_removed = True
    return {
        "was_loaded": was_loaded,
        "keychain_removed": keychain_removed,
        "config_removed": config_removed,
    }


def get_autosync_status(
    *,
    plist_path: Path = DEFAULT_PLIST_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    log_path: Path = DEFAULT_LOG_PATH,
    label: str = LAUNCHD_LABEL,
    service: str = KEYCHAIN_SERVICE,
) -> AutoSyncStatus:
    """Gather complete current status of Auto-Sync, Keychain, and LaunchAgent."""
    cfg = load_autosync_config(config_path)
    loaded = is_launchd_loaded(label)
    keychain_ok = has_keychain_password(cfg.admin_user, service=service) if cfg else False
    return AutoSyncStatus(
        enabled=loaded,
        plist_exists=plist_path.is_file(),
        configured=cfg is not None,
        keychain_ready=keychain_ok,
        admin_user=cfg.admin_user if cfg else "",
        interval_minutes=cfg.interval_minutes if cfg else DEFAULT_INTERVAL_MINUTES,
        delete_refs=cfg.delete_refs if cfg else True,
        threshold=cfg.threshold if cfg else CALIBRATED_COSINE_THRESHOLD,
        plist_path=str(plist_path),
        log_path=str(log_path),
    )
