import base64
import io
import mimetypes
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file, current_app
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Font
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from .service_audit import record_service_audit
from .extensions import db
from .feature_access import has_feature_access
from .models import FEATURE_BULK_EMAIL, BulkEmailJob, BulkEmailRecipient
from .recipient_list import EMAIL_LIST, validate_upload
from .recipient_list import read_recipients as _read_recipients
from .shared import (
    substitute_template, substitute_html, substitute_docx, substitute_xlsx,
    substitute_attachment, clean_html, convert_to_pdf,
    is_html, is_doc, is_pdf, is_convertible_to_pdf, is_cleanable_html,
)

bp = Blueprint("bulk_email", __name__, url_prefix="/apps/emailer")

ALLOWED_ATTACHMENTS = {".pdf", ".docx", ".xlsx", ".pptx", ".htm", ".html"}
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
jobs = {}
jobs_lock = threading.Lock()

# Human-readable error messages for common SendGrid HTTP status codes
ERROR_MESSAGES = {
    400: "Bad request — check your email content and recipient addresses.",
    401: "Authentication failed — the SendGrid API key is invalid or missing.",
    403: "Permission denied — the sender address is not verified in SendGrid.",
    404: "Not found — the requested resource does not exist.",
    413: "Payload too large — reduce attachment size or recipient count.",
    429: "Rate limited — too many requests. Wait a moment and try again.",
    500: "SendGrid server error — try again later.",
    502: "Bad gateway — SendGrid is temporarily unavailable.",
    503: "Service unavailable — SendGrid is down for maintenance.",
    504: "Gateway timeout — the request took too long. Try again.",
}


def _friendly_error(status_code, response_body=None):
    """Return a user-friendly error message for an HTTP status code with parsed SendGrid details."""
    base = ERROR_MESSAGES.get(status_code, f"SendGrid returned HTTP {status_code}.")
    if response_body:
        if isinstance(response_body, bytes):
            response_body = response_body.decode("utf-8", errors="replace")
        if isinstance(response_body, str):
            body_str = response_body.strip()
            if "max content-length exceeded" in body_str.lower():
                return "Payload size exceeds SendGrid limit (30 MB). Please reduce total attachment file size."
            if body_str.startswith("{"):
                try:
                    data = json.loads(body_str)
                    if isinstance(data, dict) and "errors" in data:
                        err_texts = []
                        for e in data["errors"]:
                            if isinstance(e, dict) and e.get("message"):
                                fld = e.get("field")
                                msg = f"{fld}: {e['message']}" if fld else e["message"]
                                err_texts.append(msg)
                        if err_texts:
                            return f"{base} Details: {' | '.join(err_texts)}"
                except Exception:
                    pass
            if len(body_str) > 0:
                return f"{base} Details: {body_str[:300]}"
    return base


def extract_sendgrid_error(exc):
    """Extract human-readable error message and HTTP status code from a SendGrid exception."""
    status_code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)

    if body is None and hasattr(exc, "read"):
        try:
            body = exc.read()
        except Exception:
            pass

    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")

    exc_str = str(exc) or exc.__class__.__name__

    if status_code is None:
        match = re.search(r"HTTP Error (\d{3})", exc_str)
        if match:
            status_code = int(match.group(1))

    if status_code is not None:
        return status_code, _friendly_error(status_code, body or exc_str)

    return 500, exc_str


def _read_file_content(uploaded_file):
    """Read uploaded file content, handling both .read() and BytesIO."""
    content = uploaded_file.read()
    if isinstance(content, str):
        return content
    return content


def _is_html(filename):
    return Path(filename).suffix.lower() in {".htm", ".html"}


def _is_doc(filename):
    return Path(filename).suffix.lower() in {".doc", ".docx"}


def _is_pdf(filename):
    return Path(filename).suffix.lower() == ".pdf"


def _is_office(filename):
    return Path(filename).suffix.lower() in {".xlsx", ".pptx"}


def _is_convertible_to_pdf(filename):
    """Check if the file can be converted to PDF."""
    return _is_html(filename) or _is_doc(filename)


def _is_cleanable_html(filename):
    """Check if the file is HTML and can be cleaned/inlined."""
    return _is_html(filename)


