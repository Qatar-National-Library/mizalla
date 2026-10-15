"""Admin blueprint — member, server, settings, and audit management."""

import io
import ipaddress
import os
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from .azure_ai import service_status
from .extensions import db
from .feature_access import sync_member_features
from .jobs import run_all_scheduled_jobs
from .models import (
    FEATURE_KEYS,
    FEATURE_LABELS,
    FEEDBACK_STATUS_ACTIVE,
    FEEDBACK_STATUS_HIDDEN,
    FEEDBACK_TARGET_EXISTING,
    FEEDBACK_TARGET_NEW,
    AppFeatureAccess,
    AppSetting,
    BulkEmailJob,
    GowaDevice,
    GowaDeviceAccess,
    Member,
    ProjectMembership,
    Server,
    ServerAccess,
    ServerCredential,
    ServiceAuditLog,
    UtilityAppFeedback,
)
from .settings import (
    BOOLEAN_SETTING_KEYS,
    DEFAULT_APP_SETTINGS,
    DIRECTORY_SETTING_KEYS,
    INTEGER_SETTING_KEYS,
    LABEL_LIST_SETTING_KEYS,
    RETIRED_SETTING_KEYS,
    get_setting,
    reset_setting,
    seed_default_settings,
    set_setting,
    validate_directory_setting,
    validate_integer_setting,
    validate_label_list,
)
from .azure_ai import FEATURE_REQUIREMENTS, service_status
from .validators import is_valid_email

bp = Blueprint("admin", __name__, url_prefix="/apps/admin")

#: Per-service notes shown next to each access checkbox, so an administrator
#: can see which services are not usable yet without leaving the member form.
FEATURE_NOTES = {
    "bulk_email": "Send templated email to a recipient list.",
    "bulk_whatsapp": "Send templated messages through a registered GOWA device.",
    "bulk_sms": "Send templated SMS through Pingram, one SMS per recipient.",
    "vision": "Analyse an image: description, full response, and mapped fields.",
    "speech": "Turn text into a playable audio clip.",
    "chat": "Upload documents and ask questions about them.",
    "upload_file": "Store PDF, Word, Excel, PowerPoint, text and HTML files in a folder on the server.",
    "video_upload": "Send videos straight to Cloudflare Stream. Needs a Cloudflare API token in .env.",
}


def _feature_cards() -> list[dict]:
    cards = []
    for key in FEATURE_KEYS:
        status = service_status(key)
        cards.append(
            {
                "key": key,
                "label": FEATURE_LABELS[key],
                "note": FEATURE_NOTES.get(key, ""),
                "configured": status["configured"],
                "missing": status["missing"],
            }
        )
    return cards





def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@bp.route("/members", methods=["GET", "POST"])
@admin_required
def member_list():
    devices = GowaDevice.query.order_by(GowaDevice.name).all()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not is_valid_email(email) or len(password) < 12:
            flash("Enter a name, valid email, and password of at least 12 characters.", "error")
        elif Member.query.filter_by(email=email).first():
            flash("That email address is already registered.", "error")
        else:
            requested_features = set(request.form.getlist("feature_ids", type=str))
            member = Member(
                name=name,
                email=email,
                designation=request.form.get("designation", "").strip(),
                department_organization=request.form.get("department_organization", "").strip(),
                is_pm=bool(request.form.get("is_pm")),
                can_bulk_email="bulk_email" in requested_features,
            )
            member.set_password(password)
            db.session.add(member)
            db.session.flush()
            sync_member_features(member, requested_features, granted_by_id=current_user.id)
            if "bulk_whatsapp" in requested_features:
                new_device_ids = set(request.form.getlist("device_ids", type=int))
                for device_id in new_device_ids:
                    db.session.add(
                        GowaDeviceAccess(device_id=device_id, member_id=member.id, granted_by_id=current_user.id)
                    )
            db.session.commit()
            flash("Member account created.", "success")
            return redirect(url_for("admin.member_list"))
    members = Member.query.order_by(Member.name).all()
    # One grouped query for the whole page: N+1 grants lookups would fire on a
    # directory of any size.
    member_features: dict[int, list[str]] = {}
    for member_id, feature_key in db.session.execute(
        db.select(AppFeatureAccess.member_id, AppFeatureAccess.feature_key).order_by(AppFeatureAccess.feature_key)
    ):
        member_features.setdefault(member_id, []).append(feature_key)
    return render_template(
        "members.html",
        members=members,
        member_features=member_features,
        feature_cards=_feature_cards(),
        devices=devices,
    )


def get_accessible_servers_for_user(user):
    """Return all servers accessible to the given user. Admins have access to all servers."""
    if not user or not getattr(user, "is_authenticated", False):
        return []
    if getattr(user, "is_admin", False):
        return Server.query.order_by(Server.name).all()
    project_server_ids = set()
    for m in ProjectMembership.query.filter_by(member_id=user.id).all():
        if m.project and m.project.test_server_id:
            project_server_ids.add(m.project.test_server_id)
        if m.project and m.project.production_server_id:
            project_server_ids.add(m.project.production_server_id)
    cred_server_ids = {c.server_id for c in ServerCredential.query.filter_by(user_id=user.id).all()}
    access_server_ids = {a.server_id for a in ServerAccess.query.filter_by(member_id=user.id).all()}
    all_server_ids = project_server_ids | cred_server_ids | access_server_ids
    if all_server_ids:
        return Server.query.filter(Server.id.in_(all_server_ids)).order_by(Server.name).all()
    return []


