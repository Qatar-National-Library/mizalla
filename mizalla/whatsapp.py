import base64
import io
import json
import logging
import random
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Font
from werkzeug.utils import secure_filename

from .service_audit import record_service_audit
from .extensions import db
from .feature_access import has_feature_access
from .gowa import (
    call_mcp,
    check_health,
    get_connection_status,
    get_gowa_devices,
    send_text_message,
    send_media_message,
)
from .models import (
    FEATURE_BULK_WHATSAPP,
    GowaDevice,
    GowaDeviceAccess,
    Member,
    WhatsAppJob,
    WhatsAppRecipient,
)
from .recipient_list import PHONE_LIST, validate_upload
from .recipient_list import read_recipients as _read_recipients
from .shared import substitute_attachment, substitute_template

logger = logging.getLogger("mizalla.whatsapp")

bp = Blueprint("whatsapp", __name__, url_prefix="/apps/whatsapp")

ALLOWED_ATTACHMENTS = {".pdf", ".docx", ".xlsx", ".pptx", ".htm", ".html", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mkv", ".avi", ".mp3", ".wav", ".ogg", ".m4a"}
PHONE_PATTERN = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
jobs = {}
jobs_lock = threading.Lock()


def read_recipients(file_storage):
    """Read a CSV phone list and return (recipients, rows) for substitution.

    Thin wrapper over :func:`recipient_list.read_recipients` so the required
    column is named once here, and so this reader cannot drift away from the
    email one.
    """
    return _read_recipients(file_storage, "PHONE")


def _add_recipient_result(job_id, phone, status, reason=None):
    with jobs_lock:
        job = jobs[job_id]
        db.session.add(WhatsAppRecipient(job_id=job_id, phone=phone, status=status, reason=reason or ""))
        db.session.commit()
        job["results"].append({"to": phone, "status": status, "reason": reason or ""})
        if status == "SENT":
            job["sent"] += 1
        else:
            job["errors"] += 1
        if job["sent"] + job["errors"] >= job["total"]:
            job["complete"] = True
            job["completed_at"] = datetime.now(timezone.utc)


def process_job(job_id, recipients, message, attachments, device_device_id, created_by_name, rows_data, lock_pdf=None, pdf_alignment=None):
    """Process a bulk WhatsApp job — send messages with template substitution."""
    from .settings import get_setting
    job = jobs[job_id]
    job["created_by_name"] = created_by_name

    if current_app.config.get("TESTING"):
        base_delay, jitter, batch_size, batch_delay = 0, 0, 100, 120
    else:
        try:
            base_delay = int(get_setting("whatsapp_throttle_base_delay", "5"))
            jitter = int(get_setting("whatsapp_throttle_jitter", "5"))
            batch_size = int(get_setting("whatsapp_batch_size", "100"))
            batch_delay = int(get_setting("whatsapp_batch_delay", "120"))
        except ValueError:
            base_delay, jitter, batch_size, batch_delay = 5, 5, 100, 120

    for idx, phone in enumerate(recipients):
        if idx > 0:
            if batch_size > 0 and idx % batch_size == 0:
                time.sleep(batch_delay)
            else:
                sleep_time = base_delay + random.uniform(0, jitter)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        if not PHONE_PATTERN.match(phone):
            _add_recipient_result(job_id, phone, "ERROR", "Invalid phone number.")
            continue

        row_data = rows_data[idx] if idx < len(rows_data) else {}
        customized_message = substitute_template(message, row_data)

        try:
            # Send text message
            result = send_text_message(phone, customized_message, device_id=device_device_id)
            if "error" in result:
                _add_recipient_result(job_id, phone, "ERROR", result["error"])
                continue

            # Send attachments as media messages
            attachment_errors = []
            for att in attachments:
                if len(att) == 3:
                    filename, content, should_convert = att
                else:
                    filename, content = att
                    should_convert = False

                customized_content = substitute_attachment(filename, content, row_data, convert_pdf=should_convert, lock_pdf=lock_pdf, pdf_alignment=pdf_alignment)
                if should_convert and not filename.lower().endswith(".pdf"):
                    filename = Path(filename).stem + ".pdf"

                # Save to uploads and get URL
                upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
                upload_folder.mkdir(parents=True, exist_ok=True)
                temp_filename = f"wa_{str(job_id)[-12:]}_{secrets.token_hex(8)}_{secure_filename(filename)}"
                temp_path = upload_folder / temp_filename
                temp_path.write_bytes(customized_content)

                base_url = current_app.config.get("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
                file_url = f"{base_url}/apps/whatsapp/uploads/{temp_filename}"

                suffix = Path(filename).suffix.lower()
                if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    media_type = "image"
                elif suffix in {".mp4", ".mkv", ".avi"}:
                    media_type = "video"
                elif suffix in {".mp3", ".wav", ".ogg", ".m4a"}:
                    media_type = "audio"
                else:
                    media_type = "document"

                media_result = send_media_message(phone, media_type, file_url, caption=customized_message[:100], device_id=device_device_id)
                if "error" in media_result:
                    attachment_errors.append(f"{filename}: {media_result['error']}")

                # Clean up temp file
                try:
                    temp_path.unlink()
                except Exception:
                    pass

            if attachment_errors:
                _add_recipient_result(job_id, phone, "ERROR", "; ".join(attachment_errors))
            else:
                _add_recipient_result(job_id, phone, "SENT")

        except Exception as exc:
            _add_recipient_result(job_id, phone, "ERROR", str(exc) or exc.__class__.__name__)

    with jobs_lock:
        job["complete"] = True
        job["completed_at"] = datetime.now(timezone.utc)
        job["duration_seconds"] = int((job["completed_at"] - job["created_at"]).total_seconds())
        db_job = db.session.get(WhatsAppJob, job_id)
        if db_job:
            db_job.completed_at = job["completed_at"]
            db_job.duration_seconds = job["duration_seconds"]
            db_job.sent = job["sent"]
            db_job.errors = job["errors"]
            db_job.summary = f"{job['total']} recipients: {job['sent']} sent, {job['errors']} errors."
            db.session.commit()


def _get_member_devices():
    """Get devices the current member has access to."""
    if current_user.is_admin:
        return GowaDevice.query.filter_by(status="active").order_by(GowaDevice.name).all()
    return (
        GowaDevice.query.join(GowaDeviceAccess)
        .filter(GowaDeviceAccess.member_id == current_user.id, GowaDevice.status == "active")
        .order_by(GowaDevice.name)
        .all()
    )


@bp.get("/uploads/<filename>")
def serve_upload(filename):
    """Serve temporary files for WhatsApp to download."""
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)


@bp.route("")
@login_required
def index():
    if not has_feature_access(current_user, FEATURE_BULK_WHATSAPP):
        return render_template("error.html", title="Not permitted",
                               message="You do not have access to the bulk messaging feature."), 403
    devices = _get_member_devices()
    return render_template("whatsapp.html", devices=devices)


def _handle_send(is_test=False):
    if not has_feature_access(current_user, FEATURE_BULK_WHATSAPP):
        return jsonify(error="Access denied."), 403

    if is_test:
        from flask_wtf.csrf import validate_csrf
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except Exception:
            return jsonify(error="Invalid CSRF token. Refresh the page and try again."), 400

        from .settings import get_setting
        test_number = request.form.get("test_recipient", "").strip() or get_setting("test_whatsapp_number", "").strip()
        if not test_number:
            return jsonify(error="Enter a test phone number or configure default in settings."), 400
        if not PHONE_PATTERN.match(test_number):
            return jsonify(error="Enter a valid test phone number."), 400

    device_id = request.form.get("device_id", "").strip()
    message = request.form.get("message", "").strip()

    if not device_id:
        return jsonify(error="Select a WhatsApp device."), 400
    if not message:
        return jsonify(error="Message is required."), 400

    device = db.session.get(GowaDevice, device_id)
    if not device or device.status != "active":
        return jsonify(error="Selected device is not available."), 400

    # Check member access
    if not current_user.is_admin:
        has_access = GowaDeviceAccess.query.filter_by(device_id=device.id, member_id=current_user.id).first()
        if not has_access:
            return jsonify(error="You do not have access to this device."), 403

    list_file = request.files.get("phone_list")
    recipients = []
    rows_data = []

    if list_file and getattr(list_file, "filename", ""):
        problem = validate_upload(getattr(list_file, "filename", ""), PHONE_LIST)
        if problem:
            return jsonify(error=problem), 400
        try:
            recipients, rows_data = read_recipients(list_file)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    if is_test:
        recipients = [test_number]
        row_data = {"name": current_user.name, "email": "", "phone": test_number}
        if rows_data:
            rows_data = [rows_data[0]]
        else:
            rows_data = [row_data]
    else:
        if not recipients:
            return jsonify(error="No phone numbers were found under the PHONE column."), 400

    max_recipients = current_app.config.get("MAX_RECIPIENTS", 2000)
    if not is_test and len(recipients) > max_recipients:
        return jsonify(error=f"The list exceeds the {max_recipients} recipient limit."), 400

    lock_pdf = None
    if "lock_pdf_submitted" in request.form:
        lock_pdf = request.form.get("lock_substituted_pdf") in {"true", "1", "on", "yes"}
    elif "lock_substituted_pdf" in request.form:
        lock_pdf = request.form.get("lock_substituted_pdf") in {"true", "1", "on", "yes"}

    pdf_alignment = request.form.get("pdf_text_alignment") or None

    attachments = []
    for idx, uploaded in enumerate(request.files.getlist("attachments")):
        if not uploaded.filename:
            continue
        filename = secure_filename(uploaded.filename)
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_ATTACHMENTS:
            return jsonify(error=f"Unsupported attachment: {filename}."), 400
        content = uploaded.read()
        if hasattr(uploaded, "seek"):
            try:
                uploaded.seek(0)
            except Exception:
                pass

        should_convert = request.form.get(f"convert_pdf_{idx}") == "1"
        if is_test:
            content = substitute_attachment(filename, content, rows_data[0], convert_pdf=should_convert, lock_pdf=lock_pdf, pdf_alignment=pdf_alignment)
            if should_convert and not filename.lower().endswith(".pdf"):
                filename = Path(filename).stem + ".pdf"

        # Note: Process job handles substitute_attachment for non-test mode so we don't do it here
        # Actually wait, in process_job it needs the flag `should_convert`.
        attachments.append((filename, content, should_convert))

    total = len(recipients)
    job_id = secrets.randbelow(2**53 - 1)
    created_at = datetime.now(timezone.utc)

    with jobs_lock:
        jobs[job_id] = {
            "total": total, "results": [], "complete": False,
            "sent": 0, "errors": 0, "created_at": created_at,
            "completed_at": None, "duration_seconds": None,
            "created_by_name": current_user.name,
        }

    db_job = WhatsAppJob(
        id=job_id,
        created_by=current_user,
        created_by_name=current_user.name,
        total=total,
        device_id=device.id,
        device_name=device.name,
        message_preview=message[:500],
        attachment_count=len(attachments),
    )
    db.session.add(db_job)
    
    if is_test:
        record_service_audit(
            service_key=FEATURE_BULK_WHATSAPP,
            action="send_test",
            request_data={"test_number": test_number, "device_id": device.id, "device_name": device.name},
            response_data={"job_id": job_id},
            status="SUCCESS",
        )
    else:
        record_service_audit(
            service_key=FEATURE_BULK_WHATSAPP,
            action="send_campaign",
            request_data={
                "device_id": device.id,
                "device_name": device.name,
                "recipient_count": total,
                "message_preview": message[:100],
            },
            response_data={"job_id": job_id},
            status="SUCCESS",
        )
    db.session.commit()

    app = current_app._get_current_object()
    user_name = current_user.name
    device_device_id = device.device_id if device else None

    def run_with_context():
        with app.app_context():
            process_job(job_id, recipients, message, attachments, device_device_id, user_name, rows_data, lock_pdf=lock_pdf, pdf_alignment=pdf_alignment)

    if is_test:
        run_with_context()
        job = jobs[job_id]
        if job["errors"] > 0:
            error_reason = job["results"][0]["reason"] if job["results"] else "Unknown error"
            return jsonify(error=error_reason), 400
        return jsonify(message=f"Test WhatsApp message sent to {test_number}."), 200
    else:
        worker = threading.Thread(target=run_with_context, daemon=True)
        worker.start()
        return jsonify(job_id=job_id), 202

@bp.post("/send")
@login_required
def send():
    return _handle_send(is_test=False)


@bp.get("/jobs/<int:job_id>")

@login_required
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error="Job not found."), 404
        snapshot = {
            "total": job["total"],
            "results": list(job["results"]),
            "sent": job["sent"],
            "errors": job["errors"],
            "complete": job["complete"],
            "duration_seconds": job.get("duration_seconds"),
            "created_by_name": job.get("created_by_name", ""),
        }
    return jsonify(snapshot)


