"""Bulk SMS — templated text messages through Pingram.

The third campaign composer alongside Bulk Emailer and Bulk Messenger, and the
closest of the three to the WhatsApp one: it reads a PHONE column from a CSV
list, substitutes ``{{ COLUMN }}`` placeholders per recipient, and reports
per-recipient results as the campaign runs.

The one thing it does differently is length. A single SMS is a metered unit:
160 characters on GSM-7 networks, 70 on the wider-character ones that Arabic,
Cyrillic and emoji require. The limit is enforced in three places because
substitution makes it per-recipient, not per-campaign — see :func:`fits_one_sms`
and its use in :func:`send`.
"""

import io
import logging
import re
import secrets
import threading
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request, send_file
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Font

from .service_audit import record_service_audit
from .extensions import db
from .feature_access import has_feature_access
from .models import FEATURE_BULK_SMS, SmsJob, SmsRecipient
from .pingram import api_base_url, is_configured, region, send_sms
from .recipient_list import PHONE_LIST, validate_upload
from .recipient_list import read_recipients as _read_recipients

logger = logging.getLogger("mizalla.bulk_sms")

bp = Blueprint("bulk_sms", __name__, url_prefix="/apps/sms")

#: Loose shape check. Pingram does the authoritative validation and its reason
#: is shown to the member, so this only catches values that cannot be a number
#: at all — which would otherwise be billed as a failed send.
PHONE_PATTERN = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
from .shared import TEMPLATE_PATTERN

# ─── One-SMS limit ────────────────────────────────────────────────────────────

#: The GSM 03.38 basic alphabet. A message drawn entirely from this plus
#: :data:`GSM_EXTENSION` packs 160 characters into one segment; anything else
#: forces UCS-2, which packs 70.
GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ"
    "ÆæßÉ !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

#: Characters that are on the handset but occupy a second septet in the encoded
#: message, so they cost against the 160-character budget.
GSM_EXTENSION = set("^{}\\[~]|€\f")

#: Segments the service will ever use. One, deliberately: a longer message is
#: billed as N messages and silently concatenated on the handset, which is
#: rarely what someone composing an SMS intended.
SEGMENTS = 1
GSM_7_PER_SEGMENT = 160
UCS_2_PER_SEGMENT = 70


def sms_encoding(text: str) -> str:
    """``"GSM-7"`` when ``text`` fits the narrow alphabet, else ``"UCS-2"``."""
    return "GSM-7" if all(ch in GSM_BASIC or ch in GSM_EXTENSION for ch in text) else "UCS-2"


def _measure(text: str) -> tuple[str, int, int]:
    """``(encoding, units, budget)`` for ``text``.

    ``units`` is what the message costs on the wire, which is not always its
    character count: a GSM-7 extension character (``{}``, ``€``, ``|``) encodes as
    an escape *plus* the character, so it uses two of the 160 available septets.
    ``budget`` is the segment size that encoding allows.

    Everything that needs to reason about length goes through this one function.
    When the rules were applied in three places separately they drifted, and a
    message could pass one check and fail another.
    """
    if sms_encoding(text) == "UCS-2":
        return "UCS-2", len(text), UCS_2_PER_SEGMENT
    extension_count = sum(1 for ch in text if ch in GSM_EXTENSION)
    return "GSM-7", len(text) + extension_count, GSM_7_PER_SEGMENT


def fits_one_sms(text: str) -> bool:
    """True when ``text`` occupies a single SMS segment."""
    if not text.strip():
        return False
    _, units, budget = _measure(text)
    return units <= budget