@bp.route("/members/<int:member_id>/edit", methods=["GET", "POST"])
@admin_required
def member_edit(member_id):
    member = db.get_or_404(Member, member_id)
    devices = GowaDevice.query.order_by(GowaDevice.name).all()
    servers = get_accessible_servers_for_user(current_user)
    current_device_ids = {a.device_id for a in GowaDeviceAccess.query.filter_by(member_id=member.id).all()}
    current_server_ids = {a.server_id for a in ServerAccess.query.filter_by(member_id=member.id).all()}
    current_feature_ids = {
        row.feature_key
        for row in db.session.scalars(
            db.select(AppFeatureAccess).filter_by(member_id=member.id)
        )
    }

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        duplicate = Member.query.filter(Member.email == email, Member.id != member.id).first()
        password = request.form.get("password", "")
        if not request.form.get("name", "").strip() or not is_valid_email(email):
            flash("Name and valid email are required.", "error")
        elif duplicate:
            flash("That email address is already registered.", "error")
        elif password and len(password) < 12:
            flash("A replacement password must be at least 12 characters.", "error")
        else:
            member.name = request.form["name"].strip()
            member.email = email
            member.designation = request.form.get("designation", "").strip()
            member.department_organization = request.form.get("department_organization", "").strip()
            member.is_pm = bool(request.form.get("is_pm"))
            # Keep the legacy flag in step with the Bulk Emailer grant so the
            # backfill stays idempotent and no access is silently lost.
            requested_features = set(request.form.getlist("feature_ids", type=str))
            member.can_bulk_email = "bulk_email" in requested_features
            sync_member_features(member, requested_features, granted_by_id=current_user.id)
            if password:
                member.set_password(password)

            # Update device access: if Bulk Messenger (bulk_whatsapp) is explicitly unchecked in feature_ids, remove all device access
            features_present = "feature_ids" in request.form or "features_submitted" in request.form
            if features_present and "bulk_whatsapp" not in requested_features:
                new_device_ids = set()
            else:
                new_device_ids = set(request.form.getlist("device_ids", type=int))

            # Revoke access to devices that are no longer selected
            for grant in GowaDeviceAccess.query.filter_by(member_id=member.id).all():
                if grant.device_id not in new_device_ids:
                    db.session.delete(grant)
            # Grant access to newly selected devices
            for device_id in new_device_ids - current_device_ids:
                db.session.add(
                    GowaDeviceAccess(device_id=device_id, member_id=member.id, granted_by_id=current_user.id)
                )

            # Update server access: if Remote Execution (remote_exec) is explicitly unchecked in feature_ids, remove all server access
            if features_present and "remote_exec" not in requested_features:
                new_server_ids = set()
            else:
                new_server_ids = set(request.form.getlist("server_ids", type=int))

            # Revoke access to servers that are no longer selected
            for grant in ServerAccess.query.filter_by(member_id=member.id).all():
                if grant.server_id not in new_server_ids:
                    db.session.delete(grant)
            # Grant access to newly selected servers
            for server_id in new_server_ids - current_server_ids:
                db.session.add(
                    ServerAccess(server_id=server_id, member_id=member.id, granted_by_id=current_user.id)
                )

            db.session.commit()
            flash("Member updated.", "success")
            return redirect(url_for("admin.member_list"))
    return render_template(
        "member_form.html",
        member=member,
        devices=devices,
        servers=servers,
        current_device_ids=current_device_ids,
        current_server_ids=current_server_ids,
        current_feature_ids=current_feature_ids,
        feature_cards=_feature_cards(),
    )



@bp.post("/members/<int:member_id>/delete")
@admin_required
def member_delete(member_id):
    member = db.get_or_404(Member, member_id)
    if member.id == current_user.id:
        flash("You cannot delete your own administrator account.", "error")
    elif member.memberships or member.task_assignments:
        flash("Remove this member from project roles and task ownership before deleting the account.", "error")
    else:
        db.session.delete(member)
        db.session.commit()
        flash("Member deleted.", "success")
    return redirect(url_for("admin.member_list"))


@bp.route("/bulk-email-audit")
@admin_required
def bulk_email_audit():
    """Admin view of all bulk email jobs with audit trail."""
    jobs = BulkEmailJob.query.order_by(BulkEmailJob.created_at.desc()).all()
    return render_template("bulk_email_admin.html", jobs=jobs)


def _start_connection_cycle(app, server_id: int, username: str, password: str, max_retries: int = 5):
    import threading

    thread = threading.Thread(
        target=_run_connection_cycle,
        args=(app, server_id, username, password, max_retries),
        daemon=True,
    )
    thread.start()


