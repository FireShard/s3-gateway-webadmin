"""Runtime settings for the web admin, read from environment variables."""
import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    operator_dir: Path
    log_dir: Path
    state_dir: Path
    host: str
    port: int
    password_hash: str
    secret_key: str
    dbup_command: list
    service_name: str
    allow_noauth: bool
    env_file: Path = Path("/opt/s3-gateway/app/.env")   # read-only: GATEWAY_ID shown under the logo
    # Serial page. Node commands go through the gateway's own REST server; the
    # direct console is off unless the installer enables it.
    gateway_api: str = "http://127.0.0.1:9090"
    serial_console: bool = False
    serial_port: str = "auto"          # "auto" = USB serial ports found on this machine
    serial_idle_seconds: int = 600
    serial_pause_command: list = field(default_factory=list)
    serial_resume_command: list = field(default_factory=list)

    @classmethod
    def from_env(cls):
        operator_dir = Path(
            os.getenv("S3_OPERATOR_DIR", str(Path.home() / "S3Gateway"))
        ).expanduser()
        log_dir = Path(os.getenv("S3_LOG_DIR", str(operator_dir / "log")))
        state_dir = Path(os.getenv("S3_WEBADMIN_STATE_DIR", str(operator_dir / ".webadmin")))
        dbup = os.getenv("S3_DBUP_COMMAND", "sudo -n /usr/local/sbin/s3-gateway-dbup")
        service = os.getenv("S3_SERVICE_NAME", "s3-zigbee-gateway")
        pause = os.getenv("S3_SERIAL_PAUSE_COMMAND", f"sudo -n /usr/bin/systemctl stop {service}")
        resume = os.getenv("S3_SERIAL_RESUME_COMMAND", f"sudo -n /usr/bin/systemctl start {service}")
        return cls(
            operator_dir=operator_dir,
            log_dir=log_dir,
            state_dir=state_dir,
            host=os.getenv("S3_WEBADMIN_HOST", "0.0.0.0"),
            port=int(os.getenv("S3_WEBADMIN_PORT", "8080")),
            password_hash=os.getenv("S3_WEBADMIN_PASSWORD_HASH", ""),
            secret_key=os.getenv("S3_WEBADMIN_SECRET_KEY", ""),
            dbup_command=dbup.split(),
            service_name=service,
            allow_noauth=_bool("S3_WEBADMIN_ALLOW_NOAUTH"),
            env_file=Path(os.getenv("S3_GATEWAY_ENV_FILE", "/opt/s3-gateway/app/.env")),
            gateway_api=os.getenv("S3_GATEWAY_API", "http://127.0.0.1:9090").rstrip("/"),
            serial_console=_bool("S3_SERIAL_CONSOLE"),
            serial_port=os.getenv("S3_SERIAL_PORT", "auto").strip() or "auto",
            serial_idle_seconds=max(60, int(os.getenv("S3_SERIAL_IDLE_SECONDS", "600"))),
            serial_pause_command=pause.split(),
            serial_resume_command=resume.split(),
        )

    @property
    def csv_path(self):
        return self.operator_dir / "samplelist.csv"

    @property
    def conf_path(self):
        return self.operator_dir / "pygw_conf.py"
