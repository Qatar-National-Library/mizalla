"""Upload File: put documents in a folder on the server and get them back out.

A deliberately plain service. Unlike the other optional services it has no
upstream API to call, so it needs no credentials and can never be "not
configured" - which is why it is absent from ``FEATURE_REQUIREMENTS`` and always
shows as enabled in the header once granted.

Access is granted per member by an administrator and enforced on every route, not
merely hidden in the navigation, so a bookmarked or guessed URL cannot reach the
folder without the grant.

Files land in their own subfolder of ``UPLOAD_FOLDER`` rather than beside the
project attachments and the Chat library. Those three sets have different owners
and different lifecycles, and sharing one directory would make "delete this
document" ambiguous.

Two decisions worth stating, because both are security choices rather than
preferences:

* **Every download is an attachment.** ``.html`` and ``.htm`` are on the accepted
  list, so a member can upload markup that runs script. Serving that back with
  ``Content-Type: text/html`` would store a cross-site scripting attack against
  every member who opened it, in the app's own origin. Forcing
  ``Content-Disposition: attachment`` plus ``nosniff`` means the browser saves
  the file instead of rendering it, so the markup never executes against this app.
* **The on-disk name is generated, not the one the browser sent.** Two members
  uploading ``report.docx`` must not collide, and a filename must never be able
  to walk out of the upload directory.
"""

import html
from functools import wraps
from pathlib import Path
from uuid import uuid4

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from markupsafe import Markup
from werkzeug.utils import secure_filename

from .service_audit import record_service_audit
from .extensions import db
from .feature_access import has_feature_access
from .models import FEATURE_LABELS, FEATURE_UPLOAD, UploadedFile
from .settings import get_setting

bp = Blueprint("upload_file", __name__, url_prefix="/apps/upload-file")

#: Accepted suffixes, lower-cased, each including the leading dot.
ALLOWED_SUFFIXES = frozenset({
    ".pdf", ".txt", ".docx", ".xlsx", ".pptx", ".ppt", ".doc", ".html", ".htm",
})

#: Offered by the file input. Derived from ALLOWED_SUFFIXES so the control can
#: never drift from what the server actually accepts.
ACCEPT_ATTRIBUTE = ",".join(sorted(ALLOWED_SUFFIXES))

#: Human list for the rejection message and the page's helper text.
ALLOWED_LABEL = "PDF, TXT, DOCX, XLSX, PPTX, PPT, DOC, HTML, HTM"

#: Accepted types a browser would render as a document rather than save. These are
#: downloaded as ``application/octet-stream`` so the markup cannot execute.
MARKUP_SUFFIXES = frozenset({".html", ".htm"})

#: Subfolder this service owns. Appended to whichever base the setting names, so
#: stored files cannot land among project attachments or the Chat library even when
#: an administrator points the base at the shared uploads folder.
FOLDER_NAME = "upload-file"

#: Setting holding the base folder. Blank means "use UPLOAD_FOLDER".
SETTING_KEY = "upload_file_folder"


