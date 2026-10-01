import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

from webadmin import confstore, csvstore, logview
from webadmin.app import create_app
from webadmin.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
GW = ROOT / "PYSerialGateway"


def make_settings(tmp, dbup=None):
    op = Path(tmp) / "S3Gateway"
    (op / "log").mkdir(parents=True)
    shutil.copy(GW / "samplelist.csv", op / "samplelist.csv")
    shutil.copy(GW / "pygw_conf.py", op / "pygw_conf.py")
    return Settings(
        operator_dir=op, log_dir=op / "log", state_dir=op / ".webadmin", host="127.0.0.1", port=0,
        password_hash=generate_password_hash("secret-pass"), secret_key="test",
        dbup_command=dbup or ["true"], service_name="", allow_noauth=False,
    )


class CsvTests(unittest.TestCase):
    def test_clean_row_normalises(self):
        r = csvstore.clean_row({"pole_node": "R1-2", "node": "8eed", "pan_id": "1001", "channel": "20"})
        self.assertEqual(r["node"], "8EED")

    def test_rejects_bad_values(self):
        for bad in ({"node": "XYZ1"}, {"channel": "10"}, {"channel": "27"}, {"pan_id": "12345"}, {"pole_node": "a b"}):
            row = {"pole_node": "R1", "node": "8EED", "pan_id": "1001", "channel": "20", **bad}
            with self.assertRaises(csvstore.CsvError):
                csvstore.clean_row(row)

    def test_duplicates_and_empty(self):
        a = csvstore.clean_row({"pole_node": "R1", "node": "8EED", "pan_id": "1001", "channel": "20"})
        b = dict(a, pole_node="R2")
        with self.assertRaises(csvstore.CsvError):
            csvstore.check_unique([a, b])
        with self.assertRaises(csvstore.CsvError):
            csvstore.save("/tmp/x.csv", [], "/tmp")

    def test_bulk_defaults_and_errors(self):
        rows, errs = csvstore.parse_bulk("R1,8EED\nR2,8EEE,1002,15\nbad\n", "1001", "20")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["pan_id"], "1001")
        self.assertEqual(rows[1]["channel"], "15")
        self.assertEqual(len(errs), 1)

    def test_real_file_round_trips_dbup_header(self):
        rows = csvstore.load(GW / "samplelist.csv")
        self.assertTrue(csvstore.dumps(rows).startswith("pole_node,node,pan_id,channel\n"))


class ConfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = Path(self.tmp) / "pygw_conf.py"
        shutil.copy(GW / "pygw_conf.py", self.conf)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_load_reads_real_values(self):
        v = confstore.load(self.conf)["values"]
        self.assertEqual(v["pollinggap"], 3)
        self.assertEqual(v["active_time"], "19:30")
        self.assertEqual(v["first_GW_data"], ("FE01", "1001", "20"))

    def test_save_preserves_comments_and_other_lines(self):
        before = self.conf.read_text()
        clean, errs = confstore.validate({"pollinggap": "5", "active_time": "18:45", "first_GW_data": ["fe09", "abcd", "15"]})
        self.assertFalse(errs)
        self.assertTrue(confstore.save(self.conf, clean, Path(self.tmp) / "bk"))
        after = self.conf.read_text()
        v = confstore.load(self.conf)["values"]
        self.assertEqual((v["pollinggap"], v["active_time"], v["first_GW_data"]), (5, "18:45", ("FE09", "ABCD", "15")))
        # every comment survives and unrelated settings are untouched
        self.assertEqual(re.findall(r"#[^\n]*", before), re.findall(r"#[^\n]*", after))
        self.assertIn("cycletime = 1400", after)
        self.assertIn("client_ID_2 = 'SAMPLE2HERE'", after)
        self.assertTrue(list((Path(self.tmp) / "bk").glob("pygw_conf-*.py")))

    def test_validation(self):
        _, errs = confstore.validate({"active_time": "7:5", "pollinggap": "0", "first_GW_data": ["FE01", "1001", "30"],
                                      "minimum_power": "abc"})
        self.assertEqual(set(errs), {"active_time", "pollinggap", "first_GW_data", "minimum_power"})

    def test_unchanged_save_is_noop(self):
        clean, _ = confstore.validate({"pollinggap": "3"})
        self.assertFalse(confstore.save(self.conf, clean, Path(self.tmp) / "bk"))

    def test_crlf_file(self):
        self.conf.write_bytes(self.conf.read_bytes().replace(b"\n", b"\r\n"))
        clean, _ = confstore.validate({"pollinggap": "7"})
        confstore.save(self.conf, clean, Path(self.tmp) / "bk")
        self.assertEqual(confstore.load(self.conf)["values"]["pollinggap"], 7)
        self.assertNotIn(b"\r\r", self.conf.read_bytes())

    def test_cycle_warning(self):
        self.assertIsNotNone(confstore.cycle_warning({"cycletime": 100, "pollinggap": 3}, 50))
        self.assertIsNone(confstore.cycle_warning({"cycletime": 1400, "pollinggap": 3}, 50))