def _run_connection_cycle(app, server_id: int, username: str, password: str, max_retries: int = 5):
    import asyncio
    import time
    from datetime import datetime, timezone
    from .asyncssh_engine import test_ssh_connection

    with app.app_context():
        server = db.session.get(Server, server_id)
        if not server:
            return
        ip = server.ip
        port = server.ssh_port or 22

    last_error = "Connection timed out"
    for attempt in range(1, max_retries + 1):
        with app.app_context():
            server = db.session.get(Server, server_id)
            if not server:
                return
            server.agent_status = "connecting"
            server.connection_attempts = attempt
            server.max_connection_attempts = max_retries
            server.connection_message = f"Initiating connection cycle ({attempt} of {max_retries} retries)..."
            db.session.commit()

        try:
            coro_or_res = test_ssh_connection(
                ip=ip,
                port=port,
                username=username,
                password=password,
            )
            if asyncio.iscoroutine(coro_or_res):
                success, msg, telemetry = asyncio.run(coro_or_res)
            else:
                success, msg, telemetry = coro_or_res
        except Exception as exc:
            success = False
            msg = str(exc)
            telemetry = {}

        if success:
            with app.app_context():
                server = db.session.get(Server, server_id)
                if server:
                    server.agent_status = "online"
                    server.connection_attempts = 0
                    server.connection_message = "Connected successfully."
                    if telemetry.get("os_version"):
                        server.operating_system = telemetry["os_version"]
                    elif telemetry.get("os_type"):
                        server.operating_system = telemetry["os_type"].title()
                    if telemetry.get("packages_summary"):
                        server.technology_packages = telemetry["packages_summary"]
                    if telemetry.get("os_type"):
                        server.os_type = telemetry["os_type"]
                    if telemetry.get("os_version"):
                        server.os_version = telemetry["os_version"]

                    # Hardware & Runtime Telemetry
                    if "ram_total" in telemetry:
                        server.ram_total = telemetry.get("ram_total") or ""
                    if "ram_used" in telemetry:
                        server.ram_used = telemetry.get("ram_used") or ""
                    if "ram_percent" in telemetry:
                        try:
                            server.ram_percent = float(telemetry.get("ram_percent") or 0.0)
                        except (ValueError, TypeError):
                            server.ram_percent = 0.0
                    if "disk_total" in telemetry:
                        server.disk_total = telemetry.get("disk_total") or ""
                    if "disk_used" in telemetry:
                        server.disk_used = telemetry.get("disk_used") or ""
                    if "disk_percent" in telemetry:
                        try:
                            server.disk_percent = float(telemetry.get("disk_percent") or 0.0)
                        except (ValueError, TypeError):
                            server.disk_percent = 0.0
                    if "core_tags" in telemetry:
                        server.core_tags = telemetry.get("core_tags") or ""

                    now = datetime.now(timezone.utc)
                    server.last_checked = now
                    server.agent_last_seen = now
                    server.status = "available"
                    db.session.commit()
            return
        else:
            last_error = msg
            if attempt < max_retries:
                time.sleep(1.5)

    with app.app_context():
        server = db.session.get(Server, server_id)
        if server:
            server.agent_status = "failed"
            server.connection_attempts = max_retries
            server.connection_message = f"Connection failed after {max_retries} retries: {last_error}"
            db.session.commit()


