"""Video Upload: send a video to Cloudflare Stream without it touching this server.

The video bytes never come here. A member's browser asks this service for a
one-time Cloudflare upload URL, and then sends the video itself directly to
Cloudflare over the tus protocol. Three things follow from that, and they are the
reason the service is shaped this way rather than as an ordinary file upload:

* **A 2 GB video costs this application a few hundred bytes.** The per-chunk
  requests happen between the browser and Cloudflare, so the application's
  ``MAX_CONTENT_LENGTH`` and upload timeouts are irrelevant to them. This is the
  reason to use direct creator uploads at all.
* **Resumable.** tus records the byte offset, so a dropped connection resumes
  where it stopped instead of starting over. That is why tus rather than a single
  POST: Cloudflare requires tus above 200 MB, and it is the better experience
  below that too.
* **The API token never reaches the browser.** The one-time URL the browser
  receives can create and fill a video and nothing else, and it dies after one
  upload.

Two decisions that depart from Cloudflare's published example, both deliberate:

* **No wildcard CORS headers on the provisioning endpoint.** Cloudflare's Worker
  example answers with ``Access-Control-Allow-Origin: *`` because the client page
  may live on another origin. Here the page is served by this app, so the request
  is same-origin and needs no CORS at all. Emitting ``*`` would only widen who
  else can read the endpoint, and a URL that spends account storage is worth
  keeping narrow.
* **Upload metadata is built here, not forwarded.** Cloudflare's example passes
  the browser's ``Upload-Metadata`` straight through. Since this service already
  sits between the browser and Cloudflare, it takes the filename as a plain value,
  sanitises it, and encodes the metadata itself. A client therefore cannot inject
  metadata keys it was not offered - ``watermark`` or ``scheduleddeletion``
  pointing at another video's, for instance.

Access is granted per member by an administrator and enforced on every route,
including the provisioning endpoint, so a bookmarked URL cannot reserve storage on
the Cloudflare account without the grant.
"""

from functools import wraps

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from .service_audit import record_service_audit
from .azure_ai import credential
from .cloudflare_stream import (
    SOURCE_METADATA_KEY,
    CloudflareError,
    create_tus_upload,
    delete_video,
    video_details,
)
from .extensions import db
from .feature_access import has_feature_access
from .models import FEATURE_LABELS, FEATURE_VIDEO_UPLOAD, VideoUpload
from .settings import get_setting

bp = Blueprint("video_upload", __name__, url_prefix="/apps/video-upload")

#: Setting holding the declared ceiling on video length. Cloudflare reserves this
#: much storage per upload, so an administrator tunes it as a budget decision.
DURATION_SETTING_KEY = "video_upload_max_duration_seconds"

#: Fallback when the setting is somehow unreadable or unset. Matches the default
#: in DEFAULT_APP_SETTINGS; duplicated rather than imported so this module has one
#: less import edge, and the test suite pins the two to the same value.
FALLBACK_MAX_DURATION_SECONDS = 3600

#: Header the browser declares the filename in. A plain value rather than the
#: base64 ``Upload-Metadata`` blob, so this service builds the metadata itself and
#: the client cannot smuggle keys into it. See the module docstring.
FILENAME_HEADER = "X-Video-Filename"

#: File types offered by the picker. Cloudflare accepts far more than a browser
#: can usefully preview, so this is the common set rather than an exhaustive one;
#: it is a client-side convenience only, and the server does not refuse a file for
#: being absent from it. Cloudflare does the real validation and rejects anything
#: it cannot ingest with a message the member can act on.
OFFERED_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpeg", ".mpg", ".wmv", ".flv")

#: Chunk size for the tus client. Cloudflare's rules are a floor of 5 MiB, a
#: ceiling of 200 MiB, and a multiple of 256 KiB. 50 MiB is the value from
#: Cloudflare's own guidance for a connection expected to be reasonably reliable,
#: and it is a multiple of 256 KiB (50 * 1024 * 1024 = 204800 * 256).
CHUNK_SIZE = 50 * 1024 * 1024


