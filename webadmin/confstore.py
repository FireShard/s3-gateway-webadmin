"""Safe, whitelisted editing of PYSerialGateway/pygw_conf.py.

Only the variables declared in FIELDS are ever rewritten, in place, so every
comment and every other setting in the file is preserved. Values are read with
ast.literal_eval (the file is never executed) and the result is re-parsed and
verified before it replaces the original.
"""
import ast
import re
from pathlib import Path

from .fileutil import atomic_write

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
GW_ID_RE = re.compile(r"^[A-Za-z0-9]{4}$")
PAN_RE = re.compile(r"^[0-9A-Fa-f]{1,4}$")


class ConfError(ValueError):
    pass


# --- per-type parse / format --------------------------------------------------
def _time(raw):
    raw = raw.strip()
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


# name -> (label, help, parser, group)
FIELDS = {
    "active_time": ("Lantern ON time", "Reference time lanterns switch on (HH:MM).", _time, "times"),
    "inactive_time": ("Lantern OFF time", "Reference time lanterns switch off (HH:MM).", _time, "times"),
    "LM_active_time": ("Manual override ON time", "Manual-override reference ON time (HH:MM).", _time, "times"),
    "node_off_time": ("Manual override OFF time", "Manual-override reference OFF time (HH:MM).", _time, "times"),
    "GPS_poll_time": ("GPS mapping start time", "When the daily GPS mapping scan starts (HH:MM).", _time, "times"),
    "pollinggap": ("Polling gap (s)", "Interval between polling one node and the next.", _int(1, 600), "polling"),
    "cycletime": ("Cycle time (s)", "Node polling cycle time. Must be at least (number of nodes x polling gap).", _int(1, 86400), "polling"),
    "aggressive_poll_duration_mins": ("Aggressive polling (min)", "How long nodes are polled aggressively after the ON time.", _int(0, 1440), "polling"),
    "minimum_power": ("Minimum power (W)", "Expected minimum draw: a lantern below this during active hours is treated as OFF.", _float(0.0, 100000.0), "polling"),
    "first_GW_data": ("First gateway", "Gateway node ID, PAN ID and Zigbee channel.", _gw, "gateway"),
    "second_GW_data": ("Second gateway", "Second gateway (only used for dual-polling).", _gw, "gateway"),
}

GROUPS = [
    ("times", "Schedule times"),
    ("polling", "Polling"),
    ("gateway", "Static gateway specs"),
]

# Shown read-only for context; never written by this app.
READONLY = ["localDBpath", "client_ID", "client_ID_2", "topic_header"]


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
    source = Path(path).read_text(encoding="utf-8")
    vals = _assignment_values(source)
    return {
        "values": {k: vals.get(k) for k in FIELDS},
        "readonly": {k: vals.get(k) for k in READONLY if k in vals},
    }


def validate(form):
    """form: {name: raw value (or 3-list for gateways)}. Returns (clean, errors)."""
    clean, errors = {}, {}
    for name, (_label, _help, parser, _group) in FIELDS.items():
        if name not in form:
            continue
        try:
            clean[name] = parser(form[name])
        except ConfError as exc:
            errors[name] = str(exc)
    return clean, errors


def save(path, clean, backup_dir):
    path = Path(path)
    source = path.read_bytes().decode("utf-8")
    new = source
    for name, value in clean.items():
        pattern = re.compile(
            r"^(?P<pre>" + re.escape(name) + r"[ \t]*=[ \t]*)(?P<val>[^\r\n]*?)(?P<post>[ \t]+#[^\r\n]*)?(?=\r?$)",
            re.M,
        )
        matches = list(pattern.finditer(new))
        if len(matches) != 1:
            raise ConfError(f"could not find a single '{name} = ...' line in {path.name}")
        m = matches[0]
        new = new[: m.start("val")] + _fmt(value) + new[m.end("val"):]
    try:
        check = _assignment_values(new)
    except SyntaxError as exc:
        raise ConfError(f"refusing to write: result is not valid Python ({exc})")
    for name, value in clean.items():
        if check.get(name) != value:
            raise ConfError(f"refusing to write: '{name}' did not round-trip")
    if new != source:
        atomic_write(path, new, backup_dir)
    return new != source


def cycle_warning(values, node_count):
    """Advisory check from the comment in pygw_conf.py."""
    try:
        need = node_count * int(values["pollinggap"])
        if int(values["cycletime"]) < need:
            return (f"Cycle time {values['cycletime']}s is shorter than nodes x polling gap "
                    f"({node_count} x {values['pollinggap']} = {need}s); polling won't finish a full cycle.")
    except (KeyError, TypeError, ValueError):
        pass
    return None
