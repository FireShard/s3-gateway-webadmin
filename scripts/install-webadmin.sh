#!/bin/bash
# Installs the S3 gateway local web admin (Flask) on an already-deployed gateway.
# Run after scripts/deploy-production.sh:  sudo scripts/install-webadmin.sh
set -euo pipefail

TARGET_DIR="/opt/s3-gateway/app"
ENV_DIR="/etc/s3-gateway"
ENV_FILE="$ENV_DIR/webadmin.env"
SERVICE="s3-gateway-webadmin"
OPERATOR_CONFIG="${S3_OPERATOR_CONFIG:-$ENV_DIR/operator.conf}"
if [ -f "$OPERATOR_CONFIG" ]; then
    # shellcheck disable=SC1090
    . "$OPERATOR_CONFIG"
fi
OPERATOR_USER="${S3_OPERATOR_USER:-${SUDO_USER:-pi}}"
if [ "$OPERATOR_USER" = "root" ]; then OPERATOR_USER="pi"; fi
OPERATOR_HOME="${S3_OPERATOR_HOME:-$(getent passwd "$OPERATOR_USER" | cut -d: -f6)}"
OPERATOR_DIR="${S3_OPERATOR_DIR:-$OPERATOR_HOME/S3Gateway}"
# Direct serial console (Serial page). Unset = leave as is, 1 = enable, 0 = disable.
ENABLE_SERIAL="${S3_ENABLE_SERIAL_CONSOLE:-}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SOURCE_DIR="$(dirname "$SCRIPT_DIR")"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo/root." >&2; exit 1; }
id "$OPERATOR_USER" >/dev/null 2>&1 || { echo "Operator account missing: $OPERATOR_USER" >&2; exit 1; }
[ -x "$TARGET_DIR/.venv/bin/python" ] || { echo "Production virtualenv missing: $TARGET_DIR/.venv (run bootstrap/deploy first)" >&2; exit 1; }
[ -d "$TARGET_DIR/webadmin" ] || { echo "webadmin package not found in $TARGET_DIR - run scripts/deploy-production.sh first." >&2; exit 1; }
[ -x /usr/local/sbin/s3-gateway-dbup ] || { echo "/usr/local/sbin/s3-gateway-dbup missing - run scripts/deploy-production.sh first." >&2; exit 1; }
[ -f "$OPERATOR_DIR/samplelist.csv" ] && [ -f "$OPERATOR_DIR/pygw_conf.py" ] || { echo "Operator files missing in $OPERATOR_DIR" >&2; exit 1; }

echo "Installing web admin dependencies..."
"$TARGET_DIR/.venv/bin/pip" install -r "$TARGET_DIR/requirements.txt"

runuser -u "$OPERATOR_USER" -- "$TARGET_DIR/.venv/bin/python" -c "import sys; sys.path.insert(0,'$TARGET_DIR'); import webadmin.app" \
    || { echo "Operator account '$OPERATOR_USER' cannot import the webadmin package (check permissions on $TARGET_DIR)." >&2; exit 1; }


set_env() {  # set_env KEY VALUE - replace or append KEY=VALUE in the web admin env file
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
    fi
}

install -d -o root -g root -m 755 "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
    install -o root -g root -m 600 "$SOURCE_DIR/deploy/webadmin.env.example" "$ENV_FILE"
    SECRET="$("$TARGET_DIR/.venv/bin/python" -c 'import secrets;print(secrets.token_hex(32))')"
    sed -i "s|^S3_WEBADMIN_SECRET_KEY=.*|S3_WEBADMIN_SECRET_KEY=$SECRET|" "$ENV_FILE"
fi

if ! grep -q '^S3_WEBADMIN_PASSWORD_HASH=.\+' "$ENV_FILE"; then
    echo
    echo "Set the web admin password."
    HASH="$(cd "$TARGET_DIR" && "$TARGET_DIR/.venv/bin/python" -m webadmin.hashpw)"
    # hashes contain '$' and ':' but not '|'
    sed -i "s|^S3_WEBADMIN_PASSWORD_HASH=.*|S3_WEBADMIN_PASSWORD_HASH=$HASH|" "$ENV_FILE"
