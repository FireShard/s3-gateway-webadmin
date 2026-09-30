"""Runs `sudo s3-gateway-dbup` in the background and tracks what was applied."""
import json
import subprocess
import threading
import time

from .fileutil import atomic_write, sha256_of


class Applier:
    def __init__(self, settings):
        self.s = settings
        self._lock = threading.Lock()
        self._running = False
        self._started = None
        self._finished = None
        self._rc = None
        self.state_file = settings.state_dir / "applied.json"
        self.output_file = settings.state_dir / "apply-output.log"

    # -- what's on disk vs what was last applied -------------------------------
    def _applied(self):
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def pending(self):
        """None = unknown (never applied via web), True/False otherwise."""
        rec = self._applied()
        if not rec:
            return None
        return (rec.get("csv") != sha256_of(self.s.csv_path)
                or rec.get("conf") != sha256_of(self.s.conf_path))

    def last_applied_at(self):
        return self._applied().get("at")

    # -- running dbup -----------------------------------------------------------
    def status(self):
        with self._lock:
            info = {
                "running": self._running,
                "started": self._started,
                "finished": self._finished,
                "rc": self._rc,
            }
        try:
            info["output"] = self.output_file.read_text(errors="replace")
        except OSError:
            info["output"] = ""
        return info

    def start(self):
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._started = time.time()
            self._finished = None
            self._rc = None
        self.s.state_dir.mkdir(parents=True, exist_ok=True)
        # hashes of the files DBUP is about to install
        snapshot = {"csv": sha256_of(self.s.csv_path), "conf": sha256_of(self.s.conf_path)}
        threading.Thread(target=self._run, args=(snapshot,), daemon=True).start()
        return True

    def _run(self, snapshot):
        rc = -1
        try:
            with open(self.output_file, "w") as out:
                out.write(f"$ {' '.join(self.s.dbup_command)}\n")
                out.flush()
                proc = subprocess.Popen(
                    self.s.dbup_command, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL
                )
                rc = proc.wait()
                out.write(f"\n[exit code {rc}]\n")
        except OSError as exc:
            self.output_file.write_text(f"Could not run DBUP: {exc}\n")
        finally:
            if rc == 0:
                snapshot["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                atomic_write(self.state_file, json.dumps(snapshot))
            with self._lock:
                self._running = False
                self._finished = time.time()
                self._rc = rc


def service_state(name):
    """systemctl is-active for the gateway service (no root needed)."""
    if not name:
        return None
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return None
