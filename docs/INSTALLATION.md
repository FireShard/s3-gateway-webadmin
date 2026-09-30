# Installing the S3 Gateway Web Admin

Installation takes about five minutes and three commands. You do it once per gateway.

## Before you start

You need:

* A Raspberry Pi gateway that already has the **S3 gateway software deployed** (the `/opt/s3-gateway/app` folder exists with its `.venv`).
* The operator files `samplelist.csv` and `pygw_conf.py` in `~/S3Gateway/`.
* `sudo` access on the gateway, and a terminal (SSH or keyboard) on it.
* The gateway connected to the network and able to download Python packages (the installer runs `pip install`).
* The **source folder** for this release on the gateway, for example `~/gateway-test/s3-zigbee-gateway`.

> If the gateway was set up with the normal production procedure, all of the above is already true.

---

## Step 1: Deploy the gateway software

This copies the application, **including the web admin code**, to `/opt/s3-gateway/app`. Your `.env`, `.venv`, node list, configuration and logs are preserved.

```bash
cd ~/gateway-test/s3-zigbee-gateway
sudo ./scripts/deploy-production.sh
```

## Step 2: Install the web admin

```bash
sudo ./scripts/install-webadmin.sh
```

The script does everything for you: it installs the Python packages, creates the configuration file, sets up the service and the one `sudo` permission the site needs, and starts it.

**When it asks, choose a login password.**

```
Set the web admin password.
New web admin password:
Repeat:
```

* Use **at least 8 characters**.
* Nothing is shown while you type. That is normal.

When it finishes you will see:

```
Web admin is running:  http://192.168.1.50:8080/
Sudo rule: pi may run only /usr/local/sbin/s3-gateway-dbup as root.
```

## Step 3: Open the website

On any computer or phone **on the same network** as the gateway, open the address the installer printed:

```
http://<gateway-ip>:8080/
```

Log in with the password you just chose. You should see the login page and then the Overview page:

![Login page](images/01-login.png)

That's it. Continue with the [User Guide](USER-GUIDE.md).

---

## Check that it is working

```bash
sudo systemctl status s3-gateway-webadmin
```

You should see `active (running)`. To find the gateway's IP address if you have forgotten it:

```bash
hostname -I
```

On the Overview page, **Gateway service** should show a green `active`.

---

## Everyday maintenance

### Change the password

1. Open the settings file and empty the hash line so it reads `S3_WEBADMIN_PASSWORD_HASH=`

   ```bash
   sudo nano /etc/s3-gateway/webadmin.env
   ```

2. Run the installer again. It will ask for a new password:

   ```bash
   cd ~/gateway-test/s3-zigbee-gateway
   sudo ./scripts/install-webadmin.sh
   ```

### Change the port or address

1. Edit the settings file:

   ```bash
   sudo nano /etc/s3-gateway/webadmin.env
   ```

   ```ini
   S3_WEBADMIN_HOST=0.0.0.0     # 127.0.0.1 = this gateway only
   S3_WEBADMIN_PORT=8080
   ```

2. Restart the site:

   ```bash
   sudo systemctl restart s3-gateway-webadmin
   ```

### Upgrade to a newer release

Repeat Steps 1 and 2 with the new source folder. Your password, port and all site files are kept, and the installer restarts the website for you.

### Stop or disable the site

```bash
sudo systemctl stop s3-gateway-webadmin        # stop for now
sudo systemctl disable --now s3-gateway-webadmin   # stop and don't start at boot
```

This does not affect the gateway itself; it keeps polling and reporting as before.

### Remove it completely

```bash
sudo systemctl disable --now s3-gateway-webadmin
sudo rm /etc/systemd/system/s3-gateway-webadmin.service
sudo rm /etc/sudoers.d/s3-gateway-webadmin
sudo rm /etc/s3-gateway/webadmin.env
sudo systemctl daemon-reload
```

---

## Troubleshooting

| Problem | What to do |
|---|---|
| **The page won't load** | Check the site is running (`sudo systemctl status s3-gateway-webadmin`). Make sure your computer is on the same network as the gateway and you used the right IP (`hostname -I`) and port (default `8080`). |
| **Installer says "Production virtualenv missing"** or "webadmin package not found" | Run Step 1 (`deploy-production.sh`) first. |
| **Installer says "Operator files missing"** | `samplelist.csv` and `pygw_conf.py` must exist in `~/S3Gateway/`. Deploying (Step 1) creates them if they are missing. |
| **Installer says "Run with sudo/root"** | Put `sudo` in front of the command. |
| **"Web admin failed to start"** | The installer prints the last log lines. To see more: `sudo journalctl -u s3-gateway-webadmin -n 50 --no-pager`. |
| **Error: `S3_WEBADMIN_PASSWORD_HASH is not set`** | No password has been created. Re-run `sudo ./scripts/install-webadmin.sh`. |
| **"Too many attempts. Try again in ..."** | After 5 wrong passwords the login locks for 60 seconds. Wait and try again. |
| **Gateway ID is missing under the logo** | The site could not read `GATEWAY_ID` from `/opt/s3-gateway/app/.env`. It is optional; everything else still works. Re-run Step 2 so the service picks up the right file access. |
| **Apply seems stuck on "Starting production gateway..."** | See below. |

### Apply is stuck on "Starting production gateway..."

The gateway service is waiting in a queue behind another service that has not finished. Run:

```bash
systemctl list-jobs
systemctl status s3-gateway-firstboot --no-pager
```

If `s3-zigbee-gateway.service` is listed as a waiting `start` job and the first-boot service shows `activating (start)`, stop the first-boot service so the queued start can run:

```bash
sudo systemctl stop s3-gateway-firstboot
```

The website stops waiting after 15 minutes (`S3_WEBADMIN_APPLY_TIMEOUT`) and shows what happened in the output box.

---

## What the installer changes on the gateway

| Item | Location |
|---|---|
| Python packages | `/opt/s3-gateway/app/.venv` |
| Site configuration (password hash, port) | `/etc/s3-gateway/webadmin.env` (root only) |
| Systemd service | `/etc/systemd/system/s3-gateway-webadmin.service` |
| Sudo permission | `/etc/sudoers.d/s3-gateway-webadmin` (allows only `s3-gateway-dbup`) |

The installer is safe to run again: it keeps the existing configuration and password unless you cleared the hash.

> **Security reminder:** the website uses plain HTTP. Use it only on the site LAN or VPN, and never forward its port to the internet.
