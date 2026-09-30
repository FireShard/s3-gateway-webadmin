"""Safe, whitelisted editing of PYSerialGateway/pygw_conf.py.

Only the variables declared in FIELDS are ever rewritten. Each edit replaces
just the value expression of one top-level assignment (located with the ast
module), so comments, spacing and every other setting are preserved byte for
byte. Values are read with ast.literal_eval (the file is never executed) and
the result is re-parsed and verified before it replaces the original.
"""
import ast
import re
from collections import namedtuple
from pathlib import Path

from .fileutil import atomic_write

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
GW_ID_RE = re.compile(r"^[A-Za-z0-9]{4}$")
PAN_RE = re.compile(r"^[0-9A-Fa-f]{1,4}$")


class ConfError(ValueError):
    pass


# --- parsers ------------------------------------------------------------------
def _time(raw):
    raw = str(raw).strip()
    if not TIME_RE.match(raw):
        raise ConfError("use 24-hour HH:MM, e.g. 19:30")
    return raw


def _int(lo, hi):
    def parse(raw):
        try:
            v = int(str(raw).strip())
        except ValueError:
            raise ConfError("must be a whole number")
        if not lo <= v <= hi:
            raise ConfError(f"must be between {lo} and {hi}")
        return v
    return parse


def _float(lo, hi):
    def parse(raw):
        try:
            v = float(str(raw).strip())
        except ValueError:
            raise ConfError("must be a number")
        if not lo <= v <= hi:
            raise ConfError(f"must be between {lo} and {hi}")
        return v
    return parse


def _bool(raw):
    if isinstance(raw, bool):
        return raw
    v = str(raw).strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    raise ConfError("must be True or False")


def _text(pattern, hint, maxlen=64, nullable=False):
    rx = re.compile(pattern)

    def parse(raw):
        v = str(raw).strip()
        if not v:
            if nullable:
                return None
            raise ConfError("cannot be empty")
        if len(v) > maxlen or not rx.fullmatch(v):
            raise ConfError(hint)
        return v
    return parse


def _port(raw):
    try:
        v = int(str(raw).strip())
    except ValueError:
        raise ConfError("must be a port number")
    if not 1 <= v <= 65535:
        raise ConfError("must be between 1 and 65535")
    return str(v)  # pygw_conf.py stores the port as a string


def _password(raw):
    v = str(raw)
    if v == "":
        return None
    if len(v) > 128 or not all(c.isprintable() for c in v):
        raise ConfError("use up to 128 printable characters")
    return v