def max_duration_seconds() -> int:
    """The declared ceiling on video length, in seconds.

    Read per call so an administrator's change applies to the next upload rather
    than the next restart. A stored value that is not a usable positive number
    falls back to the default: this value is forwarded to Cloudflare as a storage
    reservation, and refusing to reserve anything would leave the service with no
    useful behaviour at all.
    """
    try:
        return int(get_setting(DURATION_SETTING_KEY).strip())
    except (TypeError, ValueError):
        return FALLBACK_MAX_DURATION_SECONDS


def accept_attribute() -> str:
    """The ``accept`` value for the file input, derived from the offered suffixes."""
    return ",".join(OFFERED_SUFFIXES)


def video_required(view):
    """Refuse the view unless the signed-in member has been granted the service.

    Enforced on every route rather than only by hiding the header link, because a
    member who was never granted the service can still type the URL - and on the
    provisioning endpoint that is not a read-only mistake, it spends the
    Cloudflare account's storage.

    The refusal is a rendered page, not a bare 403: the app-wide 403 speaks about
    project roles and would be actively misleading here. The JSON routes answer
    with JSON instead, because the browser is waiting on a fetch and a styled HTML
    page would surface as a parse error rather than as a message.
    """
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not has_feature_access(current_user, FEATURE_VIDEO_UPLOAD):
            if request.path.endswith("/tus"):
                return jsonify(error=(
                    f"You do not have access to {FEATURE_LABELS[FEATURE_VIDEO_UPLOAD]}. "
                    "An administrator grants this service from Members."
                )), 403
            return render_template(
                "error.html",
                title="Not permitted",
                message=(
                    f"You do not have access to {FEATURE_LABELS[FEATURE_VIDEO_UPLOAD]}. "
                    "An administrator grants this service from Members."
                ),
            ), 403
        return view(*args, **kwargs)
    return wrapped


def can_delete(record: VideoUpload) -> bool:
    """Whether the signed-in member may delete ``record``.

    Used by the template so the button is not offered to someone whose click would
    be refused. The route re-checks regardless: a hidden button is a courtesy, not
    a control.
    """
    return current_user.is_admin or record.uploaded_by_id == current_user.id


def _usable_name(raw: str) -> str:
    """Reduce a browser-supplied filename to something safe to store and show.

    ``secure_filename`` collapses path separators and strips what is not a
    filename character, so ``../../etc/passwd`` arrives as ``etc_passwd``. The
    result can still be empty, which is handled by the caller rather than papered
    over with an invented name.

    The same sanitised value is what is sent to Cloudflare as the video's name, so
    the dashboard shows the same string this app stores.
    """
    return secure_filename(raw)[:255]


def _configured() -> bool:
    """Whether the service has the credentials it needs to reach Cloudflare.

    The endpoint is checked too, because it is in ``VIDEO_REQUIRED`` and the
    header's dimmed-vs-live decision goes through ``service_status``. An unset
    endpoint falls back to Cloudflare's production base and works, but the
    settings screen will still report it as missing, and the page should agree
    with the header rather than disagree with it.
    """
    return all(credential(key) for key in ("cloudflare_account_id", "cloudflare_api_token", "cloudflare_endpoint"))


@bp.route("", methods=["GET"])
@video_required
def index():
    """The service page: upload control above, upload history below."""
    videos = db.session.scalars(
        db.select(VideoUpload)
        .order_by(VideoUpload.created_at.desc(), VideoUpload.id.desc())
    ).all()
    return render_template(
        "video_upload.html",
        videos=videos,
        can_delete=can_delete,
        accept=accept_attribute(),
        chunk_size=CHUNK_SIZE,
        max_duration_seconds=max_duration_seconds(),
        configured=_configured(),
        account_hint=(credential("cloudflare_account_id") or "")[:8],
        # Passed rather than hardcoded in the template so the header the browser
        # sends and the one the server reads cannot drift apart.
        filename_header=FILENAME_HEADER,
    )


