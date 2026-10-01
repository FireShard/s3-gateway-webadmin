"""Serial controls for the web admin.

Two independent ways to talk to the lamp nodes:

* GatewayClient - asks the *running* gateway to send a command, through the gateway's own
  REST server (port 9090). The gateway keeps the USB port, so nothing is interrupted.
* SerialConsole - a minicom-style raw console. The gateway holds an exclusive lock on the USB
  port while it runs, so the console has to pause the gateway service first and restarts it
  when the session ends (or goes idle).
"""
import collections
import fcntl
import json
import os
import re
import subprocess
import threading
import time

import requests
import serial
import serial.tools.list_ports

NODE_RE = re.compile(r"^[0-9A-Fa-f]{4}$")
LEVELS = "0123456789"
MAX_NODES = 500
BUNDLE_SIZE = 20          # node IDs per REST request (they travel in the URL, joined with '-')
BAUD = 115200
MAX_COMMAND_LEN = 96

# key -> (REST endpoint, label, serial command the gateway sends)
ACTIONS = {
    "poll":         ("poll-node",               "Poll",                "+PM"),
    "blink":        ("find-me",                 "Blink",               "+TFM"),
    "lamp-on":      ("on-node",                 "Lamp on",             "+LCB"),
    "lamp-off":     ("off-node",                "Lamp off",            "+LCC"),
    "dim":          ("dim-node",                "Dim",                 "+LCD"),
    "override-on":  ("enable-manual-override",  "Manual override on",  "+LM1"),
    "override-off": ("disable-manual-override", "Manual override off", "+LM0"),
}