def segment_count(text: str) -> int:
    """Segments ``text`` occupies, rounded up.

    Reported rather than used to reject anything: it is what tells a member their
    message would be billed as more than one SMS, which is the information they
    need to decide what to cut.
    """
    if not text.strip():
        return 0
    _, units, budget = _measure(text)
    return -(-units // budget)  # ceiling division


def limit_message(text: str, what: str = "message") -> str:
    """The error for a ``text`` that will not fit one SMS, or "" when it does."""
    if fits_one_sms(text):
        return ""
    encoding, units, budget = _measure(text)
    widest = (
        "Arabic and other non-Latin text fits only 70"
        if encoding == "UCS-2"
        else "Latin text fits 160"
    )
    return (
        f"The {what} is {units} characters, which needs {segment_count(text)} SMS. "
        f"This service sends one SMS per recipient only ({widest}). Shorten it and try again."
    )


jobs = {}
jobs_lock = threading.Lock()


def _substitute_template(text: str, row_data: dict) -> str:
    """Replace ``{{ COLUMN }}`` with this recipient's values.

    Unknown columns and missing cells both become empty rather than raising,
    matching the other composers: a spreadsheet is edited by hand, and one
    renamed column should not fail the whole campaign.
    """

    def replace(match):
        raw_key = match.group(1) if match.group(1) is not None else match.group(2)
        key = (raw_key or "").strip().lower()
        value = row_data.get(key, "")
        return "" if value is None else str(value)

    return TEMPLATE_PATTERN.sub(replace, text)


def read_recipients(file_storage):
    """Read a CSV phone list and return ``(recipients, rows)`` for substitution.

    Thin wrapper over :func:`recipient_list.read_recipients` so the required
    column is named once here, and so this reader cannot drift away from the
    email one.
    """
    return _read_recipients(file_storage, "PHONE")


def _add_recipient_result(job_id, phone, status, reason=None, tracking_id=None):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        db.session.add(SmsRecipient(
            job_id=job_id, phone=phone, status=status,
            reason=reason or "", tracking_id=tracking_id or "",
        ))
        db.session.commit()
        job["results"].append({
            "to": phone, "status": status, "reason": reason or "",
            "tracking_id": tracking_id or "",
        })
        if status == "SENT":
            job["sent"] += 1
        else:
            job["errors"] += 1
        if job["sent"] + job["errors"] >= job["total"]:
            job["complete"] = True
            job["completed_at"] = datetime.now(timezone.utc)


def process_job(app, job_id, recipients, message, sender, created_by_name, rows_data):
    """Send to every recipient, recording a result for each.

    Runs on a worker thread. Each send is independent, so one unreachable number
    is recorded as an error and the campaign carries on — that is the whole
    point of a bulk sender.

    ``app`` is passed in and its context pushed because a bare thread has none,
    and ``db.session`` refuses to resolve outside an application context. The
    sibling services have the same latent fault; their worker threads only work
    because a request context happens to still be on the stack.
    """
    with app.app_context():
        _process_job(job_id, recipients, message, sender, created_by_name, rows_data)


def _process_job(job_id, recipients, message, sender, created_by_name, rows_data):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        job["created_by_name"] = created_by_name

    for idx, phone in enumerate(recipients):
        if not PHONE_PATTERN.match(phone):
            _add_recipient_result(job_id, phone, "ERROR", "Invalid phone number.")
            continue

        row_data = rows_data[idx] if idx < len(rows_data) else {}
        customized_message = _substitute_template(message, row_data)

        # Substitution can lengthen the message: a placeholder of 8 characters
        # can become a 60-character name. The campaign-level check that admitted
        # the job cannot catch that, so each rendered message is re-checked here
        # or the recipient would be billed for a multi-part SMS.
        over_limit = limit_message(customized_message, "rendered message")
        if over_limit:
            _add_recipient_result(job_id, phone, "ERROR", over_limit)
            continue

        try:
            result = send_sms(phone, customized_message, sender=sender or None)
            if "error" in result:
                _add_recipient_result(job_id, phone, "ERROR", result["error"])
            else:
                _add_recipient_result(job_id, phone, "SENT", tracking_id=result.get("tracking_id", ""))
        except Exception as exc:  # noqa: BLE001 - one bad recipient must not kill the campaign
            logger.exception("SMS send failed for %s", phone)
            _add_recipient_result(job_id, phone, "ERROR", str(exc) or exc.__class__.__name__)

    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return
        job["complete"] = True
        job["completed_at"] = datetime.now(timezone.utc)
        job["duration_seconds"] = int((job["completed_at"] - job["created_at"]).total_seconds())
        db_job = db.session.get(SmsJob, job_id)
        if db_job:
            db_job.completed_at = job["completed_at"]
            db_job.duration_seconds = job["duration_seconds"]
            db_job.sent = job["sent"]
            db_job.errors = job["errors"]
            db_job.summary = f"{job['total']} recipients: {job['sent']} sent, {job['errors']} errors."
            db.session.commit()


@bp.route("")
@login_required
def index():
    if not has_feature_access(current_user, FEATURE_BULK_SMS):
        return render_template("error.html", title="Not permitted",
                               message="You do not have access to the bulk SMS feature."), 403
    if not is_configured():
        return render_template(
            "error.html", title="Not configured",
            message="Bulk SMS needs PINGRAM_API_KEY and PINGRAM_REGION_CODE in the .env file. "
                    "An administrator must add them and restart the application.",
        ), 503
    return render_template(
        "bulk_sms.html",
        configured=True,
        region=region(),
        api_host=api_base_url(),
        max_characters=GSM_7_PER_SEGMENT,
    )


def _handle_send(is_test=False):
    if not has_feature_access(current_user, FEATURE_BULK_SMS):
        return jsonify(error="Access denied."), 403

    if is_test:
        from flask_wtf.csrf import validate_csrf
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except Exception:
            return jsonify(error="Invalid CSRF token. Refresh the page and try again."), 400

        from .settings import get_setting
        test_number = request.form.get("test_recipient", "").strip() or get_setting("test_sms_number", "").strip()
        if not test_number:
            return jsonify(error="Enter a test phone number or configure default in settings."), 400
        if not PHONE_PATTERN.match(test_number):
            return jsonify(error="Enter a valid test phone number."), 400

    if not is_configured():
        return jsonify(
            error="Bulk SMS is not configured. An administrator must add PINGRAM_API_KEY "
                  "and PINGRAM_REGION_CODE to the .env file."
        ), 503

    message = request.form.get("message", "").strip()
    sender = request.form.get("sender", "").strip() or None

    if not message:
        return jsonify(error="Message is required."), 400

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

    # Ensure message fits even before substitution in test (handled during processing too)
    if is_test:
        customized_message = _substitute_template(message, rows_data[0])
        over_limit = limit_message(customized_message)
        if over_limit:
            return jsonify(error=over_limit), 400
    else:
        over_limit = limit_message(message)
        if over_limit:
            return jsonify(error=over_limit), 400

    total = len(recipients)
    job_id = secrets.token_urlsafe(24)
    created_at = datetime.now(timezone.utc)

    with jobs_lock:
        jobs[job_id] = {
            "total": total, "results": [], "complete": False,
            "sent": 0, "errors": 0, "created_at": created_at,
            "completed_at": None, "duration_seconds": None,
            "created_by_name": current_user.name,
        }

    db_job = SmsJob(
        id=job_id,
        created_by=current_user,
        created_by_name=current_user.name,
        total=total,
        sender=sender or "",
        region=region(),
        message_preview=message[:500],
    )
    db.session.add(db_job)
    
    if is_test:
        record_service_audit(
            service_key=FEATURE_BULK_SMS,
            action="send_test",
            request_data={"test_number": test_number, "sender": sender or ""},
            response_data={"job_id": job_id},
            status="SUCCESS",
        )
    else:
        record_service_audit(
            service_key=FEATURE_BULK_SMS,
            action="send_campaign",
            request_data={"sender": sender or "", "recipient_count": total, "message_preview": message[:100]},
            response_data={"job_id": job_id},
            status="SUCCESS",
        )
    db.session.commit()

    app = current_app._get_current_object()
    user_name = current_user.name

    def run_with_context():
        with app.app_context():
            process_job(app, job_id, recipients, message, sender, user_name, rows_data)

    if is_test:
        run_with_context()
        job = jobs[job_id]
        if job["errors"] > 0:
            error_reason = job["results"][0]["reason"] if job["results"] else "Unknown error"
            return jsonify(error=error_reason), 502
        
        tracking_id = job["results"][0].get("tracking_id", "")
        suffix = f" Tracking ID: {tracking_id}" if tracking_id else ""
        return jsonify(message=f"Test SMS sent to {test_number}.{suffix}"), 200
    else:
        worker = threading.Thread(target=run_with_context, daemon=True)
        worker.start()
        return jsonify(job_id=job_id), 202

@bp.post("/send")
@login_required
def send():
    return _handle_send(is_test=False)


@bp.get("/jobs/<job_id>")
@login_required
def get_job(job_id):
    if not has_feature_access(current_user, FEATURE_BULK_SMS):
        return jsonify(error="Access denied."), 403
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


@bp.get("/jobs/<job_id>/export")
@login_required
def export_job(job_id):
    if not has_feature_access(current_user, FEATURE_BULK_SMS):
        return jsonify(error="Access denied."), 403

    db_job = db.session.get(SmsJob, job_id)
    if not db_job:
        return jsonify(error="Job record not found."), 404

    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "SMS Results"
    ws.append(["Job ID", "Sender", "Region", "Created By", "Created At"])
    ws.append([
        job_id, db_job.sender, db_job.region, db_job.created_by_name,
        db_job.created_at.strftime("%Y-%m-%d %H:%M:%S") if db_job.created_at else "",
    ])
    ws.append([])
    ws.append(["Phone", "Status", "Reason", "Tracking ID"])
    for r in db_job.recipients:
        ws.append([r.phone, r.status, r.reason, r.tracking_id])
    ws.append([])
    ws.append(["Summary", db_job.summary or f"{db_job.total} total, {db_job.sent} sent, {db_job.errors} errors"])
    ws.append(["Duration (seconds)", db_job.duration_seconds])

    for row in ws.iter_rows(min_row=1, max_row=3):
        for cell in row:
            if cell.value:
                cell.font = Font(bold=True)

    # The workbook only becomes bytes here. Without this the response is a
    # well-formed but empty .xlsx: status 200, correct Content-Type, zero
    # content, which opens as a file Excel cannot read.
    wb.save(output)
    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"sms_{job_id[:12]}.xlsx",
    )


@bp.post("/test")
@login_required
def test_send():
    return _handle_send(is_test=True)