@bp.get("/jobs/<int:job_id>/export")
@login_required
def export_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error="Job not found."), 404

    db_job = db.session.get(WhatsAppJob, job_id)
    if not db_job:
        return jsonify(error="Job record not found."), 404

    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "WhatsApp Results"
    ws.append(["Job ID", "Device", "Created By", "Created At"])
    ws.append([job_id, db_job.device_name, db_job.created_by_name,
               db_job.created_at.strftime("%Y-%m-%d %H:%M:%S") if db_job.created_at else ""])
    ws.append([])
    ws.append(["Phone", "Status", "Reason"])
    for r in db_job.recipients:
        ws.append([r.phone, r.status, r.reason])
    ws.append([])
    ws.append(["Summary", db_job.summary or f"{db_job.total} total, {db_job.sent} sent, {db_job.errors} errors"])
    ws.append(["Duration (seconds)", db_job.duration_seconds])

    for row in ws.iter_rows(min_row=1, max_row=3):
        for cell in row:
            if cell.value:
                cell.font = Font(bold=True)

    # openpyxl writes nothing until asked. Without this the .xlsx is valid HTTP
    # but empty: 200, right Content-Type, zero bytes.
    wb.save(output)
    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"whatsapp_{str(job_id)[-12:]}.xlsx",
    )


