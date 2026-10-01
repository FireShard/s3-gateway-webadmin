import fcntl
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from webadmin import serialtool
from webadmin.app import create_app
from webadmin.serialtool import GatewayClient, SerialConsole, SerialError
try:
    from tests.test_webadmin import make_settings, ROOT
except ImportError:          # run as `python -m unittest discover tests`
    from test_webadmin import make_settings, ROOT


class FakeGateway:
    """Stands in for the gateway's REST server (port 9090) and records the paths it is asked for."""

    def __init__(self, status=200):
        self.paths = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                code = 404 if self.path == "/" else (status if callable(status) is False else status(self.path))
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id": 1}).encode())

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ValidationTests(unittest.TestCase):
    def test_nodes_are_normalised_and_checked(self):
        self.assertEqual(serialtool.clean_nodes(["8eed", "8EED", " 2001 "]), ["8EED", "2001"])
        for bad in ([], None, "8EED", ["8EE"], ["8EEDD"], ["GGGG"], ["8EED", ""], ["../x"]):
            with self.assertRaises(SerialError, msg=repr(bad)):
                serialtool.clean_nodes(bad)
        with self.assertRaises(SerialError):
            serialtool.clean_nodes(["%04X" % i for i in range(serialtool.MAX_NODES + 1)])

    def test_console_commands_are_printable_ascii(self):
        self.assertEqual(serialtool.clean_command("  +DS "), "+DS")
        for bad in ("", "   ", "+DS\r\n+DR", "+D\x00S", "café", "x" * 97):
            with self.assertRaises(SerialError, msg=repr(bad)):
                serialtool.clean_command(bad)


