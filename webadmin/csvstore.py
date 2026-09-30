"""samplelist.csv reader/writer. Validation mirrors scripts/s3-gateway-dbup."""
import csv
import io
import re
from pathlib import Path

from .fileutil import atomic_write

FIELDS = ["pole_node", "node", "pan_id", "channel"]
_POLE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
_NODE_RE = re.compile(r"^[0-9A-Fa-f]{4}$")
_PAN_RE = re.compile(r"^[0-9A-Fa-f]{1,4}$")


class CsvError(ValueError):
    pass


def clean_row(raw):
    """Return a normalised row dict or raise CsvError."""
    row = {k: str(raw.get(k, "") or "").strip() for k in FIELDS}
    if not all(row.values()):
        missing = [k for k in FIELDS if not row[k]]
        raise CsvError("missing value for: " + ", ".join(missing))
    if not _POLE_RE.match(row["pole_node"]):
        raise CsvError(f"pole_node '{row['pole_node']}' may only contain letters, digits, '.', '_' and '-'")
    if not _NODE_RE.match(row["node"]):
        raise CsvError(f"node ID must be 4 hex characters, got '{row['node']}'")
    if not _PAN_RE.match(row["pan_id"]):
        raise CsvError(f"PAN ID must be hex (0000-FFFF), got '{row['pan_id']}'")
    try:
        channel = int(row["channel"])
    except ValueError:
        raise CsvError(f"channel must be a number, got '{row['channel']}'")
    if not 11 <= channel <= 26:
        raise CsvError(f"Zigbee channel must be 11-26, got {channel}")
    row["node"] = row["node"].upper()
    row["pan_id"] = row["pan_id"].upper()
    row["channel"] = str(channel)
    return row


def check_unique(rows):
    seen_nodes, seen_poles = {}, {}
    for i, row in enumerate(rows, start=1):
        if row["node"] in seen_nodes:
            raise CsvError(f"duplicate node ID {row['node']} (rows {seen_nodes[row['node']]} and {i})")
        if row["pole_node"] in seen_poles:
            raise CsvError(f"duplicate pole ID {row['pole_node']} (rows {seen_poles[row['pole_node']]} and {i})")
        seen_nodes[row["node"]] = i
        seen_poles[row["pole_node"]] = i


def parse_csv_text(text):
    """Parse full CSV text (with header) into validated rows."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames != FIELDS:
        raise CsvError("CSV header must be exactly: " + ",".join(FIELDS))
    rows = []
    for line_no, raw in enumerate(reader, start=2):
        if not any((v or "").strip() for v in raw.values() if isinstance(v, str)):
            continue
        try:
            rows.append(clean_row(raw))
        except CsvError as exc:
            raise CsvError(f"line {line_no}: {exc}")
    check_unique(rows)
    return rows


def parse_bulk(text, default_pan, default_channel):
    """Parse pasted lines: pole,node[,pan,channel] separated by comma/tab/space."""
    rows, errors = [], []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in re.split(r"[,\t; ]+", line) if p]
        if parts and parts[0].lower() == "pole_node":
            continue  # pasted header
        if len(parts) not in (2, 4):
            errors.append(f"line {line_no}: expected 'pole,node' or 'pole,node,pan,channel'")
            continue
        if len(parts) == 2:
            parts += [default_pan, default_channel]
        try:
            rows.append(clean_row(dict(zip(FIELDS, parts))))
        except CsvError as exc:
            errors.append(f"line {line_no}: {exc}")
    return rows, errors


def load(path):
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        if not any((v or "").strip() for v in raw.values() if isinstance(v, str)):
            continue
        rows.append({k: str(raw.get(k) or "").strip() for k in FIELDS})
    return rows


def dumps(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row[k] for k in FIELDS})
    return buf.getvalue()


def save(path, rows, backup_dir):
    if not rows:
        raise CsvError("the node list cannot be empty (DBUP requires at least one node)")
    rows = [clean_row(r) for r in rows]
    check_unique(rows)
    atomic_write(path, dumps(rows), backup_dir)
    return rows
