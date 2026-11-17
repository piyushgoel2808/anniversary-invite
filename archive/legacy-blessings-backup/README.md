# Legacy Blessings Backup (Phase 0 Snapshot)

This directory is a verbatim archival snapshot of the invitation and legacy blessings server prior to replacing Page 5 with the Private Anniversary Memories Gallery card.

## Restoration & Security Notes

- `server.py` expects `index.html` and `blessings.html` inside a `static/` subdirectory relative to `server.py` (matching the original repository root layout).
- **Security Warning:** This archived `server.py` is preserved verbatim for offline archival reference only. Because the archived server binds to `0.0.0.0` and has a legacy fallback password, **never run it on a shared network without first changing the bind address to `127.0.0.1` and setting a strong unique `ADMIN_PASSWORD` environment variable**:

```bash
cp archive/legacy-blessings-backup/server.py ./server.py
cp archive/legacy-blessings-backup/index.html ./index.html
cp archive/legacy-blessings-backup/index.html ./static/index.html
cp archive/legacy-blessings-backup/blessings.html ./blessings.html
cp archive/legacy-blessings-backup/blessings.html ./static/blessings.html
# Before starting, edit server.py to bind HTTPServer(("127.0.0.1", port), ...) and set a strong unique password:
ADMIN_PASSWORD="ReplaceWithUniqueLocalPassword" python3 server.py
```