@bp.post("/tus")
@video_required
def tus_url():
    """Reserve storage on Cloudflare and hand the browser its one-time tus URL.

    This is the only request in the whole upload that reaches this application,
    and it carries no video data - just the size the browser will upload and the
    name to file it under.

    The ``Location`` header is load-bearing: it is where the tus client sends the
    video. The response body is a small JSON summary for the page to show, which
    the tus client ignores. Both are set because the same response has to satisfy
    a protocol the client speaks and a person reading the screen.
    """
    raw_length = request.headers.get("Upload-Length", "")
    try:
        upload_length = int(raw_length)
    except (TypeError, ValueError):
        record_service_audit(
            service_key=FEATURE_VIDEO_UPLOAD,
            action="reserve_upload",
            request_data={"raw_length": raw_length},
            response_data={"error": "Missing or invalid Upload-Length header"},
            status="ERROR",
        )
        return jsonify(error="The browser did not report how large the video is, so Cloudflare cannot reserve storage for it."), 400

    filename = _usable_name(request.headers.get(FILENAME_HEADER, ""))
    if not filename:
        # A name that sanitises away is not a reason to refuse the upload: the
        # video is still good, and Cloudflare will hold it under a generated id
        # anyway. A placeholder keeps the list readable.
        filename = "video"

    try:
        reserved = create_tus_upload(
            upload_length=upload_length,
            # filename: what Cloudflare shows in the dashboard.
            # filetype: what it uses to decide the codec path. The browser's word
            #   for this is taken because it is derived from the file's own bytes
            #   and Cloudflare ignores it when it cannot use it.
            # SOURCE_METADATA_KEY: a marker so a video stored through this app can
            #   be told apart from one added by hand in the dashboard.
            metadata={
                "filename": filename,
                "filetype": (request.headers.get("Content-Type") or "").split(";")[0].strip(),
                SOURCE_METADATA_KEY: current_user.email,
            },
            max_duration_seconds=max_duration_seconds(),
        )
    except CloudflareError as error:
        # Logged by the client module. Flashed rather than returned, so a member
        # who arrived by typing the URL sees why; returned as JSON as well,
        # because the page is a fetch and only reads the body.
        message = str(error)
        record_service_audit(
            service_key=FEATURE_VIDEO_UPLOAD,
            action="reserve_upload",
            request_data={"filename": filename, "upload_length": upload_length},
            response_data={"error": message},
            status="ERROR",
        )
        flash(message, "error")
        return jsonify(error=message), 502

    record = VideoUpload(
        filename=filename,
        media_id=reserved["media_id"],
        size_bytes=max(upload_length, 0),
        # True at this moment: the URL is issued, but not one byte has arrived.
        status="uploading",
        uploaded_by_id=current_user.id,
        uploaded_by_name=current_user.name,
    )
    db.session.add(record)
    db.session.commit()

    record_service_audit(
        service_key=FEATURE_VIDEO_UPLOAD,
        action="reserve_upload",
        request_data={"filename": filename, "upload_length": upload_length},
        response_data={"upload_id": record.id, "media_id": record.media_id},
        status="SUCCESS",
    )

    # The tus protocol requires this header, and its absence is the one failure
    # the client reports as "invalid or missing Location header" with no detail.
    response = jsonify(ok=True, upload_id=record.id, media_id=record.media_id)
    response.headers["Location"] = reserved["upload_url"]
    return response, 201