class LogTests(unittest.TestCase):
    def test_tail_filter_and_names(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "gateway.log").write_text("\n".join(f"line {i} {'ERROR' if i % 10 == 0 else 'INFO'}" for i in range(100)))
            (Path(d) / "gateway.log.2026-01-02").write_text("old")
            (Path(d) / "secret.txt").write_text("nope")
            files = logview.list_files(d)
            self.assertEqual(files["gateway"], ["gateway.log", "gateway.log.2026-01-02"])
            self.assertIsNone(logview.resolve(d, "../etc/passwd"))
            self.assertIsNone(logview.resolve(d, "secret.txt"))
            r = logview.read(logview.resolve(d, "gateway.log"), 5)
            self.assertEqual(r["lines"][-1], "line 99 INFO")
            r = logview.read(logview.resolve(d, "gateway.log"), 50, level="ERROR")
            self.assertEqual(r["matched"], 10)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = make_settings(self.tmp)
        self.app = create_app(self.s)
        self.c = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def login(self):
        html = self.c.get("/login").get_data(as_text=True)
        tok = re.search(r'name="_csrf" value="(\w+)"', html).group(1)
        r = self.c.post("/login", data={"password": "secret-pass", "_csrf": tok})
        self.assertEqual(r.status_code, 302)

    def tok(self, path="/devices"):
        return re.search(r'name="_csrf" value="(\w+)"', self.c.get(path).get_data(as_text=True)).group(1)

    def test_requires_login_and_password(self):
        self.assertEqual(self.c.get("/devices").status_code, 302)
        self.assertEqual(self.c.get("/api/logs").status_code, 401)
        tok = re.search(r'name="_csrf" value="(\w+)"', self.c.get("/login").get_data(as_text=True)).group(1)
        r = self.c.post("/login", data={"password": "wrong", "_csrf": tok})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get("/devices").status_code, 302)

    def test_refuses_to_start_without_password(self):
        self.s.password_hash = ""
        with self.assertRaises(RuntimeError):
            create_app(self.s)

    def test_csrf_enforced(self):
        self.login()
        r = self.c.post("/devices/add", data={"pole_node": "R9", "node": "AAAA", "pan_id": "1001", "channel": "20"})
        self.assertEqual(r.status_code, 400)

    def test_add_edit_delete_device(self):
        self.login()
        t = self.tok()
        self.c.post("/devices/add", data={"_csrf": t, "pole_node": "R1-2", "node": "8eed", "pan_id": "1001", "channel": "20"})
        rows = csvstore.load(self.s.csv_path)
        self.assertEqual([r["node"] for r in rows], ["2001", "8EED"])
        self.c.post("/devices/edit", data={"_csrf": t, "original_node": "8EED", "pole_node": "R1-2", "node": "8EEF", "pan_id": "1001", "channel": "15"})
        self.assertEqual(csvstore.load(self.s.csv_path)[1]["node"], "8EEF")
        # duplicate rejected
        self.c.post("/devices/add", data={"_csrf": t, "pole_node": "R7", "node": "8EEF", "pan_id": "1001", "channel": "20"})
        self.assertEqual(len(csvstore.load(self.s.csv_path)), 2)
        self.c.post("/devices/delete", data={"_csrf": t, "node": "8EEF"})
        self.c.post("/devices/delete", data={"_csrf": t, "node": "2001"})  # last one: must be refused
        self.assertEqual([r["node"] for r in csvstore.load(self.s.csv_path)], ["2001"])
        self.assertTrue(list((self.s.state_dir / "backups").glob("samplelist-*.csv")))

    def test_settings_save_and_reject(self):
        self.login()
        t = self.tok("/settings")
        base = {"_csrf": t, "active_time": "19:30", "inactive_time": "06:46", "LM_active_time": "19:00",
                "node_off_time": "07:00", "GPS_poll_time": "01:00", "pollinggap": "4", "cycletime": "1400",
                "aggressive_poll_duration_mins": "90", "minimum_power": "15.0",
                "first_GW_data__0": "FE01", "first_GW_data__1": "1001", "first_GW_data__2": "20",
                "second_GW_data__0": "FE02", "second_GW_data__1": "1001", "second_GW_data__2": "20"}
        r = self.c.post("/settings", data=base)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(confstore.load(self.s.conf_path)["values"]["pollinggap"], 4)
        r = self.c.post("/settings", data=dict(base, pollinggap="-1"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(confstore.load(self.s.conf_path)["values"]["pollinggap"], 4)
        self.assertEqual(self.c.get("/settings").status_code, 200)

    def test_logs_api_blocks_traversal(self):
        self.login()
        (self.s.log_dir / "gateway.log").write_text("hello\nworld\n")
        r = self.c.get("/api/logs?file=gateway.log&q=wor")
        self.assertEqual(r.get_json()["lines"], ["world"])
        self.assertEqual(self.c.get("/api/logs?file=../pygw_conf.py").status_code, 404)
        self.assertEqual(self.c.get("/logs/download?file=../samplelist.csv").status_code, 404)
        self.assertEqual(self.c.get("/logs").status_code, 200)

    def test_apply_runs_command_and_tracks_state(self):
        self.login()
        self.assertIsNone(self.app.extensions["s3"] and None)
        t = self.tok("/")
        self.assertEqual(self.c.get("/").status_code, 200)
        self.c.post("/apply", data={"_csrf": t})
        for _ in range(50):
            st = self.c.get("/api/apply-status").get_json()
            if not st["running"] and st["rc"] is not None:
                break
            time.sleep(0.1)
        self.assertEqual(st["rc"], 0)
        self.assertIs(st["pending"], False)
        # editing after apply flips it to pending
        t = self.tok()
        self.c.post("/devices/add", data={"_csrf": t, "pole_node": "R5", "node": "ABCD", "pan_id": "1001", "channel": "20"})
        self.assertIs(self.c.get("/api/apply-status").get_json()["pending"], True)

    def test_all_pages_render(self):
        self.login()
        for p in ("/", "/devices", "/serial", "/settings", "/logs"):
            self.assertEqual(self.c.get(p).status_code, 200, p)


if __name__ == "__main__":
    unittest.main()


class WebadminDeployAssetsTests(unittest.TestCase):
    def test_sudoers_is_limited_to_dbup(self):
        text = (ROOT / "deploy/sudoers/s3-gateway-webadmin").read_text()
        self.assertIn("NOPASSWD: /usr/local/sbin/s3-gateway-dbup", text)
        self.assertNotIn("NOPASSWD: ALL", text)

    def test_unit_and_installer(self):
        unit = (ROOT / "deploy/systemd/s3-gateway-webadmin.service").read_text()
        self.assertIn("-m webadmin", unit)
        self.assertNotIn("User=root", unit)
        inst = (ROOT / "scripts/install-webadmin.sh").read_text()
        self.assertIn("visudo -cf", inst)
        self.assertIn("waitress", (ROOT / "requirements.txt").read_text())