class GatewayClientTests(unittest.TestCase):
    def test_paths_and_bundling(self):
        gw = FakeGateway()
        self.addCleanup(gw.stop)
        client = GatewayClient(gw.url)
        nodes = ["%04X" % (0x2000 + i) for i in range(45)]
        res = client.send("lamp-off", nodes)
        self.assertEqual(res["sent"], 45)
        self.assertEqual(len(gw.paths), 3)                      # 20 + 20 + 5
        self.assertEqual(gw.paths[0], "/gateway-serial-listener/off-node/" + "-".join(nodes[:20]))
        self.assertEqual(gw.paths[2].rsplit("/", 1)[1], "-".join(nodes[40:]))

    def test_every_action_maps_to_the_gateway_route(self):
        gw = FakeGateway()
        self.addCleanup(gw.stop)
        client = GatewayClient(gw.url)
        want = {"poll": "poll-node", "blink": "find-me", "lamp-on": "on-node", "lamp-off": "off-node",
                "dim": "dim-node", "override-on": "enable-manual-override", "override-off": "disable-manual-override"}
        for action, route in want.items():
            client.send(action, ["2001"])
        client.send("dim-level", ["2001", "2002"], level="7")
        self.assertEqual(gw.paths[:-1], ["/gateway-serial-listener/%s/2001" % r for r in want.values()])
        self.assertEqual(gw.paths[-1], "/gateway-serial-listener/dim-level-node/7/2001-2002")

    def test_these_routes_exist_in_the_gateway_rest_api(self):
        """The routes we call must match what the gateway registers (kept in sync by hand otherwise)."""
        rest = ROOT / "pyserialgateway/PYGatewayListener/rest_api.py"
        if not rest.exists():
            self.skipTest("gateway source (pyserialgateway/) not in this checkout")
        text = rest.read_text()
        for endpoint, *_ in serialtool.ACTIONS.values():
            self.assertIn("/gateway-serial-listener/%s/" % endpoint, text)
        self.assertIn("/gateway-serial-listener/dim-level-node/", text)

    def test_rejects_bad_input_before_calling_the_gateway(self):
        gw = FakeGateway()
        self.addCleanup(gw.stop)
        client = GatewayClient(gw.url)
        for args in (("nope", ["2001"], None), ("dim-level", ["2001"], None), ("dim-level", ["2001"], "10"),
                     ("dim-level", ["2001"], "A"), ("poll", ["12"], None)):
            with self.assertRaises(SerialError, msg=repr(args)):
                client.send(args[0], args[1], args[2])
        self.assertEqual(gw.paths, [])

    def test_gateway_down_and_refusals(self):
        with self.assertRaises(SerialError) as cm:
            GatewayClient("http://127.0.0.1:%d" % free_port()).send("poll", ["2001"])
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "gateway-down"))
        self.assertFalse(GatewayClient("http://127.0.0.1:%d" % free_port()).reachable())
        gw = FakeGateway(status=lambda path: 400 if "2002" in path else 200)
        self.addCleanup(gw.stop)
        self.assertTrue(GatewayClient(gw.url).reachable())
        res = GatewayClient(gw.url).send("poll", ["2002"])
        self.assertEqual((res["sent"], res["failed"]), (0, ["2002"]))


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.s = make_settings(self.tmp)
        self.s.serial_console = True
        self.s.serial_port = "loop://"
        self.s.serial_idle_seconds = 600
        self.flag = Path(self.tmp) / "calls"
        self.s.serial_pause_command = ["sh", "-c", "echo pause >> %s" % self.flag]
        self.s.serial_resume_command = ["sh", "-c", "echo resume >> %s" % self.flag]
        self.service = "active"
        self.con = SerialConsole(self.s, service_state=lambda: self.service)
        self.addCleanup(self.con.close)

    def calls(self):
        return self.flag.read_text().split() if self.flag.exists() else []

    def wait_for(self, cond, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_open_send_read_close(self):
        self.con.open()
        self.assertTrue(self.con.status()["open"])
        self.con.send("+DS")
        # loop:// hands our own bytes back, which is enough to exercise the reader
        self.assertTrue(self.wait_for(lambda: any(l["dir"] == "rx" for l in self.con.read(0)["lines"])))
        lines = self.con.read(0)["lines"]
        self.assertEqual([(l["dir"], l["text"]) for l in lines if l["dir"] != "sys"][:2], [("tx", "+DS"), ("rx", "+DS")])
        nxt = self.con.read(0)["next"]
        self.assertEqual(self.con.read(nxt)["lines"], [])
        self.con.close()
        self.assertFalse(self.con.status()["open"])
        with self.assertRaises(SerialError) as cm:
            self.con.send("+DS")
        self.assertEqual(cm.exception.code, "closed")
        self.assertEqual(self.calls(), [])            # never paused anything: the port was free

    def test_partial_line_is_flushed(self):
        self.con.open()
        self.con._ser.write(b"#H1|2001|0001|")            # no line ending
        self.assertTrue(self.wait_for(lambda: any(l["text"] == "#H1|2001|0001|" for l in self.con.read(0)["lines"]), 3))

    def test_only_detected_ports_can_be_opened(self):
        with mock.patch.object(self.con, "_open_serial") as opener:
            for bad in ("/etc/passwd", "socket://127.0.0.1:1", "/dev/ttyUSB9"):
                with self.assertRaises(SerialError, msg=bad):
                    self.con.open(bad)
            opener.assert_not_called()
        self.s.serial_port = "auto"
        with mock.patch.object(serialtool, "usb_ports", return_value=[]):
            with self.assertRaises(SerialError) as cm:
                self.con.open()
        self.assertEqual(cm.exception.code, "no-port")
        with mock.patch.object(serialtool, "usb_ports", return_value=[{"device": "loop://", "description": ""}]):
            self.con.open("loop://")                      # a detected port is accepted
        self.assertTrue(self.con.status()["open"])

    def test_port_busy_probe_sees_the_gateways_lock(self):
        path = Path(self.tmp) / "fake-tty"
        path.write_text("")
        self.assertFalse(self.con._port_busy(str(path)))
        holder = os.open(path, os.O_RDWR)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertTrue(self.con._port_busy(str(path)))
        finally:
            os.close(holder)
        self.assertFalse(self.con._port_busy(str(path)))
        self.assertFalse(self.con._port_busy("loop://"))

    def test_busy_port_without_permission_to_pause(self):
        with mock.patch.object(self.con, "_port_busy", return_value=True):
            with self.assertRaises(SerialError) as cm:
                self.con.open(pause=False)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "busy"))
        self.assertEqual(self.calls(), [])

    def test_pause_then_resume(self):
        busy = iter([True, True, False])            # locked, locked while stopping, then released
        with mock.patch.object(self.con, "_port_busy", side_effect=lambda p: next(busy, False)), \
             mock.patch.object(serialtool.time, "sleep", lambda s: None):
            self.con.open(pause=True)
        st = self.con.status()
        self.assertTrue(st["open"] and st["paused"])
        self.assertEqual(self.calls(), ["pause"])
        self.assertTrue(self.con._marker.exists())
        self.assertEqual(self.con.close(), "")
        self.assertEqual(self.calls(), ["pause", "resume"])
        self.assertFalse(self.con._marker.exists())
        self.assertFalse(self.con.status()["paused"])
        self.assertIn("Gateway service restarted", [l["text"] for l in self.con.read(0)["lines"]])

    def test_pause_failure_leaves_everything_alone(self):
        self.s.serial_pause_command = ["sh", "-c", "echo 'sudo: a password is required' >&2; exit 1"]
        with mock.patch.object(self.con, "_port_busy", return_value=True):
            with self.assertRaises(SerialError) as cm:
                self.con.open(pause=True)
        self.assertIn("sudo rule", cm.exception.message)
        self.assertFalse(self.con.status()["open"] or self.con.status()["paused"])
        self.assertFalse(self.con._marker.exists())

    def test_port_still_locked_after_pause_restarts_the_gateway(self):
        with mock.patch.object(self.con, "_port_busy", return_value=True), \
             mock.patch.object(serialtool.time, "sleep", lambda s: None):
            with self.assertRaises(SerialError):
                self.con.open(pause=True)
        self.assertEqual(self.calls(), ["pause", "resume"])
        self.assertFalse(self.con.status()["paused"])

    def test_lock_held_by_something_else_is_not_paused(self):
        self.service = "inactive"
        with mock.patch.object(self.con, "_port_busy", return_value=True):
            with self.assertRaises(SerialError) as cm:
                self.con.open(pause=True)
        self.assertEqual(cm.exception.code, "busy-other")
        self.assertEqual(self.calls(), [])

    def test_resume_failure_is_reported_and_marker_kept(self):
        busy = iter([True, False])
        with mock.patch.object(self.con, "_port_busy", side_effect=lambda p: next(busy, False)), \
             mock.patch.object(serialtool.time, "sleep", lambda s: None):
            self.con.open(pause=True)
        self.s.serial_resume_command = ["sh", "-c", "echo boom >&2; exit 3"]
        err = self.con.close()
        self.assertIn("Could not restart the gateway", err)
        self.assertTrue(self.con._marker.exists())
        self.assertTrue(self.con.status()["paused"])
        self.assertEqual(self.con.status()["notice"], err)

    def test_idle_session_closes_and_resumes(self):
        self.s.serial_idle_seconds = 1
        busy = iter([True, False])
        with mock.patch.object(self.con, "_port_busy", side_effect=lambda p: next(busy, False)), \
             mock.patch.object(serialtool.time, "sleep", lambda s: None):
            self.con.open(pause=True)
        self.assertTrue(self.wait_for(lambda: not self.con.status()["open"], 6))
        self.assertEqual(self.calls(), ["pause", "resume"])

    def test_only_active_polling_keeps_a_session_alive(self):
        self.con.open()
        time.sleep(1.2)
        self.con.read(0)                              # passive read: must not reset the timer
        mid = self.con.status()["idle_left"]
        self.assertLessEqual(mid, self.s.serial_idle_seconds - 1)
        self.con.read(0, active=True)                 # the visible page: resets it
        self.assertGreater(self.con.status()["idle_left"], mid)

    def test_startup_recovery_after_a_crash(self):
        self.s.state_dir.mkdir(parents=True)
        self.con._marker.write_text("{}")
        self.con.recover()
        self.assertEqual(self.calls(), ["resume"])
        self.assertFalse(self.con._marker.exists())

    def test_reset_node_uses_two_stop_bits_then_restores_one(self):
        self.con.open()
        seen = []
        real = type(self.con._ser).stopbits
        with mock.patch.object(type(self.con._ser), "stopbits", new=property(real.fget, lambda o, v: (seen.append(v), real.fset(o, v))[1])):
            self.con.reset_node()
        self.assertEqual(seen, [2, 1])
        self.assertTrue(any(l["dir"] == "tx" and l["text"].startswith("+DR") for l in self.con.read(0)["lines"]))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.gw = FakeGateway()
        self.addCleanup(self.gw.stop)
        self.s = make_settings(self.tmp)
        self.s.gateway_api = self.gw.url
        self.s.serial_console = True
        self.s.serial_port = "loop://"
        self.app = create_app(self.s)
        self.addCleanup(self.app.extensions["s3_console"].close)
        self.c = self.app.test_client()

    def login(self):
        html = self.c.get("/login").get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="(\w+)"', html).group(1)
        self.c.post("/login", data={"password": "secret-pass", "_csrf": tok})
        page = self.c.get("/serial").get_data(as_text=True)
        self.tok = re.search(r'const CSRF = "(\w+)"', page).group(1)
        return page

    def post(self, path, body=None, csrf=True):
        return self.c.post(path, json=body or {}, headers={"X-CSRF-Token": self.tok} if csrf else {})

    def test_page_needs_login_and_renders(self):
        self.assertEqual(self.c.get("/serial").status_code, 302)
        self.assertEqual(self.c.get("/api/serial/status").status_code, 401)
        page = self.login()
        self.assertIn("Node controls", page)
        self.assertIn("2001", page)                       # device from samplelist.csv
        self.assertIn("+ZCFE01100120", page)              # gateway 1 config from pygw_conf.py
        self.assertIn('href="/serial"', page)             # nav entry

    def test_page_when_console_is_off(self):
        self.s.serial_console = False
        page = self.login()
        self.assertIn("The direct console is turned off", page)
        self.assertNotIn('id="cterm"', page)
        self.assertEqual(self.post("/api/serial/open").status_code, 403)
        self.assertEqual(self.post("/api/serial/send", {"text": "+DS"}).status_code, 403)
        self.assertEqual(self.post("/api/serial/reset-node").status_code, 403)

    def test_writes_need_the_csrf_header(self):
        self.login()
        for path in ("/api/serial/node-command", "/api/serial/open", "/api/serial/send",
                     "/api/serial/close", "/api/serial/reset-node", "/api/serial/clear"):
            self.assertEqual(self.post(path, csrf=False).status_code, 400, path)
        self.assertEqual(self.gw.paths, [])

    def test_node_command_round_trip(self):
        self.login()
        r = self.post("/api/serial/node-command", {"action": "lamp-on", "nodes": ["2001", "8eed"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.get_json()["ok"], r.get_json()["label"], r.get_json()["sent"]), (True, "Lamp on", 2))
        self.assertEqual(self.gw.paths, ["/gateway-serial-listener/on-node/2001-8EED"])
        r = self.post("/api/serial/node-command", {"action": "dim-level", "nodes": ["2001"], "level": "4"})
        self.assertEqual(r.get_json()["label"], "Dim to level 4")
        for bad in ({"action": "lamp-on", "nodes": ["zzzz"]}, {"action": "x", "nodes": ["2001"]},
                    {"action": "lamp-on"}, {"action": "dim-level", "nodes": ["2001"], "level": "99"}):
            self.assertEqual(self.post("/api/serial/node-command", bad).status_code, 400, bad)
        self.assertEqual(len(self.gw.paths), 2)

    def test_node_command_when_gateway_is_down(self):
        self.s.gateway_api = "http://127.0.0.1:%d" % free_port()
        self.app = create_app(self.s)
        self.c = self.app.test_client()
        self.login()
        r = self.post("/api/serial/node-command", {"action": "poll", "nodes": ["2001"]})
        self.assertEqual((r.status_code, r.get_json()["code"]), (503, "gateway-down"))

    def test_console_session_over_http(self):
        self.login()
        self.assertEqual(self.post("/api/serial/send", {"text": "+DS"}).status_code, 409)
        st = self.post("/api/serial/open", {"port": "loop://"}).get_json()
        self.assertTrue(st["open"])
        self.assertEqual(self.post("/api/serial/send", {"text": "+PM2001"}).status_code, 200)
        self.assertEqual(self.post("/api/serial/send", {"text": "bad\x01"}).status_code, 400)
        lines = []
        for _ in range(40):
            lines = self.c.get("/api/serial/read?since=0").get_json()["lines"]
            if any(l["dir"] == "rx" for l in lines):
                break
            time.sleep(0.05)
        self.assertIn(("tx", "+PM2001"), [(l["dir"], l["text"]) for l in lines])
        self.assertIn(("rx", "+PM2001"), [(l["dir"], l["text"]) for l in lines])
        status = self.c.get("/api/serial/status").get_json()
        self.assertTrue(status["open"])
        self.assertIn("gateway_reachable", status)
        self.assertFalse(self.post("/api/serial/close").get_json()["open"])
        self.post("/api/serial/clear")
        self.assertEqual(self.c.get("/api/serial/read?since=0").get_json()["lines"], [])


class DeployAssetTests(unittest.TestCase):
    def test_serial_sudoers_is_exact_commands_only(self):
        text = (ROOT / "deploy/sudoers/s3-gateway-webadmin-serial").read_text()
        rule = [l for l in text.splitlines() if l and not l.startswith("#")]
        self.assertEqual(len(rule), 1)
        self.assertIn("NOPASSWD: __SYSTEMCTL__ stop __SERVICE__, __SYSTEMCTL__ start __SERVICE__", rule[0])
        self.assertNotIn("ALL", rule[0].split("NOPASSWD:")[1])
        self.assertNotIn("*", rule[0])

    def test_installer_only_grants_it_when_asked(self):
        inst = (ROOT / "scripts/install-webadmin.sh").read_text()
        self.assertIn('S3_ENABLE_SERIAL_CONSOLE', inst)
        self.assertIn('if [ "$ENABLE_SERIAL" = "1" ]', inst)
        self.assertIn("visudo -cf \"$TMP_SERIAL\"", inst)
        # the base sudoers file must stay limited to dbup
        base = (ROOT / "deploy/sudoers/s3-gateway-webadmin").read_text()
        self.assertNotIn("systemctl", base)

    def test_console_is_off_by_default(self):
        from webadmin.settings import Settings
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S3_SERIAL_CONSOLE", None)
            self.assertFalse(Settings.from_env().serial_console)
        with mock.patch.dict(os.environ, {"S3_SERIAL_CONSOLE": "1", "S3_SERVICE_NAME": "gw"}):
            s = Settings.from_env()
            self.assertTrue(s.serial_console)
            self.assertEqual(s.serial_pause_command[-2:], ["stop", "gw"])
            self.assertEqual(s.serial_resume_command[-2:], ["start", "gw"])


if __name__ == "__main__":
    unittest.main()