@bp.route("/servers", methods=["GET", "POST"])
@admin_required
def server_list():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        ip = request.form.get("ip", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        operating_system = request.form.get("operating_system", "").strip() or "Detecting..."
        technology_packages = request.form.get("technology_packages", "").strip()
        try:
            ipaddress.ip_address(ip)
            valid_ip = True
        except ValueError:
            valid_ip = False
        if not name or not valid_ip:
            flash("Server name and valid IP address are required.", "error")
        else:
            server = Server(
                name=name,
                ip=ip,
                operating_system=operating_system,
                technology_packages=technology_packages,
                agent_status="connecting" if username else "offline",
                connection_attempts=1 if username else 0,
                max_connection_attempts=5,
                connection_message="Initiating connection cycle (1 of 5 retries)..." if username else "",
            )
            db.session.add(server)
            db.session.flush()

            if username:
                cred = ServerCredential(
                    server_id=server.id,
                    user_id=None,
                    is_default=True,
                    username=username,
                    status="available",
                )
                if password:
                    cred.password = password
                db.session.add(cred)

            db.session.commit()

            if username:
                _start_connection_cycle(current_app._get_current_object(), server.id, username, password)

            flash("Server created.", "success")
            return redirect(url_for("admin.server_list"))
    return render_template("servers.html", servers=Server.query.order_by(Server.name).all())


@bp.route("/servers/<int:server_id>/edit", methods=["GET", "POST"])
@admin_required
def server_edit(server_id):
    server = db.get_or_404(Server, server_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        ip = request.form.get("ip", "").strip()
        operating_system = request.form.get("operating_system", "").strip() or server.operating_system
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            ipaddress.ip_address(ip)
            valid_ip = True
        except ValueError:
            valid_ip = False
        if not name or not valid_ip:
            flash("Server name and valid IP address are required.", "error")
        else:
            server.name = name
            server.ip = ip
            server.operating_system = operating_system
            if "technology_packages" in request.form:
                server.technology_packages = request.form.get("technology_packages", "").strip()

            if username:
                cred = server.default_credential
                if not cred:
                    cred = ServerCredential(
                        server_id=server.id,
                        user_id=None,
                        is_default=True,
                        username=username,
                        status="available",
                    )
                    db.session.add(cred)
                cred.username = username
                if password:
                    cred.password = password

            db.session.commit()

            active_username = username or (server.default_credential.username if server.default_credential else "")
            active_password = password or (server.default_credential.password if server.default_credential else "")
            if active_username:
                server.agent_status = "connecting"
                server.connection_attempts = 1
                server.max_connection_attempts = 5
                server.connection_message = "Initiating connection cycle (1 of 5 retries)..."
                db.session.commit()
                _start_connection_cycle(current_app._get_current_object(), server.id, active_username, active_password or "")

            flash("Server updated.", "success")
            return redirect(url_for("admin.server_list"))
    return render_template("server_form.html", server=server)


@bp.route("/servers/<int:server_id>/reconnect", methods=["POST"])
@admin_required
def server_reconnect(server_id):
    server = db.get_or_404(Server, server_id)

    if request.is_json:
        data = request.get_json() or {}
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
    else:
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

    cred = server.default_credential
    if not cred:
        cred = ServerCredential(
            server_id=server.id,
            user_id=None,
            is_default=True,
            username=username or "root",
            status="available",
        )
        db.session.add(cred)

    if username:
        cred.username = username
    if password is not None and password != "":
        cred.password = password

    server.agent_status = "connecting"
    server.connection_attempts = 1
    server.max_connection_attempts = 5
    server.connection_message = "Initiating connection cycle (1 of 5 retries)..."
    db.session.commit()

    active_username = cred.username
    active_password = cred.password

    _start_connection_cycle(current_app._get_current_object(), server.id, active_username, active_password, max_retries=5)

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.headers.get("Accept") == "application/json":
        return jsonify({
            "ok": True,
            "agent_status": "connecting",
            "connection_attempts": 1,
            "max_connection_attempts": 5,
            "connection_message": "Initiating connection cycle (1 of 5 retries)...",
        })

    flash("Reconnection cycle initiated (up to 5 retries).", "success")
    return redirect(url_for("admin.server_list"))


@bp.route("/servers/<int:server_id>/connection-status")
@admin_required
def server_connection_status(server_id):
    server = db.get_or_404(Server, server_id)
    return jsonify({
        "id": server.id,
        "name": server.name,
        "ip": server.ip,
        "operating_system": server.operating_system,
        "technology_packages": server.technology_packages,
        "agent_status": server.agent_status,
        "is_online": server.agent_status == "online",
        "connection_attempts": server.connection_attempts,
        "max_connection_attempts": server.max_connection_attempts or 5,
        "connection_message": server.connection_message or "",
        "ram_total": server.ram_total or "",
        "ram_used": server.ram_used or "",
        "ram_percent": server.ram_percent or 0.0,
        "disk_total": server.disk_total or "",
        "disk_used": server.disk_used or "",
        "disk_percent": server.disk_percent or 0.0,
        "core_tags": server.core_tags or "",
        "tag_lines": server.get_tag_lines(),
        "has_telemetry": server.has_telemetry,
    })


@bp.post("/servers/<int:server_id>/delete")
@admin_required
def server_delete(server_id):
    server = db.get_or_404(Server, server_id)
    if server.test_projects or server.production_projects:
        flash("Unassign this server from projects before deleting it.", "error")
    else:
        db.session.delete(server)
        db.session.commit()
        flash("Server deleted.", "success")
    return redirect(request.referrer or url_for("admin.server_list"))


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings_list():
    seed_default_settings()
    if request.method == "POST":
        reset_key = request.form.get("reset", "").strip()
        if reset_key:
            if reset_setting(reset_key):
                flash(f"'{reset_key}' restored to its default value.", "success")
            else:
                flash(f"'{reset_key}' is not a known setting and was not changed.", "error")
            return redirect(url_for("admin.settings_list"))

        validated = {}
        errors = []
        for key in LABEL_LIST_SETTING_KEYS:
            if key in request.form:
                try:
                    validated[key] = ", ".join(validate_label_list(request.form.get(key, ""), key))
                except ValueError as error:
                    errors.append(f"{key}: {error}")
        for key in DIRECTORY_SETTING_KEYS:
            if key in request.form:
                try:
                    validated[key] = validate_directory_setting(request.form.get(key, ""), key)
                except ValueError as error:
                    errors.append(f"{key}: {error}")
        for key in INTEGER_SETTING_KEYS:
            if key in request.form:
                try:
                    validated[key] = validate_integer_setting(request.form.get(key, ""), key)
                except ValueError as error:
                    errors.append(f"{key}: {error}")
        if errors:
            for message in errors:
                flash(message, "error")
            return redirect(url_for("admin.settings_list"))

        for setting in AppSetting.query.all():
            if setting.key in request.form:
                set_setting(setting.key, validated.get(setting.key, request.form.get(setting.key, "").strip()), setting.description)
        flash("Settings saved successfully.", "success")
        return redirect(url_for("admin.settings_list"))

    settings = AppSetting.query.filter(AppSetting.key.notin_(RETIRED_SETTING_KEYS)).order_by(AppSetting.key).all()
    secret_key_configured = bool(os.environ.get("SECRET_KEY"))
    sendgrid_key_configured = bool(os.environ.get("SENDGRID_API_KEY"))

    # Credentials are never editable here - they live in .env - so the screen
    # reports each service as ready or not. service_status reads the
    # environment, which needs no database round-trip.
    service_states = {feature: service_status(feature) for feature in FEATURE_KEYS}

    return render_template(
        "settings.html",
        settings=settings,
        defaults={key: value for key, (value, _description) in DEFAULT_APP_SETTINGS.items()},
        in_effect={setting.key: get_setting(setting.key) for setting in settings},
        label_list_keys=LABEL_LIST_SETTING_KEYS,
        boolean_keys=BOOLEAN_SETTING_KEYS,
        directory_keys=DIRECTORY_SETTING_KEYS,
        integer_keys=tuple(INTEGER_SETTING_KEYS),
        # What blank means for each directory setting, so an administrator can see
        # where files go today without having to know the application layout.
        directory_placeholders={
            "upload_file_folder": str(Path(current_app.config["UPLOAD_FOLDER"])),
        },
        integer_bounds={key: bounds for key, bounds in INTEGER_SETTING_KEYS.items()},
        secret_key_configured=secret_key_configured,
        sendgrid_key_configured=sendgrid_key_configured,
        service_states=service_states,
        feature_labels=FEATURE_LABELS,
    )


@bp.post("/settings/run-jobs")
@admin_required
def run_jobs_now():
    results = run_all_scheduled_jobs()
    total_sent = sum(results.values())
    flash(f"Scheduled jobs ran successfully. Sent {total_sent} notification emails ({results}).", "success")
    return redirect(url_for("admin.settings_list"))


# ─── Bulk Email Audit ───────────────────────────────────────────────────────

@bp.get("/bulk-email-audit/export")
@admin_required
def bulk_email_audit_export():
    """Export all bulk email jobs to Excel or CSV with UTF-8 support."""
    import csv
    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Font

    export_format = request.args.get("format", "xlsx").lower()
    jobs = BulkEmailJob.query.order_by(BulkEmailJob.created_at.desc()).all()

    if export_format == "csv":
        # csv.writer emits str, so the buffer is text and encoded on the way
        # out. Writing str into a BytesIO raises TypeError rather than
        # producing a file.
        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow(["Job ID", "Subject", "From", "Created By", "Created At", "Total", "Sent", "Errors", "Duration (s)", "Summary"])
        for job in jobs:
            writer.writerow([
                job.id, job.subject, job.sender, job.created_by_name,
                job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else "",
                job.total, job.sent, job.errors, job.duration_seconds or "", job.summary,
            ])
            for r in job.recipients:
                writer.writerow(["", "", "", "", "", "", "", "", "", f"  → {r.to_email}: {r.status} — {r.reason}"])
        # utf-8-sig so Excel opens non-Latin text without mangling it.
        output = io.BytesIO(text.getvalue().encode("utf-8-sig"))
        output.seek(0)
        return send_file(output, mimetype="text/csv", as_attachment=True, download_name="bulk_email_audit.csv")

    # Default: Excel
    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "Bulk Email Audit"
    ws.append(["Job ID", "Subject", "From", "Created By", "Created At", "Total", "Sent", "Errors", "Duration (s)", "Summary"])
    for job in jobs:
        ws.append([
            job.id, job.subject, job.sender, job.created_by_name,
            job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else "",
            job.total, job.sent, job.errors, job.duration_seconds or "", job.summary,
        ])
        for r in job.recipients:
            ws.append(["", "", "", "", "", "", "", "", "", f"  → {r.to_email}: {r.status} — {r.reason}"])

    for row in ws.iter_rows(min_row=1, max_row=1):
        for cell in row:
            if cell.value:
                cell.font = Font(bold=True)

    # openpyxl writes nothing until asked. Without this the .xlsx is valid HTTP
    # but empty: 200, right Content-Type, zero bytes.
    wb.save(output)
    output.seek(0)
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name="bulk_email_audit.xlsx")


# ─── WhatsApp Audit ─────────────────────────────────────────────────────────

@bp.route("/whatsapp-audit")
@admin_required
def whatsapp_audit():
    """Admin view of all bulk WhatsApp jobs with audit trail."""
    from .models import WhatsAppJob
    jobs = WhatsAppJob.query.order_by(WhatsAppJob.created_at.desc()).all()
    return render_template("whatsapp_audit.html", jobs=jobs)


@bp.get("/whatsapp-audit/export")
@admin_required
def whatsapp_audit_export():
    """Export all bulk WhatsApp jobs to Excel or CSV with UTF-8 support."""
    import csv
    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from .models import WhatsAppJob

    export_format = request.args.get("format", "xlsx").lower()
    jobs = WhatsAppJob.query.order_by(WhatsAppJob.created_at.desc()).all()

    if export_format == "csv":
        # csv.writer emits str, so the buffer is text and encoded on the way
        # out. Writing str into a BytesIO raises TypeError rather than
        # producing a file.
        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow(["Job ID", "Device", "Created By", "Created At", "Total", "Sent", "Errors", "Duration (s)", "Summary"])
        for job in jobs:
            writer.writerow([
                job.id, job.device_name, job.created_by_name,
                job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else "",
                job.total, job.sent, job.errors, job.duration_seconds or "", job.summary,
            ])
            for r in job.recipients:
                writer.writerow(["", "", "", "", "", "", "", "", f"  → {r.phone}: {r.status} — {r.reason}"])
        # utf-8-sig so Excel opens non-Latin text without mangling it.
        output = io.BytesIO(text.getvalue().encode("utf-8-sig"))
        output.seek(0)
        return send_file(output, mimetype="text/csv", as_attachment=True, download_name="whatsapp_audit.csv")

    # Default: Excel
    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "WhatsApp Audit"
    ws.append(["Job ID", "Device", "Created By", "Created At", "Total", "Sent", "Errors", "Duration (s)", "Summary"])
    for job in jobs:
        ws.append([
            job.id, job.device_name, job.created_by_name,
            job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else "",
            job.total, job.sent, job.errors, job.duration_seconds or "", job.summary,
        ])
        for r in job.recipients:
            ws.append(["", "", "", "", "", "", "", "", f"  → {r.phone}: {r.status} — {r.reason}"])

    for row in ws.iter_rows(min_row=1, max_row=1):
        for cell in row:
            if cell.value:
                cell.font = Font(bold=True)

    # openpyxl writes nothing until asked. Without this the .xlsx is valid HTTP
    # but empty: 200, right Content-Type, zero bytes.
    wb.save(output)
    output.seek(0)
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name="whatsapp_audit.xlsx")


