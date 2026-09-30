"""Read-only access to the gateway log files (tail, filter, list rotations)."""
import re
from pathlib import Path

KINDS = ("gateway", "mqtt", "error")
_NAME_RE = re.compile(r"^(gateway|mqtt|error)\.log(\.\d{4}-\d{2}-\d{2})?$")
MAX_READ_BYTES = 4 * 1024 * 1024
MAX_LINES = 2000
LEVELS = ("ERROR", "WARNING", "INFO", "DEBUG")


def list_files(log_dir):
    """{'gateway': ['gateway.log', 'gateway.log.2026-09-28', ...newest first], ...}"""
    out = {k: [] for k in KINDS}
    log_dir = Path(log_dir)
    if log_dir.is_dir():
        for p in log_dir.iterdir():
            m = _NAME_RE.match(p.name)
            if m and p.is_file():
                out[m.group(1)].append(p.name)
    for kind in out:
        # current file first, then rotations newest-first (dateext sorts lexically)
        current = [n for n in out[kind] if n.endswith(".log")]
        rotated = sorted((n for n in out[kind] if not n.endswith(".log")), reverse=True)
        out[kind] = current + rotated
    return out


def resolve(log_dir, name):
    """Map a user-supplied file name to a safe path, or None."""
    if not name or not _NAME_RE.match(name):
        return None
    p = Path(log_dir) / name
    return p if p.is_file() else None


def _tail_bytes(path, max_bytes):
    size = path.stat().st_size
    with open(path, "rb") as fh:
        truncated = size > max_bytes
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    if truncated:
        data = data.split(b"\n", 1)[-1]  # drop the partial first line
    return data.decode("utf-8", errors="replace"), size


def read(path, lines=200, query="", level=""):
    lines = max(1, min(int(lines), MAX_LINES))
    text, size = _tail_bytes(path, MAX_READ_BYTES)
    rows = text.splitlines()
    if level and level.upper() in LEVELS:
        want = level.upper()
        rows = [r for r in rows if want in r]
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r.lower()]
    return {"lines": rows[-lines:], "size": size, "matched": len(rows)}