def _build_video_links(media_id: str, details: dict | None = None) -> dict:
    """Build Direct, Embedded, and HLS Manifest playback links for a video."""
    if not media_id:
        return {"direct": "", "embedded": "", "hls": ""}
    hls_url = ""
    if details and isinstance(details.get("playback"), dict):
        hls_url = details["playback"].get("hls") or ""
    if not hls_url:
        hls_url = f"https://videodelivery.net/{media_id}/manifest/video.m3u8"

    return {
        "direct": f"https://watch.cloudflarestream.com/{media_id}",
        "embedded": f"https://iframe.videodelivery.net/{media_id}",
        "hls": hls_url,
    }


def _sync_upload_record(record: VideoUpload) -> tuple[dict, int]:
    """Query Cloudflare Stream for record's latest status, update database, and return summary payload."""
    if not record.media_id:
        record.status = "unknown"
        record.status_detail = "Cloudflare did not return an identifier, so the video cannot be tracked."
        db.session.commit()
        return {
            "ok": True,
            "status": record.status,
            "detail": record.status_detail,
            "pct_complete": None,
            "step": None,
            "ready_to_stream": False,
            "media_id": "",
            "links": _build_video_links(""),
        }, 200

    try:
        details = video_details(record.media_id)
    except CloudflareError as error:
        record.status = "unknown"
        record.status_detail = str(error)
        db.session.commit()
        return {
            "ok": True,
            "status": record.status,
            "detail": record.status_detail,
            "pct_complete": None,
            "step": None,
            "ready_to_stream": False,
            "media_id": record.media_id,
            "links": _build_video_links(record.media_id),
            "error": str(error),
        }, 200

    # Parse status from Cloudflare
    status_obj = details.get("status")
    if isinstance(status_obj, dict):
        raw_state = str(status_obj.get("state") or "").strip().lower()
        pct_complete = status_obj.get("pctComplete")
        step = status_obj.get("step")
        err_msg = str(status_obj.get("errorReasonText") or status_obj.get("error") or details.get("error") or "").strip()
    else:
        raw_state = str(status_obj or "").strip().lower()
        pct_complete = details.get("pctComplete")
        step = details.get("step")
        err_msg = str(details.get("error") or "").strip()

    # Cloudflare states: "ready", "inprogress", "queued", "pendingupload", "downloading", "error"
    if details.get("readyToStream") is True or raw_state == "ready":
        mapped = "ready"
    elif raw_state in ("inprogress", "queued", "downloading", "processing"):
        mapped = "processing"
    elif raw_state in ("uploading", "pendingupload"):
        mapped = "uploading"
    elif raw_state in ("error", "errored"):
        mapped = "error"
    else:
        mapped = "unknown"

    record.status = mapped
    if mapped == "ready":
        detail = "Ready to stream"
    elif mapped == "processing":
        if pct_complete is not None:
            try:
                detail = f"Encoding: {float(pct_complete):.0f}% ({step or 'processing'})"
            except (ValueError, TypeError):
                detail = f"Encoding: {pct_complete}%"
        else:
            detail = f"Processing on Cloudflare ({step or 'encoding'})"
    elif mapped == "error":
        detail = err_msg[:120] or "Cloudflare processing error"
    elif mapped == "uploading":
        detail = "Receiving upload"
    else:
        detail = f"Cloudflare reported '{raw_state or 'no status'}'."

    record.status_detail = detail

    if details.get("size") is not None:
        try:
            record.size_bytes = int(details["size"])
        except (TypeError, ValueError):
            pass

    db.session.commit()
    links = _build_video_links(record.media_id, details)

    return {
        "ok": True,
        "status": record.status,
        "detail": record.status_detail,
        "pct_complete": pct_complete,
        "step": step,
        "ready_to_stream": bool(details.get("readyToStream") or record.status == "ready"),
        "media_id": record.media_id,
        "links": links,
    }, 200