def upload_root() -> Path:
    """The folder this service stores files in, created if it does not exist yet.

    The base comes from the ``upload_file_folder`` setting, so an administrator can
    move the store to a shared drive without a code change or a redeploy. Blank
    means the application's own ``UPLOAD_FOLDER``. The setting is read per call
    rather than cached, so an administrator's change takes effect on the next
    request instead of the next restart.

    :func:`settings.get_setting` swallows database errors and falls back to the
    default, so a transient database problem degrades to the previous location
    rather than failing the upload.

    A folder that cannot be created is not handled here. Raising on every request
    would hide the reason behind a stack trace; instead the folder is created
    eagerly and the failure surfaces at the point of use, where the caller can
    report it.
    """
    configured = get_setting(SETTING_KEY).strip()
    if configured:
        root = Path(configured) / FOLDER_NAME
    else:
        root = Path(current_app.config["UPLOAD_FOLDER"]) / FOLDER_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def upload_required(view):
    """Refuse the view unless the signed-in member has been granted the service.

    Enforced here rather than only by hiding the header link, because a member
    who was never granted the service can still type the URL. The refusal is a
    rendered page, not a bare 403: the app-wide 403 speaks about project roles
    and would be actively misleading here.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not has_feature_access(current_user, FEATURE_UPLOAD):
            return render_template(
                "error.html",
                title="Not permitted",
                message=(
                    f"You do not have access to {FEATURE_LABELS[FEATURE_UPLOAD]}. "
                    "An administrator grants this service from Members."
                ),
            ), 403
        return view(*args, **kwargs)
    return wrapped


def can_delete(record: UploadedFile) -> bool:
    """Whether the signed-in member may delete ``record``.

    Used by the template so the button is not offered to someone whose click
    would be refused. The route re-checks regardless: a hidden button is a
    courtesy, not a control.
    """
    return current_user.is_admin or record.uploaded_by_id == current_user.id


def _usable_name(raw: str) -> str:
    """The client's filename reduced to something safe to store and echo back.

    ``secure_filename`` collapses path separators and strips anything that is not
    a filename character, so ``../../etc/passwd`` arrives as ``etc_passwd``. The
    result can still come back empty - a name written entirely in characters
    Werkzeug strips sanitises away entirely - which the caller treats as a
    rejection rather than inventing a name for it.
    """
    return secure_filename(raw)[:255]


@bp.route("", methods=["GET", "POST"])
@upload_required
def index():
    """The service page: upload control above, folder contents below.

    One route rather than two, because uploading and then seeing the result are a
    single action to the person doing them, and a post-redirect-get keeps a
    refresh from re-uploading the same file.
    """
    if request.method == "POST":
        uploads = request.files.getlist("file")
        if not uploads or not any(u.filename for u in uploads):
            record_service_audit(
                service_key=FEATURE_UPLOAD,
                action="upload_file",
                request_data={"filename": None},
                response_data={"error": "No file chosen"},
                status="ERROR",
            )
            flash("Choose a file to upload.", "error")
            return redirect(url_for("upload_file.index"))

        base_url = get_setting("upload_file_base_url").strip().rstrip("/")
        uploaded_filenames = []
        for upload in uploads:
            if not upload.filename:
                continue

            filename = _usable_name(upload.filename)
            suffix = Path(filename).suffix.lower()
            if not filename or suffix not in ALLOWED_SUFFIXES:
                record_service_audit(
                    service_key=FEATURE_UPLOAD,
                    action="upload_file",
                    request_data={"filename": upload.filename},
                    response_data={"error": "Unaccepted file type"},
                    status="ERROR",
                )
                flash(f"'{upload.filename}' is not an accepted file type. Upload one of: {ALLOWED_LABEL}.", "error")
                return redirect(url_for("upload_file.index"))

            data = upload.read()
            stored_name = f"{uuid4().hex}{suffix}"
            (upload_root() / stored_name).write_bytes(data)

            new_record = UploadedFile(
                filename=filename,
                stored_name=stored_name,
                size_bytes=len(data),
                uploaded_by_id=current_user.id,
                uploaded_by_name=current_user.name,
            )
            db.session.add(new_record)
            record_service_audit(
                service_key=FEATURE_UPLOAD,
                action="upload_file",
                request_data={"filename": filename, "size_bytes": len(data)},
                response_data={"file_id": new_record.id, "stored_name": stored_name},
                status="SUCCESS",
            )
            uploaded_filenames.append(filename)

        db.session.commit()
        
        for name in uploaded_filenames:
            if base_url:
                link = f"{base_url}/{name}"
                flash(
                    Markup(
                        f'Uploaded {html.escape(name)}. '
                        f'<a href="{html.escape(link)}" target="_blank" rel="noopener">{html.escape(link)}</a>'
                    ),
                    "success",
                )
            else:
                flash(f"Uploaded {name}.", "success")

        return redirect(url_for("upload_file.index"))

    # Newest first: the file a member just uploaded is the one they came back for.
    files = db.session.scalars(
        db.select(UploadedFile).order_by(UploadedFile.created_at.desc(), UploadedFile.id.desc())
    ).all()
    base_url = get_setting("upload_file_base_url").strip().rstrip("/")
    return render_template(
        "upload_file.html",
        files=files,
        can_delete=can_delete,
        accept=ACCEPT_ATTRIBUTE,
        allowed_label=ALLOWED_LABEL,
        folder=upload_root(),
        base_url=base_url,
    )


@bp.get("/files/<int:file_id>/download")
@upload_required
def download(file_id: int):
    """Send one stored file to the member as a download."""
    record = db.session.get(UploadedFile, file_id)
    if record is None:
        record_service_audit(
            service_key=FEATURE_UPLOAD,
            action="download_file",
            request_data={"file_id": file_id},
            response_data={"error": "File not found"},
            status="ERROR",
        )
        flash("That file is no longer in the folder.", "error")
        return redirect(url_for("upload_file.index"))

    record_service_audit(
        service_key=FEATURE_UPLOAD,
        action="download_file",
        request_data={"file_id": file_id, "filename": record.filename},
        response_data={"stored_name": record.stored_name, "size_bytes": record.size_bytes},
        status="SUCCESS",
    )

    # Served from the resolved root rather than from UPLOAD_FOLDER directly, so a
    # download still works after an administrator moves the folder.
    response = send_from_directory(
        upload_root(),
        record.stored_name,
        as_attachment=True,
        download_name=record.filename,
        max_age=0,
    )
    # Stops the browser second-guessing the content type we send and rendering an
    # uploaded .html as a page. See the module docstring.
    response.headers["X-Content-Type-Options"] = "nosniff"
    if Path(record.filename).suffix.lower() in MARKUP_SUFFIXES:
        # send_from_directory guesses text/html from the extension, which is the
        # one type where a browser would run what the member uploaded. Attachment
        # disposition already stops rendering, but declaring it as markup keeps
        # the file safe if that header is ever stripped by a proxy in between.
        response.headers["Content-Type"] = "application/octet-stream"
    return response


@bp.post("/files/<int:file_id>/delete")
@upload_required
def delete(file_id: int):
    """Remove one file from the folder.

    Limited to the member who uploaded it and to administrators. A shared folder
    where anyone may delete anything turns one mistaken click by one member into
    lost work for whoever put the file there.
    """
    record = db.session.get(UploadedFile, file_id)
    if record is None:
        record_service_audit(
            service_key=FEATURE_UPLOAD,
            action="delete_file",
            request_data={"file_id": file_id},
            response_data={"error": "File not found"},
            status="ERROR",
        )
        flash("That file is no longer in the folder.", "error")
        return redirect(url_for("upload_file.index"))

    if not can_delete(record):
        record_service_audit(
            service_key=FEATURE_UPLOAD,
            action="delete_file",
            request_data={"file_id": file_id, "filename": record.filename},
            response_data={"error": "Permission denied"},
            status="ERROR",
        )
        flash("You can only delete a file that you uploaded.", "error")
        return redirect(url_for("upload_file.index"))

    filename = record.filename
    stored_name = record.stored_name
    db.session.delete(record)
    db.session.commit()
    (upload_root() / stored_name).unlink(missing_ok=True)
    record_service_audit(
        service_key=FEATURE_UPLOAD,
        action="delete_file",
        request_data={"file_id": file_id, "filename": filename},
        response_data={"deleted": True},
        status="SUCCESS",
    )
    flash(f"Deleted {filename}.", "success")
    return redirect(url_for("upload_file.index"))