# ─── SMS Audit ───────────────────────────────────────────────────────────────

@bp.route("/sms-audit")
@admin_required
def sms_audit():
    """Admin view of all bulk SMS jobs with audit trail."""
    from .models import SmsJob
    jobs = SmsJob.query.order_by(SmsJob.created_at.desc()).all()
    return render_template("sms_audit.html", jobs=jobs)


@bp.get("/sms-audit/export")
@admin_required
def sms_audit_export():
    """Export all bulk SMS jobs to Excel or CSV with UTF-8 support."""
    import csv
    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from .models import SmsJob

    export_format = request.args.get("format", "xlsx").lower()
    jobs = SmsJob.query.order_by(SmsJob.created_at.desc()).all()

    if export_format == "csv":
        # csv.writer emits str, so the buffer is text and encoded on the way
        # out. Writing str into a BytesIO (as the sibling exports do) raises
        # TypeError rather than producing a file.
        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow(["Job ID", "Sender", "Region", "Created By", "Created At", "Total", "Sent", "Errors", "Duration (s)", "Summary"])
        for job in jobs:
            writer.writerow([
                job.id, job.sender, job.region, job.created_by_name,
                job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else "",
                job.total, job.sent, job.errors, job.duration_seconds or "", job.summary,
            ])
            for r in job.recipients:
                writer.writerow(["", "", "", "", "", "", "", "", "", f"  → {r.phone}: {r.status} — {r.reason}"])
        # utf-8-sig so Excel opens Arabic text without mangling it.
        output = io.BytesIO(text.getvalue().encode("utf-8-sig"))
        output.seek(0)
        return send_file(output, mimetype="text/csv", as_attachment=True, download_name="sms_audit.csv")

    # Default: Excel
    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "SMS Audit"
    ws.append(["Job ID", "Sender", "Region", "Created By", "Created At", "Total", "Sent", "Errors", "Duration (s)", "Summary"])
    for job in jobs:
        ws.append([
            job.id, job.sender, job.region, job.created_by_name,
            job.created_at.strftime("%Y-%m-%d %H:%M:%S") if job.created_at else "",
            job.total, job.sent, job.errors, job.duration_seconds or "", job.summary,
        ])
        for r in job.recipients:
            ws.append(["", "", "", "", "", "", "", "", "", f"  → {r.phone}: {r.status} — {r.reason}"])

    for row in ws.iter_rows(min_row=1, max_row=1):
        for cell in row:
            if cell.value:
                cell.font = Font(bold=True)

    wb.save(output)
    output.seek(0)
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name="sms_audit.xlsx")