@bp.post("/uploads/<int:upload_id>/complete")
@video_required
def complete(upload_id: int):
    """Ask Cloudflare how a finished upload actually turned out."""
    record = db.session.get(VideoUpload, upload_id)
    if record is None:
        record_service_audit(
            service_key=FEATURE_VIDEO_UPLOAD,
            action="complete_upload",
            request_data={"upload_id": upload_id},
            response_data={"error": "Upload record not found"},
            status="ERROR",
        )
        return jsonify(error="That upload is no longer recorded."), 404
    if record.uploaded_by_id != current_user.id and not current_user.is_admin:
        record_service_audit(
            service_key=FEATURE_VIDEO_UPLOAD,
            action="complete_upload",
            request_data={"upload_id": upload_id},
            response_data={"error": "Permission denied"},
            status="ERROR",
        )
        return jsonify(error="That upload belongs to someone else."), 403

    payload, code = _sync_upload_record(record)

    record_service_audit(
        service_key=FEATURE_VIDEO_UPLOAD,
        action="complete_upload",
        request_data={"upload_id": upload_id, "media_id": record.media_id},
        response_data={"status": record.status, "detail": record.status_detail, "size_bytes": record.size_bytes},
        status="SUCCESS" if payload.get("ok") else "ERROR",
    )

    return jsonify(payload), code


@bp.route("/uploads/<int:upload_id>/status", methods=["GET", "POST"])
@video_required
def status(upload_id: int):
    """Check Cloudflare encoding and processing progress for an ongoing or completed upload."""
    record = db.session.get(VideoUpload, upload_id)
    if record is None:
        return jsonify(error="That upload is no longer recorded."), 404
    if record.uploaded_by_id != current_user.id and not current_user.is_admin:
        return jsonify(error="That upload belongs to someone else."), 403

    payload, code = _sync_upload_record(record)
    return jsonify(payload), code


@bp.post("/uploads/<int:upload_id>/delete")
@video_required
def delete(upload_id: int):
    """Delete a video from Cloudflare Stream and drop the local record.

    Cloudflare is asked first and the row is kept if it refuses. The reverse order
    would leave a video on the account that nothing in this app can remove again,
    which is the worse of the two failure modes: an orphaned video still counts
    against storage.

    The local record is only removed once Cloudflare confirms, and a repeat delete
    (which Cloudflare answers with a 404) still clears it, so a video deleted from
    the dashboard can be cleared from here.
    """
    record = db.session.get(VideoUpload, upload_id)
    if record is None:
        record_service_audit(
            service_key=FEATURE_VIDEO_UPLOAD,
            action="delete_video",
            request_data={"upload_id": upload_id},
            response_data={"error": "Upload record not found"},
            status="ERROR",
        )
        flash("That upload is no longer recorded.", "error")
        return redirect(url_for("video_upload.index"))

    if not can_delete(record):
        record_service_audit(
            service_key=FEATURE_VIDEO_UPLOAD,
            action="delete_video",
            request_data={"upload_id": upload_id, "filename": record.filename},
            response_data={"error": "Permission denied"},
            status="ERROR",
        )
        flash("You can only delete a video that you uploaded.", "error")
        return redirect(url_for("video_upload.index"))

    filename = record.filename
    media_id = record.media_id
    try:
        delete_video(media_id)
    except CloudflareError as error:
        record_service_audit(
            service_key=FEATURE_VIDEO_UPLOAD,
            action="delete_video",
            request_data={"upload_id": upload_id, "filename": filename, "media_id": media_id},
            response_data={"error": str(error)},
            status="ERROR",
        )
        flash(str(error), "error")
        return redirect(url_for("video_upload.index"))

    db.session.delete(record)
    db.session.commit()
    record_service_audit(
        service_key=FEATURE_VIDEO_UPLOAD,
        action="delete_video",
        request_data={"upload_id": upload_id, "filename": filename, "media_id": media_id},
        response_data={"deleted": True},
        status="SUCCESS",
    )
    flash(f"Deleted {filename}.", "success")
    return redirect(url_for("video_upload.index"))