# ─── Device Management (Admin only) ─────────────────────────────────────────

def sync_gowa_devices(user=None, remove_stale=True):
    """Fetch devices from GOWA service and sync with local GowaDevice database.

    If remove_stale is True, any locally recorded GowaDevice whose device_id is not
    present in the fetched GOWA devices list is removed.
    Returns (synced_devices, removed_count).
    """
    fetched = get_gowa_devices()
    synced = []
    removed_count = 0

    fetched_dev_ids = {
        gdev.get("device_id")
        for gdev in fetched
        if gdev.get("device_id") and gdev.get("device_id") != "unknown"
    }

    if remove_stale:
        all_local = GowaDevice.query.all()
        for loc_dev in all_local:
            if loc_dev.device_id not in fetched_dev_ids:
                # Remove access grants first
                GowaDeviceAccess.query.filter_by(device_id=loc_dev.id).delete()
                db.session.delete(loc_dev)
                removed_count += 1

    for gdev in fetched:
        dev_id = gdev.get("device_id")
        if not dev_id or dev_id == "unknown":
            continue
        existing = GowaDevice.query.filter_by(device_id=dev_id).first()
        if not existing:
            device = GowaDevice(
                name=gdev.get("name") or "GOWA Device",
                device_id=dev_id,
                status=gdev.get("status", "active"),
                registered_by=user,
                registered_by_name=user.name if user else "GOWA Service",
            )
            db.session.add(device)
            synced.append(device)
        else:
            existing.status = gdev.get("status", "active")
            if gdev.get("name") and (existing.name == "GOWA Device" or not existing.name):
                existing.name = gdev["name"]
            synced.append(existing)

    if synced or removed_count > 0:
        db.session.commit()

    return synced, removed_count


