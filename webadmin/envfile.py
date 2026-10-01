"""Read-only lookup of single values from the gateway's .env file."""
from pathlib import Path


def read_value(path, key, max_len=64):
    """Return the value of ``key`` in a KEY=VALUE env file, or "" if it cannot be read.

    Never raises: the file may be missing, or unreadable by the web admin's
    account, and the UI simply omits the value in that case. The last
    assignment wins (as when the file is sourced). Only ``key`` is returned;
    nothing else in the file (MQTT credentials, etc.) is exposed.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    value = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, raw = line.partition("=")
        if not sep or name.strip() != key:
            continue
        raw = raw.strip()
        if raw[:1] in "\"'" and raw.endswith(raw[:1]) and len(raw) >= 2:
            raw = raw[1:-1]
        else:
            raw = raw.split(" #", 1)[0].strip()
        value = raw
    return "".join(ch for ch in value if ch.isprintable())[:max_len]
