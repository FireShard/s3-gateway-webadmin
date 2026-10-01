# S3 Gateway Web Admin

A small Flask site that runs on the gateway so the site operator can manage the
gateway from a browser instead of editing files over SSH.

| Page | What it does |
|------|--------------|
| Overview | Service status, device count, pending-changes flag, **Apply** button |
| Devices | Add / edit / remove nodes in `samplelist.csv`, bulk paste, CSV upload/download |
| Serial | Send commands to lamp nodes (poll, blink, lamp on/off/dim, manual override) through the running gateway; optional direct console for raw commands on the gateway's USB port |
| Settings | Edit schedule times, polling gap, cycle time, aggressive-poll window, minimum power, first/second gateway (node ID, PAN ID, channel) in `pygw_conf.py` |
| Logs | View `gateway.log`, `mqtt.log`, `error.log` and rotated copies; tail length, level filter, search, auto-refresh, download |

## How it fits the existing workflow

The site edits the **operator files** in `~/S3Gateway/` (the same files an operator
edits by hand). Saving never touches the running gateway. Pressing **Apply** runs
`sudo /usr/local/sbin/s3-gateway-dbup`, exactly as the manual procedure does:
validate, back up PostgreSQL and production files, sync the DB, restart the gateway.

Every save keeps a timestamped backup in `~/S3Gateway/.webadmin/backups/` (last 30 per file).

## Install

```bash
sudo scripts/deploy-production.sh      # ships the webadmin package with the app
sudo scripts/install-webadmin.sh       # deps, password, sudo rule, systemd unit
```

Then open `http://<gateway-ip>:8080/`.

Change the port/host in `/etc/s3-gateway/webadmin.env`, then
`sudo systemctl restart s3-gateway-webadmin`.
Reset the password with `sudo scripts/install-webadmin.sh` after clearing
`S3_WEBADMIN_PASSWORD_HASH=` in that file.

## Serial page

Two tabs.

**Node controls** (always available). Tick devices from `samplelist.csv` (or type node IDs) and press
Poll, Blink, Lamp on/off, Dim, Dim to level, or Manual override on/off. The page asks the running
gateway to send the command, using the gateway's own REST server on `127.0.0.1:9090`
(`S3_GATEWAY_API`). The gateway keeps the USB port, so nothing is interrupted. The page only
confirms the gateway accepted the request; the lamps' replies show up in the gateway log.
Commands go out in batches of 20 nodes. Sending lamp or override commands to 5 or more devices
asks for confirmation.

| Button | REST route | Serial command |
|--------|-----------|----------------|
| Poll | `poll-node` | `+PM<node>` |
| Blink | `find-me` | `+TFM<node>` |
| Lamp on / off / dim | `on-node` / `off-node` / `dim-node` | `+LCB` / `+LCC` / `+LCD` |
| Dim to level | `dim-level-node/<n>` | `+LC<n><node>` |
| Manual override on / off | `enable-` / `disable-manual-override` | `+LM1` / `+LM0` |

The gateway code does not define what the dim level digits mean (the page offers 0 to 9 and
passes the digit through), so try a level on one lamp first.

**Direct console** (off by default). A minicom-style console: 115200 8N1, each line sent with
CR LF. The gateway holds an exclusive lock on the USB port while it runs, so opening the console
stops the gateway service ("Pause gateway and open") and closing it starts the service again.
Polling and data collection pause in between. Safety nets:

* the session closes itself, and restarts the gateway, after `S3_SERIAL_IDLE_SECONDS` (default 600)
  without a command sent or the page being open and visible;
* if the web admin stops or crashes while the gateway is paused, it starts the gateway again on its
  next start (marker file `~/S3Gateway/.webadmin/serial-paused`);
* a port held by something other than the gateway service (for example minicom) is never touched.

Quick buttons: Identify (`+DS`), Configure gateway 1 / 2 (fills `+ZC` + node ID + PAN ID + channel
from `pygw_conf.py` so you can check it before sending), and Reset gateway node (`+DR` sent with two
stop bits, the same recovery the gateway uses when it detects a hung node).

Enable it with
```bash
sudo env S3_ENABLE_SERIAL_CONSOLE=1 scripts/install-webadmin.sh
```
which adds `/etc/sudoers.d/s3-gateway-webadmin-serial` (exact commands
`systemctl stop|start s3-zigbee-gateway`, nothing else), sets `S3_SERIAL_CONSOLE=1`, and puts the
operator account in the `dialout` group. Turn it off with `S3_ENABLE_SERIAL_CONSOLE=0`.

| Variable | Default | |
|----------|---------|--|
| `S3_GATEWAY_API` | `http://127.0.0.1:9090` | Gateway REST server used by Node controls |
| `S3_SERIAL_CONSOLE` | `0` | Direct console on/off |
| `S3_SERIAL_PORT` | `auto` | `auto` lists `/dev/ttyUSB*`; or a fixed device |
| `S3_SERIAL_IDLE_SECONDS` | `600` | Idle limit (minimum 60) |
| `S3_SERIAL_PAUSE_COMMAND` / `S3_SERIAL_RESUME_COMMAND` | `sudo -n systemctl stop/start <service>` | Written by the installer |

## Security notes

* Login required (password hash, CSRF tokens, lockout after 5 failed attempts). The app refuses to start without a password.
* The only privilege granted is passwordless sudo for `/usr/local/sbin/s3-gateway-dbup` to the operator account.
* Traffic is plain HTTP. Keep it on the site LAN/VPN; do not expose the port to the internet.
* Only whitelisted variables in `pygw_conf.py` can be changed, in place; comments and all other settings are preserved, and `pygw_conf.py` is never executed by the site.
* Serial: node commands are limited to the fixed set above and 4-hex-character node IDs. The direct console only opens ports it detects (or `S3_SERIAL_PORT`), accepts printable ASCII lines up to 96 characters, and needs the extra sudo rule above. It stays off until you enable it.
* Log viewing is limited to `gateway|mqtt|error.log[.YYYY-MM-DD]` in the log directory.

## Development

```bash
S3_OPERATOR_DIR=/tmp/S3Gateway S3_LOG_DIR=/tmp/S3Gateway/log \
S3_WEBADMIN_ALLOW_NOAUTH=1 S3_DBUP_COMMAND="echo dbup" python -m webadmin
python -m unittest tests.test_webadmin
```