@bp.route("/devices")
@login_required
def device_list():
    if not current_user.is_admin:
        return jsonify(error="Admin access required."), 403

    # Probe GOWA. If online, fetch and sync registered devices from GOWA service.
    gowa_health = check_health()
    if gowa_health["online"]:
        try:
            sync_gowa_devices(current_user, remove_stale=True)
        except Exception as exc:
            logger.warning("Error syncing GOWA devices: %s", exc)

    # Get devices from local database
    local_devices = GowaDevice.query.order_by(GowaDevice.created_at.desc()).all()

    from . import gowa_manager
    gowa_status = gowa_manager.get_gowa_process_status(current_app)

    return render_template(
        "whatsapp_devices.html",
        devices=local_devices,
        gowa_devices=[],
        gowa_health=gowa_health,
        gowa_status=gowa_status,
    )


@bp.post("/devices/fetch")
@login_required
def device_fetch():
    if not current_user.is_admin:
        return jsonify(error="Admin access required."), 403

    health = check_health()
    if not health["online"]:
        flash(f"GOWA is unavailable. {health['error']}", "error")
        return redirect(url_for("whatsapp.device_list"))

    try:
        synced, removed_count = sync_gowa_devices(current_user, remove_stale=True)
        msg = f"Successfully synced {len(synced)} device(s) from GOWA."
        if removed_count > 0:
            msg += f" Removed {removed_count} old device(s)."
        flash(msg, "success")
    except Exception as exc:
        flash(f"Failed to fetch devices from GOWA: {exc}", "error")

    return redirect(url_for("whatsapp.device_list"))


