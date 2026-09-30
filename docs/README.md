# S3 Gateway Web Admin

A small, local website that runs **on the S3 Zigbee gateway itself** (a Raspberry Pi). It lets the site operator manage the gateway from any browser on the same network, instead of editing files over SSH.

![Overview page](images/02-overview.png)

> **About the screenshots:** all screenshots in these documents were taken from a demo gateway with sample data (14 street-light nodes, gateway ID `GW-KL-0014`). Your own site will show your own devices, settings and logs.

## Documentation

| Document | Read it to... |
|---|---|
| **README.md** (this file) | Understand what the website is, what it can do and how it is built |
| [INSTALLATION.md](INSTALLATION.md) | Install it on a gateway in a few simple steps |
| [USER-GUIDE.md](USER-GUIDE.md) | Learn day-to-day use, with screenshots of every page |

---

## What it does

| Page | Purpose |
|---|---|
| **Overview** | Shows whether the gateway service is running, how many devices are in the list, and whether you have saved changes the gateway is not using yet. Holds the **Apply changes** button. |
| **Devices** | Add, edit and remove street-light nodes (`samplelist.csv`). Supports adding many at once, filtering, downloading the list, and replacing the whole list from a CSV file. |
| **Settings** | Edit the gateway configuration (`pygw_conf.py`) through a validated form. Every field is labelled with its exact variable name. |
| **Logs** | Read `gateway.log`, `mqtt.log` and `error.log` (and their rotated copies) with tail length, level filter, text search, auto-refresh and download. |

The sidebar also shows the **gateway ID** under the logo (read from `GATEWAY_ID` in the gateway's `.env` file), so the operator can tell which site they are looking at.

## How it fits the existing workflow

The website does not replace the existing operator procedure; it is a friendlier front end for it.

```
 Browser  ──►  Web Admin  ──►  ~/S3Gateway/samplelist.csv
                          ──►  ~/S3Gateway/pygw_conf.py        (saved, NOT live yet)
                                       │
                          [ Apply changes ]
                                       ▼
                     sudo /usr/local/sbin/s3-gateway-dbup
        validate ► back up PostgreSQL + files ► sync database ► restart gateway
```

* **Saving** a device or a setting only edits the operator files in `~/S3Gateway/`. The running gateway is not touched.
* **Applying** runs `s3-gateway-dbup`, exactly like the manual `sudo s3-gateway-dbup` procedure: it validates the files, backs up PostgreSQL and the production files, syncs the database and restarts the gateway.
* Devices you remove from the list are **deleted from the database** on Apply.
* Every save keeps a timestamped backup in `~/S3Gateway/.webadmin/backups/` (the last 30 per file).

## Key features

* **Safe editing.** Every value is validated (node IDs are 4 hex characters, Zigbee channels are 11-26, times are `HH:MM`, and so on). If anything is wrong, nothing is saved and the offending field is highlighted.
* **Only whitelisted settings can change**, and they are edited in place: comments and all other lines in `pygw_conf.py` are preserved. The file is never executed by the website.
* **Pending-changes tracking.** The site remembers what was last applied and warns you (banner + Overview status) when the files on disk differ.
* **Live apply output.** Pressing Apply shows the DBUP output on the Overview page as it runs.
* **Schedule preview.** A 24-hour bar on the Settings page draws the lantern ON/OFF window, manual-override window and GPS mapping time as you type.
* **Works on a phone.** The layout adapts to narrow screens.
* **No internet needed at runtime.** Fonts and styles are bundled with the site.

## Security

* Login required (password hash, CSRF tokens, lockout for 60 seconds after 5 failed attempts). The site **refuses to start without a password**.
* The only extra privilege it has is passwordless `sudo` for one command: `/usr/local/sbin/s3-gateway-dbup`, for the operator account.
* The site uses **plain HTTP**. Keep it on the site LAN or VPN and **do not expose the port to the internet**.
* The database password (`db_password`) is never sent back to the browser. Leave the box blank to keep the current one.
* Log viewing is limited to `gateway`, `mqtt` and `error` log files (including dated rotations) in the log directory.

## Technical summary

| Item | Detail |
|---|---|
| Framework | Python / Flask, served by Waitress |
| Runs as | systemd service `s3-gateway-webadmin`, as the operator account (default `pi`) |
| Default address | `http://<gateway-ip>:8080/` |
| Code location | `/opt/s3-gateway/app/webadmin/` |
| Files it edits | `~/S3Gateway/samplelist.csv`, `~/S3Gateway/pygw_conf.py` |
| Logs it reads | `~/S3Gateway/log/` |
| Backups | `~/S3Gateway/.webadmin/backups/` |
| Configuration | `/etc/s3-gateway/webadmin.env` (root only, mode 600) |

### Configuration options (`/etc/s3-gateway/webadmin.env`)

| Variable | Default | Meaning |
|---|---|---|
| `S3_WEBADMIN_HOST` | `0.0.0.0` | Network address to listen on |
| `S3_WEBADMIN_PORT` | `8080` | Port to listen on |
| `S3_WEBADMIN_PASSWORD_HASH` | *(set by installer)* | Hash of the login password |
| `S3_WEBADMIN_SECRET_KEY` | *(set by installer)* | Random string that signs login cookies |
| `S3_SERVICE_NAME` | `s3-zigbee-gateway` | Gateway service whose status is shown on Overview |
| `S3_GATEWAY_ENV_FILE` | `/opt/s3-gateway/app/.env` | File the gateway ID is read from |
| `S3_WEBADMIN_APPLY_TIMEOUT` | `900` | Seconds to wait for Apply before giving up (15 minutes) |

### Source layout

```
webadmin/
├── app.py          # routes: login, overview, devices, settings, logs
├── applier.py      # runs s3-gateway-dbup in the background, tracks pending changes
├── confstore.py    # validated, in-place editing of pygw_conf.py
├── csvstore.py     # reading/validating/writing samplelist.csv
├── logview.py      # safe read-only log access
├── envfile.py      # reads GATEWAY_ID from the gateway .env
├── fileutil.py     # atomic writes + rolling backups
├── hashpw.py       # helper that creates the password hash
├── templates/      # HTML pages
└── static/         # stylesheet and bundled fonts
deploy/
├── systemd/s3-gateway-webadmin.service
├── sudoers/s3-gateway-webadmin
└── webadmin.env.example
scripts/install-webadmin.sh
tests/test_webadmin.py
```

## Development

Run the site on any computer without a real gateway:

```bash
S3_OPERATOR_DIR=/tmp/S3Gateway S3_LOG_DIR=/tmp/S3Gateway/log \
S3_WEBADMIN_ALLOW_NOAUTH=1 S3_DBUP_COMMAND="echo dbup" python -m webadmin
```

Put a `samplelist.csv` and `pygw_conf.py` in `/tmp/S3Gateway`, then open <http://localhost:8080/>. Run the tests with:

```bash
python -m unittest tests.test_webadmin
```

`S3_WEBADMIN_ALLOW_NOAUTH=1` disables the login and is for local testing only.