fi

# sudoers rule (validated before it is installed)
TMP_SUDOERS="$(mktemp)"
sed "s|__OPERATOR_USER__|$OPERATOR_USER|g" "$SOURCE_DIR/deploy/sudoers/s3-gateway-webadmin" > "$TMP_SUDOERS"
visudo -cf "$TMP_SUDOERS" >/dev/null || { rm -f "$TMP_SUDOERS"; echo "Generated sudoers rule failed validation." >&2; exit 1; }
install -o root -g root -m 440 "$TMP_SUDOERS" /etc/sudoers.d/s3-gateway-webadmin
rm -f "$TMP_SUDOERS"

# Optional sudo rule + settings for the direct serial console (pause/resume the gateway service only)
SERIAL_SUDOERS="/etc/sudoers.d/s3-gateway-webadmin-serial"
SERVICE_NAME="$(grep '^S3_SERVICE_NAME=' "$ENV_FILE" | cut -d= -f2 || true)"
SERVICE_NAME="${SERVICE_NAME:-s3-zigbee-gateway}"
if [ "$ENABLE_SERIAL" = "1" ]; then
    SYSTEMCTL="$(readlink -f "$(command -v systemctl)")"
    TMP_SERIAL="$(mktemp)"
    sed -e "s|__OPERATOR_USER__|$OPERATOR_USER|g" -e "s|__SYSTEMCTL__|$SYSTEMCTL|g" -e "s|__SERVICE__|$SERVICE_NAME|g" \
        "$SOURCE_DIR/deploy/sudoers/s3-gateway-webadmin-serial" > "$TMP_SERIAL"
    visudo -cf "$TMP_SERIAL" >/dev/null || { rm -f "$TMP_SERIAL"; echo "Generated serial sudoers rule failed validation." >&2; exit 1; }
    install -o root -g root -m 440 "$TMP_SERIAL" "$SERIAL_SUDOERS"
    rm -f "$TMP_SERIAL"
    set_env S3_SERIAL_CONSOLE 1
    set_env S3_SERIAL_PAUSE_COMMAND "sudo -n $SYSTEMCTL stop $SERVICE_NAME"
    set_env S3_SERIAL_RESUME_COMMAND "sudo -n $SYSTEMCTL start $SERVICE_NAME"
    if ! id -nG "$OPERATOR_USER" | tr ' ' '\n' | grep -qx dialout; then
        getent group dialout >/dev/null 2>&1 || { echo "Group 'dialout' is missing." >&2; exit 1; }
        usermod -a -G dialout "$OPERATOR_USER"
        echo "Added $OPERATOR_USER to the dialout group (needed to open the USB serial port)."
    fi
elif [ "$ENABLE_SERIAL" = "0" ]; then
    rm -f "$SERIAL_SUDOERS"
    set_env S3_SERIAL_CONSOLE 0
fi

sed -e "s|__OPERATOR_USER__|$OPERATOR_USER|g" -e "s|__OPERATOR_DIR__|$OPERATOR_DIR|g" \
    "$SOURCE_DIR/deploy/systemd/s3-gateway-webadmin.service" > "/etc/systemd/system/$SERVICE.service"
chmod 644 "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE" || { journalctl -u "$SERVICE" -n 20 --no-pager >&2; echo "Web admin failed to start." >&2; exit 1; }

PORT="$(grep '^S3_WEBADMIN_PORT=' "$ENV_FILE" | cut -d= -f2)"
echo
echo "Web admin is running:  http://$(hostname -I | awk '{print $1}'):${PORT:-8080}/"
echo "Sudo rule: $OPERATOR_USER may run only /usr/local/sbin/s3-gateway-dbup as root."
if grep -q '^S3_SERIAL_CONSOLE=1' "$ENV_FILE"; then
    echo "Serial console: on. $OPERATOR_USER may also stop/start $SERVICE_NAME (nothing else)."
else
    echo "Serial console: off (Serial page still sends node commands). Enable: sudo env S3_ENABLE_SERIAL_CONSOLE=1 $0"
fi