class SerialError(Exception):
    def __init__(self, message, status=400, code="error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def clean_nodes(values):
    """Validate node IDs (4 hex characters), upper-case, drop repeats, keep order."""
    if not isinstance(values, (list, tuple)) or not values:
        raise SerialError("Choose at least one device.")
    out, seen = [], set()
    for v in values:
        v = str(v).strip().upper()
        if not NODE_RE.match(v):
            raise SerialError(f"'{v[:12]}' is not a node ID (4 hex characters, like 2001).")
        if v not in seen:
            seen.add(v)
            out.append(v)
    if len(out) > MAX_NODES:
        raise SerialError(f"Too many devices in one request (limit {MAX_NODES}).")
    return out


def clean_command(text):
    """A raw line typed in the console. Printable ASCII only; the line ending is added on send."""
    text = (text or "").strip()
    if not text:
        raise SerialError("Type a command first.")
    if len(text) > MAX_COMMAND_LEN or any(not 0x20 <= ord(c) <= 0x7E for c in text):
        raise SerialError(f"Commands are printable ASCII, up to {MAX_COMMAND_LEN} characters.")
    return text


# ------------------------------------------------------------------------------------------
# Node commands through the running gateway
# ------------------------------------------------------------------------------------------
class GatewayClient:
    def __init__(self, base_url, timeout=5):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def reachable(self):
        try:
            requests.get(self.base + "/", timeout=1)   # any HTTP answer (even 404) means it is up
            return True
        except requests.RequestException:
            return False

    def send(self, action, nodes, level=None):
        if action not in ACTIONS and action != "dim-level":
            raise SerialError("Unknown action.")
        nodes = clean_nodes(nodes)
        if action == "dim-level":
            if level is None or str(level) not in tuple(LEVELS):
                raise SerialError("Choose a dim level from 0 to 9.")
            prefix = f"/gateway-serial-listener/dim-level-node/{level}/"
        else:
            prefix = f"/gateway-serial-listener/{ACTIONS[action][0]}/"
        sent, failed = 0, []
        for i in range(0, len(nodes), BUNDLE_SIZE):
            chunk = nodes[i:i + BUNDLE_SIZE]
            try:
                r = requests.get(self.base + prefix + "-".join(chunk), timeout=self.timeout)
            except requests.ConnectionError:
                raise SerialError(
                    "The gateway is not answering on its control port. Is the service running "
                    "(or paused by the direct console)?", 503, "gateway-down")
            except requests.RequestException as exc:
                raise SerialError(f"Could not reach the gateway: {exc.__class__.__name__}.", 502)
            if r.status_code == 200:
                sent += len(chunk)
            else:
                failed.extend(chunk)
        return {"sent": sent, "failed": failed, "nodes": nodes}


# ------------------------------------------------------------------------------------------
# Direct console
# ------------------------------------------------------------------------------------------
def usb_ports():
    """USB serial ports, ordered like the gateway does (numerically by the number after USB)."""
    found = []
    for p in serial.tools.list_ports.comports():
        m = re.search(r"USB(\d+)$", p.device)
        if m:
            found.append((int(m.group(1)), p.device, p.description or ""))
    return [{"device": d, "description": desc} for _, d, desc in sorted(found)]


class SerialConsole:
    def __init__(self, settings, service_state=None):
        self.s = settings
        self.service_state = service_state or (lambda: None)
        self._lock = threading.RLock()
        self._ser = None
        self._port = None
        self._paused = False            # we stopped the gateway service and must start it again
        self._seq = 0
        self._lines = collections.deque(maxlen=3000)
        self._last_activity = 0.0
        self._notice = ""
        self._marker = settings.state_dir / "serial-paused"

    # ---- ports -------------------------------------------------------------------------
    def allowed_ports(self):
        if self.s.serial_port != "auto":
            return [{"device": self.s.serial_port, "description": "configured (S3_SERIAL_PORT)"}]
        return usb_ports()

    def _check_port(self, port):
        allowed = [p["device"] for p in self.allowed_ports()]
        if not allowed:
            raise SerialError("No USB serial port found. Is the gateway node plugged in?", 404, "no-port")
        if not port:
            port = allowed[0]
        if port not in allowed:
            raise SerialError("That is not one of the detected serial ports.")
        return port

    # ---- low-level hooks (patched in tests) ----------------------------------------------
    def _port_busy(self, port):
        """True if another program (normally the gateway) holds the port's exclusive lock.
        Probed without touching the port settings, so a running gateway is not disturbed."""
        if not port.startswith("/"):
            return False
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError:
            return False        # let the real open report permission / missing device
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def _open_serial(self, port):
        try:
            ser = serial.serial_for_url(port, baudrate=BAUD, bytesize=serial.EIGHTBITS,
                                        parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                        timeout=0.2)
        except (serial.SerialException, OSError) as exc:
            msg = str(exc)
            if "ermission" in msg:
                raise SerialError("Permission denied on the serial port. The account running the web "
                                  "admin must be in the 'dialout' group.", 403, "permission")
            raise SerialError(f"Could not open {port}: {msg}", 500)
        try:
            fcntl.flock(ser.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            ser.close()
            raise SerialError("The serial port is in use by another program.", 409, "busy")
        except (OSError, AttributeError, ValueError, serial.SerialException):
            pass                # no lock support (for example loop:// in tests)
        return ser

    def _run(self, command):
        try:
            r = subprocess.run(command, capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
        return r.returncode, (r.stderr or r.stdout or "").strip()

    # ---- gateway service pause / resume ---------------------------------------------------
    def _pause_gateway(self, port):
        if self.service_state() != "active":
            raise SerialError("The serial port is held by another program, not the gateway service. "
                              "Close it (is minicom open?) and try again.", 409, "busy-other")
        rc, msg = self._run(self.s.serial_pause_command)
        if rc != 0:
            hint = " The sudo rule for stopping the gateway is probably not installed." if "password" in msg else ""
            raise SerialError(f"Could not pause the gateway: {msg or 'exit code ' + str(rc)}.{hint}", 500)
        self._paused = True
        self._write_marker()
        for _ in range(20):
            if not self._port_busy(port):
                return
            time.sleep(0.5)
        self._resume_gateway()
        raise SerialError("The gateway was stopped but the port is still locked. It has been restarted.", 500)

    def _resume_gateway(self):
        """Start the service again. Returns an error message, or '' on success."""
        if not self._paused:
            return ""
        rc, msg = self._run(self.s.serial_resume_command)
        if rc != 0:
            return f"Could not restart the gateway: {msg or 'exit code ' + str(rc)}. Start it from Overview or over SSH."
        self._paused = False
        try:
            self._marker.unlink()
        except OSError:
            pass
        return ""

    def _write_marker(self):
        try:
            self.s.state_dir.mkdir(parents=True, exist_ok=True)
            self._marker.write_text(json.dumps({"since": time.strftime("%Y-%m-%d %H:%M:%S")}))
        except OSError:
            pass

    def recover(self):
        """After a web admin crash or restart: if we had stopped the gateway, start it again."""
        if self._marker.exists() and self._ser is None:
            self._paused = True
            err = self._resume_gateway()
            self._notice = err

    # ---- session ------------------------------------------------------------------------
    def open(self, port=None, pause=False):
        with self._lock:
            if self._ser is not None:
                return
            port = self._check_port(port)
            if self._port_busy(port):
                if not pause:
                    raise SerialError("The gateway is using the serial port.", 409, "busy")
                self._pause_gateway(port)
            try:
                self._ser = self._open_serial(port)
            except SerialError:
                self._resume_gateway()
                raise
            self._port = port
            self._notice = ""
            self._touch()
            self._add("sys", f"Opened {port} at {BAUD} 8N1")
            threading.Thread(target=self._reader, args=(self._ser,), daemon=True).start()
            threading.Thread(target=self._watchdog, args=(self._ser,), daemon=True).start()

    def close(self, reason=None):
        """Close the port and, if we paused the gateway, start it again. Returns a message or ''."""
        with self._lock:
            ser, self._ser = self._ser, None
            if ser is not None:
                try:
                    try:
                        fcntl.flock(ser.fileno(), fcntl.LOCK_UN)
                    except (OSError, AttributeError, ValueError, serial.SerialException):
                        pass
                    ser.close()
                except (OSError, serial.SerialException):
                    pass
                self._add("sys", reason or "Closed the port")
            was_paused = self._paused
            err = self._resume_gateway()
            if ser is not None and was_paused and not err:
                self._add("sys", "Gateway service restarted")
            self._notice = err
            return err

    def _touch(self):
        self._last_activity = time.monotonic()

    def _add(self, kind, text):
        with self._lock:
            self._seq += 1
            self._lines.append({"seq": self._seq, "t": time.strftime("%H:%M:%S"), "dir": kind, "text": text})

    def _reader(self, ser):
        buf, last = bytearray(), time.monotonic()
        while True:
            with self._lock:
                if self._ser is not ser:
                    return
            try:
                data = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError, TypeError):
                with self._lock:
                    if self._ser is ser:
                        self.close("Lost the serial port (unplugged?)")
                return
            if data:
                buf.extend(data)
                last = time.monotonic()
            while b"\n" in buf:
                line, _, rest = bytes(buf).partition(b"\n")
                buf = bytearray(rest)
                self._add("rx", line.rstrip(b"\r").decode("utf-8", "replace"))
            if buf and time.monotonic() - last > 0.4:      # partial line that never got its ending
                self._add("rx", bytes(buf).rstrip(b"\r").decode("utf-8", "replace"))
                buf = bytearray()
            if len(buf) > 65536:
                buf = bytearray()

    def _watchdog(self, ser):
        while True:
            time.sleep(1)
            with self._lock:
                if self._ser is not ser:
                    return
                if time.monotonic() - self._last_activity > self.s.serial_idle_seconds:
                    self.close(f"Closed after {self.s.serial_idle_seconds // 60} minutes without activity")
                    return

    def send(self, text):
        text = clean_command(text)
        with self._lock:
            if self._ser is None:
                raise SerialError("The port is not open.", 409, "closed")
            try:
                self._ser.write(text.encode("ascii") + b"\r\n")
                self._ser.flush()
            except (serial.SerialException, OSError) as exc:
                raise SerialError(f"Write failed: {exc}", 500)
            self._touch()
            self._add("tx", text)

    def reset_node(self):
        """The gateway's own recovery: send +DR with two stop bits, then go back to one."""
        with self._lock:
            if self._ser is None:
                raise SerialError("The port is not open.", 409, "closed")
            ser = self._ser
            try:
                ser.stopbits = serial.STOPBITS_TWO
                ser.write(b"+DR\r\n")
                ser.flush()
                time.sleep(0.2)
                ser.stopbits = serial.STOPBITS_ONE
            except (serial.SerialException, OSError, ValueError) as exc:
                raise SerialError(f"Reset failed: {exc}", 500)
            self._touch()
            self._add("tx", "+DR   (sent with 2 stop bits)")

    def read(self, since=0, active=False):
        with self._lock:
            if active and self._ser is not None:
                self._touch()
            lines = [l for l in self._lines if l["seq"] > since]
            return {"lines": lines, "next": self._seq}

    def clear(self):
        with self._lock:
            self._lines.clear()

    def status(self):
        with self._lock:
            is_open = self._ser is not None
            left = None
            if is_open:
                left = max(0, int(self.s.serial_idle_seconds - (time.monotonic() - self._last_activity)))
            return {
                "enabled": self.s.serial_console,
                "open": is_open,
                "port": self._port if is_open else None,
                "paused": self._paused,
                "idle_left": left,
                "notice": self._notice,
                "ports": self.allowed_ports(),
            }
