"""Atomic file writes with rolling backups."""
import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path

KEEP_BACKUPS = 30


def sha256_of(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def backup(path, backup_dir):
    path = Path(path)
    if not path.exists():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{path.stem}-{stamp}{path.suffix}"
    n = 1
    while dest.exists():
        dest = backup_dir / f"{path.stem}-{stamp}-{n}{path.suffix}"
        n += 1
    shutil.copy2(path, dest)
    old = sorted(backup_dir.glob(f"{path.stem}-*{path.suffix}"))
    for stale in old[:-KEEP_BACKUPS]:
        stale.unlink(missing_ok=True)
    return dest


def atomic_write(path, data, backup_dir=None):
    """Write bytes/str to path atomically, backing up the previous version."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if backup_dir is not None:
        backup(path, backup_dir)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copymode(path, tmp)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
