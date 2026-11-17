"""test_autosync.py — Unit tests for macOS LaunchAgent, Keychain wrapper, lock & log rotation."""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Any

import pytest

from face_indexer.autosync import (
    DEFAULT_INTERVAL_MINUTES,
    LAUNCHD_LABEL,
    AutoSyncConfig,
    build_launchd_plist,
    disable_autosync,
    enable_autosync,
    load_autosync_config,
    read_recent_logs,
    rotate_log_if_needed,
    save_autosync_config,
    sync_overlap_lock,
    uninstall_autosync,
)


def test_config_roundtrip_contains_no_secrets(tmp_path: Path) -> None:
    cfg_path = tmp_path / "autosync_config.json"
    assert load_autosync_config(cfg_path) is None

    cfg = AutoSyncConfig(admin_user="piyush", interval_minutes=15, delete_refs=True)
    save_autosync_config(cfg, cfg_path)

    loaded = load_autosync_config(cfg_path)
    assert loaded is not None
    assert loaded.admin_user == "piyush"
    assert loaded.interval_minutes == DEFAULT_INTERVAL_MINUTES
    assert loaded.delete_refs is True
    assert "password" not in cfg_path.read_text(encoding="utf-8").lower()


def test_build_launchd_plist_structure_and_interval(tmp_path: Path) -> None:
    fake_exe = tmp_path / ".venv" / "bin" / "face-indexer"
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_text("#!/bin/sh\n", encoding="utf-8")

    cfg = AutoSyncConfig(admin_user="piyush", interval_minutes=15)
    plist_dict = build_launchd_plist(
        cfg,
        executable_path=fake_exe,
        project_root=tmp_path,
        log_path=tmp_path / "autosync.log",
    )

    assert plist_dict["Label"] == LAUNCHD_LABEL
    assert plist_dict["StartInterval"] == 900
    assert plist_dict["RunAtLoad"] is True
    assert plist_dict["KeepAlive"] is False
    assert plist_dict["ProcessType"] == "Background"
    assert plist_dict["ProgramArguments"] == [str(fake_exe), "autosync", "run-now"]

    # Must serialize cleanly as valid Apple XML plist
    xml_bytes = plistlib.dumps(plist_dict, fmt=plistlib.FMT_XML)
    reparsed = plistlib.loads(xml_bytes)
    assert reparsed["StartInterval"] == 900


def test_sync_overlap_lock_prevents_concurrent_runs(tmp_path: Path) -> None:
    lock_file = tmp_path / ".sync.lock"
    with sync_overlap_lock(lock_file) as first_acquired:
        assert first_acquired is True
        # Second concurrent attempt must fail immediately without blocking
        with sync_overlap_lock(lock_file) as second_acquired:
            assert second_acquired is False

    # After exiting outer context, lock must be acquirable again
    with sync_overlap_lock(lock_file) as third_acquired:
        assert third_acquired is True


def test_rotate_log_if_needed_and_read_recent(tmp_path: Path) -> None:
    log_path = tmp_path / "autosync.log"
    assert "No auto-sync log entries" in read_recent_logs(log_path)

    # Small log is not rotated
    log_path.write_text("line1\nline2\n", encoding="utf-8")
    assert rotate_log_if_needed(log_path, max_bytes=1024, keep_bytes=256) is False
    assert read_recent_logs(log_path, max_lines=5) == "line1\nline2"

    # Oversized log rotates cleanly
    big = "\n".join(f"log-entry-{i:04d}" for i in range(500)) + "\n"
    log_path.write_text(big, encoding="utf-8")
    assert rotate_log_if_needed(log_path, max_bytes=500, keep_bytes=200) is True
    rotated = log_path.read_text(encoding="utf-8")
    assert "Log rotated automatically" in rotated
    assert "log-entry-0499" in rotated
    assert len(rotated) < 400


def test_enable_disable_uninstall_lifecycle_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    class _FakeCompleted:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        commands.append(cmd)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr("face_indexer.autosync.subprocess.run", _fake_run)

    fake_exe = tmp_path / ".venv" / "bin" / "face-indexer"
    fake_exe.parent.mkdir(parents=True, exist_ok=True)
    fake_exe.write_text("#!/bin/sh\n", encoding="utf-8")

    plist_path = tmp_path / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    cfg_path = tmp_path / "data" / "autosync_config.json"
    log_path = tmp_path / "data" / "autosync.log"

    cfg = AutoSyncConfig(admin_user="piyush", interval_minutes=15)
    created = enable_autosync(
        cfg,
        plist_path=plist_path,
        config_path=cfg_path,
        log_path=log_path,
        project_root=tmp_path,
    )
    assert created == plist_path
    assert plist_path.is_file()
    assert cfg_path.is_file()
    assert any("bootstrap" in c for c in commands)

    # Disabling removes the plist so it stays OFF across reboots
    was_loaded = disable_autosync(plist_path=plist_path)
    assert was_loaded is True
    assert not plist_path.exists()
    assert any("bootout" in c for c in commands)

    # Uninstall cleans config and calls security delete-generic-password
    res = uninstall_autosync(plist_path=plist_path, config_path=cfg_path)
    assert res["config_removed"] is True
    assert res["keychain_removed"] is True
    assert not cfg_path.exists()
    assert any("delete-generic-password" in c for c in commands)
