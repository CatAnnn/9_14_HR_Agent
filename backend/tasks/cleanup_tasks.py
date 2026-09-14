from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path


def cleanup_old_files(base_dir: Path, older_than_days: int = 7) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    removed = 0
    for path in base_dir.rglob("*"):
        if path.is_file():
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                path.unlink()
                removed += 1
    return removed