# ─── Service Audit Trail ───────────────────────────────────────────────────

@bp.route("/service-audit")
@admin_required
def service_audit():
    """Admin view of all service audit logs (who, when, request, response)."""
    service_filter = request.args.get("service", "").strip()
    status_filter = request.args.get("status", "").strip()
    start_date_str = request.args.get("start_date", "").strip()
    end_date_str = request.args.get("end_date", "").strip()

    query = ServiceAuditLog.query.order_by(ServiceAuditLog.created_at.desc())
    if service_filter:
        query = query.filter_by(service_key=service_filter)
    if status_filter:
        query = query.filter_by(status=status_filter)

    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            query = query.filter(ServiceAuditLog.created_at >= start_date)
        except ValueError:
            start_date_str = ""

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(ServiceAuditLog.created_at <= end_date)
        except ValueError:
            end_date_str = ""

    logs = query.limit(500).all()
    return render_template(
        "service_audit_admin.html",
        logs=logs,
        service_filter=service_filter,
        status_filter=status_filter,
        start_date=start_date_str,
        end_date=end_date_str,
        feature_labels=FEATURE_LABELS,
    )


@bp.get("/service-audit/export")
@admin_required
def service_audit_export():
    """Export all service audit log entries to Excel or CSV."""
    import csv
    from openpyxl import Workbook
    from openpyxl.styles import Font

    export_format = request.args.get("format", "xlsx").lower()
    service_filter = request.args.get("service", "").strip()
    status_filter = request.args.get("status", "").strip()
    start_date_str = request.args.get("start_date", "").strip()
    end_date_str = request.args.get("end_date", "").strip()

    query = ServiceAuditLog.query.order_by(ServiceAuditLog.created_at.desc())
    if service_filter:
        query = query.filter_by(service_key=service_filter)
    if status_filter:
        query = query.filter_by(status=status_filter)

    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            query = query.filter(ServiceAuditLog.created_at >= start_date)
        except ValueError:
            pass

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59, microsecond=999999)
            query = query.filter(ServiceAuditLog.created_at <= end_date)
        except ValueError:
            pass

    logs = query.all()

    if export_format == "csv":
        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow(["ID", "Timestamp (UTC)", "Service", "Action", "Actor Name", "Actor Email", "Status", "Duration (ms)", "Request Values", "Response Values"])
        for log in logs:
            writer.writerow([
                log.id,
                log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else "",
                FEATURE_LABELS.get(log.service_key, log.service_name or log.service_key),
                log.action,
                log.actor_name,
                log.actor_email,
                log.status,
                log.duration_ms or 0,
                log.request_data or "",
                log.response_data or "",
            ])
        output = io.BytesIO(text.getvalue().encode("utf-8-sig"))
        output.seek(0)
        return send_file(output, mimetype="text/csv", as_attachment=True, download_name="service_audit_trail.csv")

    # Excel format
    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "Service Audit Trail"
    ws.append(["ID", "Timestamp (UTC)", "Service", "Action", "Actor Name", "Actor Email", "Status", "Duration (ms)", "Request Values", "Response Values"])
    for log in logs:
        ws.append([
            log.id,
            log.created_at.strftime("%Y-%m-%d %H:%M:%S") if log.created_at else "",
            FEATURE_LABELS.get(log.service_key, log.service_name or log.service_key),
            log.action,
            log.actor_name,
            log.actor_email,
            log.status,
            log.duration_ms or 0,
            log.request_data or "",
            log.response_data or "",
        ])

    for row in ws.iter_rows(min_row=1, max_row=1):
        for cell in row:
            if cell.value:
                cell.font = Font(bold=True)

    wb.save(output)
    output.seek(0)
    return send_file(output, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name="service_audit_trail.xlsx")


