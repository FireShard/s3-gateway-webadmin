"""S3 gateway local web admin (Flask)."""
import atexit
import hmac
import secrets
import time
from pathlib import Path

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash

from . import confstore, csvstore, envfile, logview, serialtool
from .applier import Applier, service_state
from .settings import Settings

MAX_FAILS = 5
LOCKOUT_SECONDS = 60


def create_app(settings=None):
    s = settings or Settings.from_env()
    if not s.password_hash and not s.allow_noauth:
        raise RuntimeError(
            "S3_WEBADMIN_PASSWORD_HASH is not set. Generate one with "
            "`python -m webadmin.hashpw` (or set S3_WEBADMIN_ALLOW_NOAUTH=1 for local testing only)."
        )
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=s.secret_key or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=1024 * 1024,
        PERMANENT_SESSION_LIFETIME=8 * 3600,
    )
    app.extensions["s3"] = s
    applier = Applier(s)
    backups = s.state_dir / "backups"
    fails = {}  # ip -> [count, locked_until]
    gateway = serialtool.GatewayClient(s.gateway_api)
    console = serialtool.SerialConsole(s, service_state=lambda: service_state(s.service_name))
    app.extensions["s3_console"] = console
    if s.serial_console:
        console.recover()
        atexit.register(console.close, "Web admin stopped")

    # ---------------------------------------------------------------- helpers
    def defaults_pan_channel():
        try:
            gw = confstore.load(s.conf_path)["values"]["first_GW_data"]
            return gw[1], gw[2]
        except Exception:
            return "1001", "11"

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.context_processor
    def shared_context():
        if session.get("auth") or not s.password_hash:
            return {"pending": applier.pending(),
                    "gateway_id": envfile.read_value(s.env_file, "GATEWAY_ID")}
        return {}

    @app.before_request
    def guard():
        if request.endpoint in (None, "static"):
            return None
        if request.method == "POST":
            token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(token, session.get("csrf", "x")):
                abort(400, "Invalid or missing CSRF token. Reload the page and try again.")
        if request.endpoint == "login" or s.allow_noauth and not s.password_hash:
            return None
        if not session.get("auth"):
            if request.path.startswith("/api/"):
                return jsonify(error="login required"), 401
            return redirect(url_for("login", next=request.path))
        return None

    @app.after_request
    def headers(resp):
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------------ auth
    @app.route("/login", methods=["GET", "POST"])
    def login():
        ip = request.remote_addr or "?"
        rec = fails.get(ip, [0, 0])
        if request.method == "POST":
            if rec[1] > time.time():
                flash(f"Too many attempts. Try again in {int(rec[1] - time.time())}s.", "error")
            elif s.password_hash and check_password_hash(s.password_hash, request.form.get("password", "")):
                fails.pop(ip, None)
                session.clear()
                session["auth"] = True
                session.permanent = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("overview"))
            else:
                rec[0] += 1
                if rec[0] >= MAX_FAILS:
                    rec = [0, time.time() + LOCKOUT_SECONDS]
                fails[ip] = rec
                flash("Wrong password.", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # -------------------------------------------------------------- overview
    @app.route("/")
    def overview():
        rows = csvstore.load(s.csv_path)
        return render_template(
            "overview.html",
            node_count=len(rows),
            service=service_state(s.service_name),
            pending=applier.pending(),
            applied_at=applier.last_applied_at(),
            apply=applier.status(),
        )

    @app.route("/apply", methods=["POST"])
    def apply_changes():
        if not applier.start():
            flash("An apply is already running.", "error")
        else:
            flash("Apply started. The gateway service restarts briefly while the database syncs.", "ok")
        return redirect(url_for("overview"))

    @app.route("/api/apply-status")
    def apply_status():
        return jsonify(applier.status() | {"pending": applier.pending(), "service": service_state(s.service_name)})

    # --------------------------------------------------------------- devices
    @app.route("/devices")
    def devices():
        rows = csvstore.load(s.csv_path)
        pan, ch = defaults_pan_channel()
        return render_template("devices.html", rows=rows, default_pan=pan, default_channel=ch,
                               pending=applier.pending())

    def _save_rows(rows, ok_message):
        try:
            csvstore.save(s.csv_path, rows, backups)
        except csvstore.CsvError as exc:
            flash(str(exc), "error")
            return False
        flash(ok_message + " Press Apply to sync the database and restart the gateway.", "ok")
        return True

    @app.route("/devices/add", methods=["POST"])
    def device_add():
        rows = csvstore.load(s.csv_path)
        try:
            new = csvstore.clean_row(request.form)
            csvstore.check_unique(rows + [new])
        except csvstore.CsvError as exc:
            flash(str(exc), "error")
            return redirect(url_for("devices"))
        _save_rows(rows + [new], f"Added {new['pole_node']} ({new['node']}).")
        return redirect(url_for("devices"))

    @app.route("/devices/bulk", methods=["POST"])
    def device_bulk():
        pan, ch = defaults_pan_channel()
        new, errors = csvstore.parse_bulk(request.form.get("bulk", ""), pan, ch)
        if errors:
            for e in errors[:10]:
                flash(e, "error")
            flash("Nothing was added. Fix the lines above and resubmit.", "error")
            return redirect(url_for("devices"))
        if not new:
            flash("No devices found in the pasted text.", "error")
            return redirect(url_for("devices"))
        rows = csvstore.load(s.csv_path)
        try:
            csvstore.check_unique(rows + new)
        except csvstore.CsvError as exc:
            flash(f"Nothing was added: {exc}", "error")
            return redirect(url_for("devices"))
        _save_rows(rows + new, f"Added {len(new)} devices.")
        return redirect(url_for("devices"))

    @app.route("/devices/edit", methods=["POST"])
    def device_edit():
        original = request.form.get("original_node", "").upper()
        rows = csvstore.load(s.csv_path)
        idx = next((i for i, r in enumerate(rows) if r["node"].upper() == original), None)
        if idx is None:
            flash("Device not found (was it already changed?).", "error")
            return redirect(url_for("devices"))
        try:
            updated = csvstore.clean_row(request.form)
            candidate = rows[:idx] + [updated] + rows[idx + 1:]
            csvstore.check_unique(candidate)
        except csvstore.CsvError as exc:
            flash(str(exc), "error")
            return redirect(url_for("devices"))
        _save_rows(candidate, f"Updated {updated['pole_node']} ({updated['node']}).")
        return redirect(url_for("devices"))

    @app.route("/devices/delete", methods=["POST"])
    def device_delete():
        node = request.form.get("node", "").upper()
        rows = csvstore.load(s.csv_path)
        left = [r for r in rows if r["node"].upper() != node]
        if len(left) == len(rows):
            flash("Device not found.", "error")
        elif _save_rows(left, f"Removed {node}. It will be deleted from the database on Apply."):
            pass
        return redirect(url_for("devices"))

    @app.route("/devices/replace", methods=["POST"])
    def device_replace():
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a CSV file first.", "error")
            return redirect(url_for("devices"))
        try:
            text = f.read().decode("utf-8-sig")
            rows = csvstore.parse_csv_text(text)
        except (UnicodeDecodeError, csvstore.CsvError) as exc:
            flash(f"Upload rejected: {exc}", "error")
            return redirect(url_for("devices"))
        _save_rows(rows, f"Replaced the node list with {len(rows)} devices from the upload.")
        return redirect(url_for("devices"))

    @app.route("/devices/download")
    def device_download():
        if not s.csv_path.exists():
            abort(404)
        return send_file(s.csv_path, mimetype="text/csv", as_attachment=True, download_name="samplelist.csv")

    # -------------------------------------------------------------- settings
    def _settings_context(values=None, errors=None):
        loaded = confstore.load(s.conf_path)
        vals = dict(values if values is not None else loaded["values"])
        nodes = len(csvstore.load(s.csv_path))
        # secrets are never sent back to the browser, only whether one is set
        secret_set = {k: bool(loaded["values"].get(k)) for k in confstore.SECRETS}
        for k in confstore.SECRETS:
            vals[k] = ""
        warnings = [] if errors else [w for w in (confstore.cycle_warning(vals, nodes),
                                                  confstore.local_db_warning(vals)) if w]
        return dict(groups=confstore.GROUPS, fields=confstore.FIELDS, values=vals,
                    errors=errors or {}, readonly=loaded["readonly"], secret_set=secret_set,
                    warnings=warnings, node_count=nodes, pending=applier.pending())

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page():
        if request.method == "GET":
            return render_template("settings.html", **_settings_context())
        form = {}
        for name, field in confstore.FIELDS.items():
            if field.kind == "gw":
                if any(f"{name}__{i}" in request.form for i in range(3)):
                    form[name] = [request.form.get(f"{name}__{i}", "") for i in range(3)]
            elif field.kind == "secret":
                if request.form.get(f"{name}__clear"):
                    form[name] = ""          # remove the password (None)
                elif request.form.get(name, ""):
                    form[name] = request.form[name]
                # blank and not cleared: keep the current value
            elif name in request.form:
                form[name] = request.form[name]
        clean, errors = confstore.validate(form)
        if errors:
            flash("Nothing saved - please fix the highlighted fields.", "error")
            shown = {**confstore.load(s.conf_path)["values"], **form}
            return render_template("settings.html", **_settings_context(shown, errors)), 400
        old = confstore.load(s.conf_path)["values"]
        try:
            changed = confstore.save(s.conf_path, clean, backups)
        except confstore.ConfError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings_page"))
        flash("Settings saved. Press Apply to restart the gateway with them." if changed
              else "No changes to save.", "ok" if changed else "info")
        risky = [k for k in confstore.RISKY if k in clean and clean[k] != old.get(k)]
        if risky:
            flash("Check " + ", ".join(risky) + " carefully before applying: a wrong value can stop the "
                  "gateway from starting or from reaching its database. The previous pygw_conf.py is backed up.", "info")
        return redirect(url_for("settings_page"))

    # ---------------------------------------------------------------- serial
    def _json_body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def _serial_error(exc):
        return jsonify(error=exc.message, code=exc.code), exc.status

    def _console_guard():
        if not s.serial_console:
            raise serialtool.SerialError("The direct console is turned off on this gateway.", 403, "disabled")

    @app.route("/serial")
    def serial_page():
        rows = csvstore.load(s.csv_path)
        try:
            gw = confstore.load(s.conf_path)["values"]
            zc = [{"label": f"Gateway {i}", "cmd": "+ZC" + "".join(gw[k])}
                  for i, k in ((1, "first_GW_data"), (2, "second_GW_data"))]
        except Exception:
            zc = []
        return render_template(
            "serial.html", devices=rows, actions=serialtool.ACTIONS, levels=serialtool.LEVELS,
            console_on=s.serial_console, zc=zc, idle_minutes=s.serial_idle_seconds // 60,
            baud=serialtool.BAUD, pending=applier.pending())

    @app.route("/api/serial/node-command", methods=["POST"])
    def serial_node_command():
        body = _json_body()
        action = str(body.get("action", ""))
        try:
            result = gateway.send(action, body.get("nodes"), body.get("level"))
        except serialtool.SerialError as exc:
            return _serial_error(exc)
        label = "Dim to level " + str(body.get("level")) if action == "dim-level" else serialtool.ACTIONS[action][1]
        status = 200 if not result["failed"] else 502
        return jsonify(**result, label=label, ok=not result["failed"]), status

    @app.route("/api/serial/status")
    def serial_status():
        return jsonify(console.status() | {
            "gateway_reachable": gateway.reachable(),
            "service": service_state(s.service_name),
        })

    @app.route("/api/serial/open", methods=["POST"])
    def serial_open():
        body = _json_body()
        try:
            _console_guard()
            console.open(body.get("port"), bool(body.get("pause")))
        except serialtool.SerialError as exc:
            return _serial_error(exc)
        return jsonify(console.status())

    @app.route("/api/serial/close", methods=["POST"])
    def serial_close():
        err = console.close() if s.serial_console else ""
        return jsonify(console.status() | {"warning": err})

    @app.route("/api/serial/send", methods=["POST"])
    def serial_send():
        try:
            _console_guard()
            console.send(_json_body().get("text"))
        except serialtool.SerialError as exc:
            return _serial_error(exc)
        return jsonify(ok=True)

    @app.route("/api/serial/reset-node", methods=["POST"])
    def serial_reset_node():
        try:
            _console_guard()
            console.reset_node()
        except serialtool.SerialError as exc:
            return _serial_error(exc)
        return jsonify(ok=True)

    @app.route("/api/serial/clear", methods=["POST"])
    def serial_clear():
        console.clear()
        return jsonify(ok=True)

    @app.route("/api/serial/read")
    def serial_read():
        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        return jsonify(console.read(since, active=request.args.get("active") == "1"))

    # ------------------------------------------------------------------ logs
    @app.route("/logs")
    def logs():
        files = logview.list_files(s.log_dir)
        kind = request.args.get("kind", "gateway")
        if kind not in logview.KINDS:
            kind = "gateway"
        return render_template("logs.html", files=files, kind=kind, levels=logview.LEVELS)

    @app.route("/api/logs")
    def api_logs():
        path = logview.resolve(s.log_dir, request.args.get("file", ""))
        if path is None:
            return jsonify(error="log file not found"), 404
        try:
            lines = int(request.args.get("lines", 200))
        except ValueError:
            lines = 200
        data = logview.read(path, lines, request.args.get("q", ""), request.args.get("level", ""))
        return jsonify(data)

    @app.route("/logs/download")
    def log_download():
        path = logview.resolve(s.log_dir, request.args.get("file", ""))
        if path is None:
            abort(404)
        return send_file(path, mimetype="text/plain", as_attachment=True, download_name=path.name)

    return app
