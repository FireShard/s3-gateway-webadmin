# S3 Gateway Web Admin

A small Flask site that runs on the gateway so the site operator can manage the
gateway from a browser instead of editing files over SSH.

| Page | What it does |
|------|--------------|
| Overview | Service status, device count, pending-changes flag, **Apply** button |
| Devices | Add / edit / remove nodes in `samplelist.csv`, bulk paste, CSV upload/download |
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

## Security notes

* Login required (password hash, CSRF tokens, lockout after 5 failed attempts). The app refuses to start without a password.
* The only privilege granted is passwordless sudo for `/usr/local/sbin/s3-gateway-dbup` to the operator account.
* Traffic is plain HTTP. Keep it on the site LAN/VPN; do not expose the port to the internet.
* Only whitelisted variables in `pygw_conf.py` can be changed, in place; comments and all other settings are preserved, and `pygw_conf.py` is never executed by the site.
* Log viewing is limited to `gateway|mqtt|error.log[.YYYY-MM-DD]` in the log directory.

## Development

```bash
S3_OPERATOR_DIR=/tmp/S3Gateway S3_LOG_DIR=/tmp/S3Gateway/log \
S3_WEBADMIN_ALLOW_NOAUTH=1 S3_DBUP_COMMAND="echo dbup" python -m webadmin
python -m unittest tests.test_webadmin
```