def _clean_html(html_content, base_url=None):
    """Clean HTML by inlining CSS, JS, and embedding images as base64.

    - Fetches external stylesheets via HTTP and inlines them as <style>
    - Converts <img src="..."> to base64 data URIs
    - Inlines external <script src="..."> as inline <script>
    - Preserves existing <style> and <script> tags
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html_content

    soup = BeautifulSoup(html_content, "html.parser")

    # Inline external stylesheets
    for link in soup.find_all("link", rel="stylesheet"):
        href = link.get("href", "")
        if not href:
            link.decompose()
            continue
        if href.startswith("data:"):
            continue
        if base_url and not href.startswith(("http://", "https://", "//")):
            from urllib.parse import urljoin
            href = urljoin(base_url, href)
        if href.startswith("//"):
            href = "https:" + href
        try:
            import urllib.request
            req = urllib.request.Request(href, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                css_content = resp.read().decode("utf-8", errors="replace")
            style_tag = soup.new_tag("style")
            style_tag.string = css_content
            link.replace_with(style_tag)
        except Exception:
            link.decompose()

    # Inline external scripts
    for script in soup.find_all("script", src=True):
        src = script.get("src", "")
        if not src:
            continue
        if base_url and not src.startswith(("http://", "https://", "//")):
            from urllib.parse import urljoin
            src = urljoin(base_url, src)
        if src.startswith("//"):
            src = "https:" + src
        try:
            import urllib.request
            req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                js_content = resp.read().decode("utf-8", errors="replace")
            script["src"] = None
            script.string = js_content
        except Exception:
            script.decompose()

    # Embed images as base64 data URIs
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        if src.startswith("data:"):
            continue
        if base_url and not src.startswith(("http://", "https://", "//")):
            from urllib.parse import urljoin
            src = urljoin(base_url, src)
        if src.startswith("//"):
            src = "https:" + src
        try:
            import urllib.request
            req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                img_data = resp.read()
                content_type = resp.headers.get("Content-Type", "image/png")
                if ";" in content_type:
                    content_type = content_type.split(";")[0]
            encoded = base64.b64encode(img_data).decode("ascii")
            img["src"] = f"data:{content_type};base64,{encoded}"
        except Exception:
            pass  # Keep original src if fetch fails

    return str(soup)


def _html_to_pdf(html_content):
    """Convert HTML content to PDF using weasyprint."""
    try:
        from weasyprint import HTML
        pdf_bytes = HTML(string=html_content).write_pdf()
        return pdf_bytes
    except Exception:
        return None


def _docx_to_pdf(docx_content, filename="document.docx"):
    """Convert DOCX content to PDF using python-docx + reportlab."""
    try:
        from docx import Document
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError:
        return None

    doc = Document(io.BytesIO(docx_content))
    pdf_buffer = io.BytesIO()
    pdf_doc = SimpleDocTemplate(pdf_buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            style = styles["Normal"]
            if para.style and para.style.name.startswith("Heading"):
                style = styles["Heading1"]
            story.append(Paragraph(text, style))
            story.append(Spacer(1, 6))

    if not story:
        story.append(Paragraph("(empty document)", styles["Normal"]))

    pdf_doc.build(story)
    return pdf_buffer.getvalue()


def _doc_to_pdf(doc_content, filename="document.doc"):
    """Convert legacy DOC content to PDF (basic text extraction)."""
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph
        from reportlab.lib.styles import getSampleStyleSheet
    except ImportError:
        return None

    # Basic text extraction from DOC binary format
    text = doc_content.decode("utf-8", errors="replace")
    # Remove non-printable characters
    text = "".join(c for c in text if c.isprintable() or c in "\n\r\t")

    pdf_buffer = io.BytesIO()
    pdf_doc = SimpleDocTemplate(pdf_buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    story = [Paragraph(text[:5000], styles["Normal"])]
    pdf_doc.build(story)
    return pdf_buffer.getvalue()


def _convert_to_pdf(filename, content):
    """Convert file content to PDF based on file type."""
    suffix = Path(filename).suffix.lower()
    if suffix in {".htm", ".html"}:
        return _html_to_pdf(content.decode("utf-8", errors="replace"))
    elif suffix == ".docx":
        return _docx_to_pdf(content, filename)
    elif suffix == ".doc":
        return _doc_to_pdf(content, filename)
    return None


def read_recipients(file_storage):
    """Read a CSV email list and return (recipients, rows) for substitution.

    Thin wrapper over :func:`recipient_list.read_recipients` so the required
    column is named once here rather than at each call site, and so the email
    reader cannot drift away from the phone one.
    """
    return _read_recipients(file_storage, "EMAIL")


def make_message(sender, recipient, subject, body, attachments):
    from sendgrid.helpers.mail import Mail, Attachment, Disposition, FileContent, FileName, FileType
    message = Mail(
        from_email=sender,
        to_emails=recipient,
        subject=subject,
        html_content=body,
    )
    for filename, content in attachments:
        content_type, _ = mimetypes.guess_type(filename)
        if content_type is None:
            content_type = "application/octet-stream"
        encoded_content = base64.b64encode(content).decode("ascii")
        message.add_attachment(
            Attachment(
                FileContent(encoded_content),
                FileName(filename),
                FileType(content_type),
                Disposition("attachment"),
            )
        )
    return message


def _add_recipient_result(job_id, to_email, status, reason=None):
    with jobs_lock:
        job = jobs[job_id]
        db.session.add(BulkEmailRecipient(job_id=job_id, to_email=to_email, status=status, reason=reason or ""))
        db.session.commit()
        job["results"].append({"to": to_email, "status": status, "reason": reason or ""})
        if status == "SENT":
            job["sent"] += 1
        else:
            job["errors"] += 1
        if job["sent"] + job["errors"] >= job["total"]:
            job["complete"] = True
            job["completed_at"] = datetime.now(timezone.utc)


def process_job(job_id, recipients, sender, subject, body, attachments, created_by_name, api_key, rows_data):
    client = None
    client_error = None
    try:
        from sendgrid import SendGridAPIClient
        api_key = current_app.config.get("SENDGRID_API_KEY", "")
        if api_key:
            client = SendGridAPIClient(api_key=api_key.strip())
    except Exception as exc:
        client_error = str(exc) or exc.__class__.__name__

    job = jobs[job_id]
    job["created_by_name"] = created_by_name

    try:
        for idx, recipient in enumerate(recipients):
            # Check for cancellation
            with jobs_lock:
                if job.get("cancelled"):
                    break

            if not EMAIL_PATTERN.fullmatch(recipient):
                _add_recipient_result(job_id, recipient, "ERROR", "Invalid email address.")
                continue
            if client_error:
                _add_recipient_result(job_id, recipient, "ERROR", client_error)
                continue
            try:
                # Get row data for this recipient (empty dict if no data)
                row_data = rows_data[idx] if idx < len(rows_data) else {}

                # Substitute template variables in email body
                customized_body = substitute_template(body, row_data)

                from sendgrid.helpers.mail import Mail, Attachment, Disposition, FileContent, FileName, FileType
                customized_subject = substitute_template(subject, row_data)
                message = Mail(
                    from_email=sender,
                    to_emails=recipient,
                    subject=customized_subject,
                    plain_text_content=customized_body,
                    html_content=customized_body,
                )
                for att in attachments:
                    if len(att) == 3:
                        filename, content, should_convert = att
                    else:
                        filename, content = att
                        should_convert = False

                    # Substitute template variables in attachment for this recipient
                    customized_content = substitute_attachment(filename, content, row_data, convert_pdf=should_convert)
                    out_filename = Path(filename).stem + ".pdf" if (should_convert and not filename.lower().endswith(".pdf")) else filename

                    content_type, _ = mimetypes.guess_type(out_filename)
                    if content_type is None:
                        content_type = "application/octet-stream"
                    encoded_content = base64.b64encode(customized_content).decode("ascii")
                    message.add_attachment(
                        Attachment(
                            FileContent(encoded_content),
                            FileName(out_filename),
                            FileType(content_type),
                            Disposition("attachment"),
                        )
                    )



                # Retry logic: up to 3 attempts for transient failures
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        response = client.send(message)
                        status_code = int(response.status_code)
                        if 200 <= status_code < 300:
                            _add_recipient_result(job_id, recipient, "SENT")
                            break
                        elif status_code in (429, 500, 502, 503) and attempt < max_retries - 1:
                            time.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s, 4s
                            continue
                        else:
                            response_body = response.body
                            if isinstance(response_body, bytes):
                                response_body = response_body.decode("utf-8", errors="replace")
                            reason = f"SendGrid API returned HTTP {status_code}"
                            if response_body:
                                reason = f"{reason}: {response_body}"
                            _add_recipient_result(job_id, recipient, "ERROR", reason)
                            break
                    except Exception as exc:
                        if attempt < max_retries - 1:
                            time.sleep(2 ** attempt)
                            continue
                        _add_recipient_result(job_id, recipient, "ERROR", str(exc) or exc.__class__.__name__)
                        break
            except Exception as exc:
                _add_recipient_result(job_id, recipient, "ERROR", str(exc) or exc.__class__.__name__)
    finally:
        with jobs_lock:
            job["complete"] = True
            job["completed_at"] = datetime.now(timezone.utc)
            job["duration_seconds"] = int((job["completed_at"] - job["created_at"]).total_seconds())
            db_job = db.session.get(BulkEmailJob, job_id)
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
    if not has_feature_access(current_user, FEATURE_BULK_EMAIL):
        return render_template("error.html", title="Not permitted",
                               message="You do not have access to the bulk email feature."), 403
    return render_template("bulk_email.html")


def _handle_send(is_test=False):
    if not has_feature_access(current_user, FEATURE_BULK_EMAIL):
        return jsonify(error="Access denied."), 403

    if is_test:
        from flask_wtf.csrf import validate_csrf
        try:
            validate_csrf(request.form.get("csrf_token", ""))
        except Exception:
            return jsonify(error="Invalid CSRF token. Refresh the page and try again."), 400

        from .settings import get_setting
        test_email = request.form.get("test_recipient", "").strip() or get_setting("test_email_account", "").strip()
        if not test_email:
            return jsonify(error="Enter a test email address or configure default in settings."), 400
        if not EMAIL_PATTERN.fullmatch(test_email):
            return jsonify(error="Enter a valid test email address."), 400

    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "")
    sender = request.form.get("sender", "").strip()
    if not subject or not body.strip():
        return jsonify(error="Email subject and body are required."), 400
    if "\r" in subject or "\n" in subject:
        return jsonify(error="Email subject cannot contain line breaks."), 400
    if not EMAIL_PATTERN.fullmatch(sender):
        return jsonify(error="Enter a valid Email From address."), 400

    list_file = request.files.get("email_list")
    
    # Handle CSV upload
    recipients = []
    rows_data = []
    if list_file and getattr(list_file, "filename", ""):
        problem = validate_upload(getattr(list_file, "filename", ""), EMAIL_LIST)
        if problem:
            return jsonify(error=problem), 400
        try:
            recipients, rows_data = read_recipients(list_file)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    # Test override
    if is_test:
        recipients = [test_email]
        row_data = {"name": current_user.name, "email": test_email, "phone": ""}
        if rows_data:
            rows_data = [rows_data[0]]
        else:
            rows_data = [row_data]
    else:
        if not recipients:
            return jsonify(error="No email addresses were found under the EMAIL column."), 400

    max_recipients = current_app.config.get("MAX_RECIPIENTS", 2000)
    if not is_test and len(recipients) > max_recipients:
        return jsonify(error=f"The list exceeds the {max_recipients} recipient limit."), 400

    attachments = []
    for idx, uploaded in enumerate(request.files.getlist("attachments")):
        if not uploaded.filename:
            continue
        filename = secure_filename(uploaded.filename)
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_ATTACHMENTS:
            return jsonify(error=f"Unsupported attachment: {filename}. Use PDF, DOC, DOCX, XLSX, PPTX, HTM, or HTML."), 400
        content = uploaded.read()
        if hasattr(uploaded, "seek"):
            try:
                uploaded.seek(0)
            except Exception:
                pass

        # Always clean HTML
        if is_cleanable_html(filename):
            content = clean_html(content.decode("utf-8", errors="replace")).encode("utf-8")

        # Convert to PDF flag
        should_convert = request.form.get(f"convert_pdf_{idx}") == "1"

        attachments.append((filename, content, should_convert))

    total_att_bytes = sum(len(att[1]) for att in attachments)
    if total_att_bytes > 20 * 1024 * 1024:
        total_mb = total_att_bytes / (1024 * 1024)
        return jsonify(error=f"Total attachment size ({total_mb:.1f} MB) exceeds SendGrid's 20 MB limit."), 400

    total = len(recipients)
    job_id = secrets.randbelow(2**53 - 1)
    created_at = datetime.now(timezone.utc)
    api_key = current_app.config.get("SENDGRID_API_KEY", "")

    with jobs_lock:
        jobs[job_id] = {
            "total": total, "results": [], "complete": False,
            "sent": 0, "errors": 0, "created_at": created_at,
            "completed_at": None, "duration_seconds": None,
            "created_by_name": current_user.name,
            "cancelled": False,
        }

    db_job = BulkEmailJob(
        id=job_id,
        created_by=current_user,
        created_by_name=current_user.name,
        total=total,
        subject=subject,
        sender=sender,
        attachment_count=len(attachments),
    )
    db.session.add(db_job)
    
    if is_test:
        from .models import ProjectAuditEvent
        db.session.add(ProjectAuditEvent(
            project_key="BULK_EMAIL",
            project_name="Bulk Emailer",
            actor_id=current_user.id,
            actor_name=current_user.name,
            actor_email=current_user.email,
            action="TEST",
            entity_type="Bulk Email",
            summary=f"Test email sent to {test_email}",
        ))
        record_service_audit(
            service_key=FEATURE_BULK_EMAIL,
            action="send_test",
            request_data={"test_email": test_email, "subject": subject, "sender": sender},
            response_data={"job_id": job_id},
            status="SUCCESS",
        )
    else:
        record_service_audit(
            service_key=FEATURE_BULK_EMAIL,
            action="send_campaign",
            request_data={"subject": subject, "sender": sender, "recipient_count": total, "attachment_count": len(attachments)},
            response_data={"job_id": job_id},
            status="SUCCESS",
        )
        
    db.session.commit()

    app = current_app._get_current_object()
    user_name = current_user.name

    def run_with_context():
        with app.app_context():
            process_job(job_id, recipients, sender, subject, body, attachments, user_name, api_key, rows_data)

    if is_test:
        run_with_context()
        job = jobs[job_id]
        if job["errors"] > 0:
            error_reason = job["results"][0]["reason"] if job["results"] else "Unknown error"
            return jsonify(error=error_reason), 400
        return jsonify(message=f"Test email sent to {test_email}."), 200
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
            "subject": job.get("subject", ""),
            "sender": job.get("sender", ""),
            "attachment_count": job.get("attachment_count", 0),
        }
    return jsonify(snapshot)


@bp.get("/jobs/<int:job_id>/export")
@login_required
def export_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error="Job not found."), 404

    db_job = db.session.get(BulkEmailJob, job_id)
    if not db_job:
        return jsonify(error="Job record not found."), 404

    output = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.title = "Bulk Email Results"
    ws.append(["Job ID", "Subject", "From", "Created By", "Created At"])
    ws.append([job_id, db_job.subject, db_job.sender, db_job.created_by_name,
               db_job.created_at.strftime("%Y-%m-%d %H:%M:%S") if db_job.created_at else ""])
    ws.append([])
    ws.append(["To", "Status", "Reason"])
    for r in db_job.recipients:
        ws.append([r.to_email, r.status, r.reason])
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
        download_name=f"bulk_email_{str(job_id)[-12:]}.xlsx",
    )


@bp.post("/test")
@login_required
def test_send():
    return _handle_send(is_test=True)


@bp.post("/cancel/<int:job_id>")
@login_required
def cancel_job(job_id):
    """Cancel a running bulk email job."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error="Job not found."), 404
        if job["complete"]:
            return jsonify(error="Job is already complete."), 400
        job["cancelled"] = True
        job["complete"] = True
        job["completed_at"] = datetime.now(timezone.utc)
        job["duration_seconds"] = int((job["completed_at"] - job["created_at"]).total_seconds())

        db_job = db.session.get(BulkEmailJob, job_id)
        if db_job:
            db_job.cancelled_at = job["completed_at"]
            db_job.duration_seconds = job["duration_seconds"]
            db_job.summary = f"Cancelled: {job['sent']} sent, {job['errors']} errors before cancellation."
            db.session.commit()

    return jsonify(message="Job cancelled."), 200


@bp.errorhandler(RequestEntityTooLarge)
def request_too_large(_error):
    return jsonify(error="Combined upload size exceeds the 16 MB limit."), 413