def _gw(raw):
    """Gateway spec arrives as three parts: node id, PAN id, channel."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ConfError("needs node ID, PAN ID and channel")
    node, pan, ch = [str(x).strip() for x in raw]
    if not GW_ID_RE.match(node):
        raise ConfError("gateway node ID must be 4 letters/digits")
    if not PAN_RE.match(pan):
        raise ConfError("PAN ID must be hex (0000-FFFF)")
    try:
        chn = int(ch)
    except ValueError:
        raise ConfError("channel must be a number")
    if not 11 <= chn <= 26:
        raise ConfError("channel must be 11-26")
    return (node.upper(), pan.upper(), str(chn))


def _fmt(value):
    if isinstance(value, tuple):
        return "(" + ", ".join(repr(v) for v in value) + ")"
    return repr(value)


# --- the whitelist, in the same order as pygw_conf.py ---------------------------
# kind picks the form widget: int, float, time, text, bool, gw, secret
Field = namedtuple("Field", "help parser group kind")

_NAME = r"[A-Za-z0-9_.\-]+"

FIELDS = {
    "cycletime": Field("Node polling cycle time (seconds).", _int(1, 86400), "polling", "int"),
    "pollinggap": Field("Polling interval between nodes (seconds).", _int(1, 600), "polling", "int"),
    "active_time": Field("Lantern ON reference time.", _time, "times", "time"),
    "GPS_poll_time": Field("GPS Mapping start reference time.", _time, "times", "time"),
    "inactive_time": Field("Lantern OFF reference time.", _time, "times", "time"),
    "LM_active_time": Field("Manual overriding reference ON time.", _time, "times", "time"),
    "node_off_time": Field("Manual overriding reference OFF time.", _time, "times", "time"),
    "aggressive_poll_duration_mins": Field(
        "Aggressive polling duration for nodes starting from active_time (minutes).", _int(0, 1440), "polling", "int"),
    "minimum_power": Field(
        "Power check for lantern if the lantern is OFF during active hours (watts).", _float(0.0, 100000.0), "polling", "float"),
    "max_msgID_count": Field("Max MsgID implemented.", _int(1, 99999), "polling", "int"),
    "test_align_flag": Field("Test alignment flag used by the HTTP thread.", _bool, "mqtt", "bool"),
    "cert_codename": Field(
        "Selects the required-<name>gw.zip certificate bundle the gateway loads at start-up. "
        "It must match a bundle that exists on the gateway.",
        _text(r"[A-Za-z0-9_\-]+", "letters, digits, - and _ only", 32), "mqtt", "text"),
    "topic_header": Field(
        "MQTT topic header. Must end with /, with no spaces, + or #.",
        _text(r"[A-Za-z0-9_.\-/]*/", "must end with / and use only letters, digits, . _ - and /", 100), "mqtt", "text"),
    "client_ID": Field("Client ID for first instance.", _text(_NAME, "letters, digits, . _ - only"), "mqtt", "text"),
    "client_ID_2": Field(
        "Client ID for second instance (only used for dual-polling).", _text(_NAME, "letters, digits, . _ - only"), "mqtt", "text"),
    "localDBpath": Field(
        "Local deploy node list file. Must stay samplelist.csv unless you also rename the file DBUP installs.",
        _text(r"[A-Za-z0-9_.\-]+\.csv", "a plain file name ending in .csv", 64), "gateway", "text"),
    "first_GW_data": Field("First gateway node specifications: node ID, PAN ID, channel.", _gw, "gateway", "gw"),
    "second_GW_data": Field("Second gateway node specifications: node ID, PAN ID, channel.", _gw, "gateway", "gw"),
    "db_host": Field(
        "Database host. Leave empty (None) to use the local Unix socket.",
        _text(r"[A-Za-z0-9_.:/\-]+", "host name, IP address or socket directory", 253, nullable=True), "database", "text"),
    "db_port": Field("Database port.", _port, "database", "text"),
    "db_user": Field("Database user.", _text(_NAME, "letters, digits, . _ - only", 63), "database", "text"),
    "db_password": Field(
        "Leave empty (None) for peer authentication. Sent over plain HTTP, so only use this on a trusted network.",
        _password, "database", "secret"),
    "db_name": Field("Database name.", _text(_NAME, "letters, digits, . _ - only", 63), "database", "text"),
}

SECRETS = {"db_password"}
# Changing these can stop the gateway from starting or reaching its database.
RISKY = ["db_host", "db_port", "db_user", "db_password", "db_name", "cert_codename", "localDBpath"]

GROUPS = [
    ("polling", "Polling", "Timing and limits for polling the nodes."),
    ("times", "Daily schedule", "Reference times on the gateway's own clock."),
    ("mqtt", "HTTP and MQTT", "Used by the HTTP and MQTT threads."),
    ("gateway", "Static gateway and database specs", ""),
    ("database", "PostgreSQL connection",
     "A wrong value here stops the gateway from reaching its database. Leave db_host and db_password empty to use the local socket with peer authentication."),
]

# Shown read-only for context; never written by this app.
READONLY = ["problemlogpath", "logfilepath", "maplogpath", "msgID", "msgID_vers", "nodeID"]


def _assignment_values(source):
    tree = ast.parse(source)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                pass
    return out


def load(path):
    """Return {'values': {...editable}, 'readonly': {...}}."""
    source = Path(path).read_bytes().decode("utf-8")
    vals = _assignment_values(source)
    return {
        "values": {k: vals.get(k) for k in FIELDS},
        "readonly": {k: vals.get(k) for k in READONLY if k in vals},
    }


def validate(form):
    """form: {name: raw value (or 3-list for gateways)}. Returns (clean, errors)."""
    clean, errors = {}, {}
    for name, field in FIELDS.items():
        if name not in form:
            continue
        try:
            clean[name] = field.parser(form[name])
        except ConfError as exc:
            errors[name] = str(exc)
    return clean, errors


def _line_starts(data):
    starts, pos = [0], 0
    while True:
        pos = data.find(b"\n", pos)
        if pos < 0:
            return starts
        pos += 1
        starts.append(pos)


def save(path, clean, backup_dir):
    """Rewrite the value of each name in `clean`. Returns True if the file changed."""
    path = Path(path)
    original = path.read_bytes()
    try:
        tree = ast.parse(original)
    except SyntaxError as exc:
        raise ConfError(f"{path.name} is not valid Python ({exc})")
    hits = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in clean:
            hits.setdefault(node.targets[0].id, []).append(node.value)
    starts = _line_starts(original)
    edits = []
    for name in clean:
        found = hits.get(name, [])
        if len(found) != 1:
            raise ConfError(f"could not find a single '{name} = ...' line in {path.name}")
        v = found[0]
        begin = starts[v.lineno - 1] + v.col_offset
        end = starts[v.end_lineno - 1] + v.end_col_offset
        edits.append((begin, end, _fmt(clean[name]).encode("utf-8")))
    new = original
    for begin, end, text in sorted(edits, reverse=True):
        new = new[:begin] + text + new[end:]

    try:
        before = _assignment_values(original.decode("utf-8"))
        after = _assignment_values(new.decode("utf-8"))
    except SyntaxError as exc:
        raise ConfError(f"refusing to write: result is not valid Python ({exc})")
    for name, value in clean.items():
        if after.get(name) != value:
            raise ConfError(f"refusing to write: '{name}' did not round-trip")
    for name, value in before.items():
        if name not in clean and after.get(name) != value:
            raise ConfError(f"refusing to write: '{name}' changed unexpectedly")
    if new != original:
        atomic_write(path, new, backup_dir)
    return new != original


def cycle_warning(values, node_count):
    """Advisory check from the comment in pygw_conf.py."""
    try:
        need = node_count * int(values["pollinggap"])
        if int(values["cycletime"]) < need:
            return (f"cycletime {values['cycletime']}s is shorter than nodes x pollinggap "
                    f"({node_count} x {values['pollinggap']} = {need}s); polling won't finish a full cycle.")
    except (KeyError, TypeError, ValueError):
        pass
    return None


def local_db_warning(values):
    name = values.get("localDBpath")
    if name and name != "samplelist.csv":
        return (f"localDBpath is '{name}', but the Devices page and DBUP work on samplelist.csv. "
                "The gateway will look for a file that DBUP never installs.")
    return None