@bp.post("/devices/<int:device_id>/delete")
@login_required
def device_delete(device_id):
    if not current_user.is_admin:
        return jsonify(error="Admin access required."), 403

    device = db.get_or_404(GowaDevice, device_id)
    # Remove access grants first
    GowaDeviceAccess.query.filter_by(device_id=device.id).delete()
    db.session.delete(device)
    db.session.commit()
    return jsonify(message="Device deleted."), 200


@bp.get("/devices/<int:device_id>/access")
@login_required
def device_access(device_id):
    if not current_user.is_admin:
        return jsonify(error="Admin access required."), 403

    device = db.get_or_404(GowaDevice, device_id)
    access_grants = GowaDeviceAccess.query.filter_by(device_id=device.id).all()
    members = Member.query.order_by(Member.name).all()
    return render_template("whatsapp_device_access.html", device=device, access_grants=access_grants, members=members)


@bp.post("/devices/<int:device_id>/access")
@login_required
def device_access_update(device_id):
    if not current_user.is_admin:
        return jsonify(error="Admin access required."), 403

    device = db.get_or_404(GowaDevice, device_id)
    selected_member_ids = {int(v) for v in request.form.getlist("member_ids", type=int)}

    # Remove existing grants
    GowaDeviceAccess.query.filter_by(device_id=device.id).delete()

    # Add new grants
    for member_id in selected_member_ids:
        member = db.session.get(Member, member_id)
        if member:
            grant = GowaDeviceAccess(device=device, member=member, granted_by=current_user)
            db.session.add(grant)

    db.session.commit()
    return jsonify(message="Access updated."), 200


@bp.post("/test")
@login_required
def test_send():
    return _handle_send(is_test=True)