# ─── Utility Application Feedback & Suggestions Moderation ─────────────────

@bp.route("/feedback")
@admin_required
def feedback_dashboard():
    """Admin dashboard for viewing, monitoring, and moderating utility app feedback."""
    app_filter = request.args.get("app", "").strip()
    target_filter = request.args.get("target", "").strip()
    status_filter = request.args.get("status", "").strip()

    query = UtilityAppFeedback.query.order_by(UtilityAppFeedback.created_at.desc())
    if app_filter:
        query = query.filter_by(feature_key=app_filter)
    if target_filter:
        query = query.filter_by(target_type=target_filter)
    if status_filter:
        query = query.filter_by(status=status_filter)

    feedbacks = query.all()

    # Calculate overall metrics
    all_existing = UtilityAppFeedback.query.filter_by(target_type=FEEDBACK_TARGET_EXISTING).all()
    overall_existing_count = len(all_existing)
    rated = [fb.rating for fb in all_existing if fb.rating is not None]
    overall_avg_display = round(sum(rated) / len(rated), 1) if rated else None

    overall_new_count = UtilityAppFeedback.query.filter_by(target_type=FEEDBACK_TARGET_NEW).count()

    # Stats per feature key
    stats_by_key = {}
    for key in FEATURE_KEYS:
        app_feedbacks = [fb for fb in all_existing if fb.feature_key == key]
        ratings = [fb.rating for fb in app_feedbacks if fb.rating is not None]
        avg = round(sum(ratings) / len(ratings), 1) if ratings else None
        stats_by_key[key] = {
            "avg_rating": avg,
            "total_count": len(app_feedbacks),
        }

    return render_template(
        "feedback_admin.html",
        feedbacks=feedbacks,
        overall_avg_display=overall_avg_display,
        overall_existing_count=overall_existing_count,
        overall_new_count=overall_new_count,
        feature_keys=FEATURE_KEYS,
        stats_by_key=stats_by_key,
        app_filter=app_filter,
        feature_labels=FEATURE_LABELS,
        target_filter=target_filter,
        status_filter=status_filter,
    )


