"""Runtime settings for the web admin, read from environment variables."""
import os
from dataclasses import dataclass
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

    @classmethod
    def from_env(cls):
        operator_dir = Path(
            os.getenv("S3_OPERATOR_DIR", str(Path.home() / "S3Gateway"))
        ).expanduser()
        log_dir = Path(os.getenv("S3_LOG_DIR", str(operator_dir / "log")))
        state_dir = Path(os.getenv("S3_WEBADMIN_STATE_DIR", str(operator_dir / ".webadmin")))
        dbup = os.getenv("S3_DBUP_COMMAND", "sudo -n /usr/local/sbin/s3-gateway-dbup")
        return cls(
            operator_dir=operator_dir,
            log_dir=log_dir,
            state_dir=state_dir,
            host=os.getenv("S3_WEBADMIN_HOST", "0.0.0.0"),
            port=int(os.getenv("S3_WEBADMIN_PORT", "8080")),
            password_hash=os.getenv("S3_WEBADMIN_PASSWORD_HASH", ""),
            secret_key=os.getenv("S3_WEBADMIN_SECRET_KEY", ""),
            dbup_command=dbup.split(),
            service_name=os.getenv("S3_SERVICE_NAME", "s3-zigbee-gateway"),
            allow_noauth=_bool("S3_WEBADMIN_ALLOW_NOAUTH"),
        )

    @property
    def csv_path(self):
        return self.operator_dir / "samplelist.csv"

    @property
    def conf_path(self):
        return self.operator_dir / "pygw_conf.py"