@bp.post("/feedback/<int:feedback_id>/status")
@admin_required
def feedback_toggle_status(feedback_id: int):
    """Toggle visibility status of a feedback entry between active and hidden."""
    feedback = db.session.get(UtilityAppFeedback, feedback_id)
    if not feedback:
        return jsonify(error="Feedback not found"), 404

    new_status = request.form.get("status", "").strip().lower()
    if new_status not in (FEEDBACK_STATUS_ACTIVE, FEEDBACK_STATUS_HIDDEN):
        return jsonify(error="Invalid status"), 400

    feedback.status = new_status
    feedback.admin_reviewed_by_id = current_user.id
    feedback.admin_reviewed_at = datetime.now(timezone.utc)
    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify(success=True, status=feedback.status)

    flash("Feedback visibility updated.", "success")
    return redirect(url_for("admin.feedback_dashboard"))


@bp.post("/feedback/<int:feedback_id>/notes")
@admin_required
def feedback_save_notes(feedback_id: int):
    """Record internal admin notes/comments against user feedback."""
    feedback = db.session.get(UtilityAppFeedback, feedback_id)
    if not feedback:
        return jsonify(error="Feedback not found"), 404

    admin_notes = request.form.get("admin_notes", "").strip()
    feedback.admin_notes = admin_notes
    feedback.admin_reviewed_by_id = current_user.id
    feedback.admin_reviewed_at = datetime.now(timezone.utc)
    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
        return jsonify(
            success=True,
            admin_notes=feedback.admin_notes,
            reviewer_name=current_user.name,
            reviewed_at=feedback.admin_reviewed_at.strftime("%Y-%m-%d %H:%M") if feedback.admin_reviewed_at else "",
        )

    flash("Admin notes saved.", "success")
    return redirect(url_for("admin.feedback_dashboard"))


@bp.get("/feedback/export")
@admin_required
def feedback_export():
    """Export feedback submissions to Excel (.xlsx) or CSV."""
    import csv
    from openpyxl import Workbook
    from openpyxl.styles import Font

    export_format = request.args.get("format", "xlsx").lower()
    app_filter = request.args.get("app", "").strip()
    target_filter = request.args.get("target", "").strip()
    status_filter = request.args.get("status", "").strip()

    query = UtilityAppFeedback.query.order_by(UtilityAppFeedback.created_at.desc())
    if app_filter:
        query = query.filter_by(feature_key=app_filter)
    if target_filter:
        query = query.filter_by(target_type=target_filter)
    if status_filter:
        query = query.filter_by(status=status_filter)

    feedbacks = query.all()

    if export_format == "csv":
        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow([
            "ID", "Submitted (UTC)", "Category", "Utility App", "Rating (1-10)",
            "User Comments", "Member Name", "Member Email", "Status",
            "Admin Notes", "Reviewed By", "Reviewed At"
        ])
        for fb in feedbacks:
            writer.writerow([
                fb.id,
                fb.created_at.strftime("%Y-%m-%d %H:%M") if fb.created_at else "",
                "New Proposal" if fb.target_type == FEEDBACK_TARGET_NEW else "Existing Review",
                FEATURE_LABELS.get(fb.feature_key, fb.app_title),
                fb.rating if fb.rating is not None else "",
                fb.comments or "",
                fb.member_name or (fb.member.name if fb.member else ""),
                fb.member_email or (fb.member.email if fb.member else ""),
                fb.status,
                fb.admin_notes or "",
                fb.admin_reviewer.name if fb.admin_reviewer else "",
                fb.admin_reviewed_at.strftime("%Y-%m-%d %H:%M") if fb.admin_reviewed_at else "",
            ])
        output = io.BytesIO(text.getvalue().encode("utf-8-sig"))
        output.seek(0)
        return send_file(output, mimetype="text/csv", as_attachment=True, download_name="utility_app_feedback.csv")

    # Excel format
    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "Utility App Feedback"
    ws.append([
        "ID", "Submitted (UTC)", "Category", "Utility App", "Rating (1-10)",
        "User Comments", "Member Name", "Member Email", "Status",
        "Admin Notes", "Reviewed By", "Reviewed At"
    ])
    for fb in feedbacks:
        ws.append([
            fb.id,
            fb.created_at.strftime("%Y-%m-%d %H:%M") if fb.created_at else "",
            "New Proposal" if fb.target_type == FEEDBACK_TARGET_NEW else "Existing Review",
            FEATURE_LABELS.get(fb.feature_key, fb.app_title),
            fb.rating if fb.rating is not None else "",
            fb.comments or "",
            fb.member_name or (fb.member.name if fb.member else ""),
            fb.member_email or (fb.member.email if fb.member else ""),
            fb.status,
            fb.admin_notes or "",
            fb.admin_reviewer.name if fb.admin_reviewer else "",
            fb.admin_reviewed_at.strftime("%Y-%m-%d %H:%M") if fb.admin_reviewed_at else "",
        ])

    for row in ws.iter_rows(min_row=1, max_row=1):
        for cell in row:
            if cell.value:
                cell.font = Font(bold=True)

    wb.save(output)
    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="utility_app_feedback.xlsx",
    )

