"""Web layer for the Vision, Speech and Chat services.

Access is granted per member by an administrator and enforced here on every
route - not just hidden in the navigation - so a bookmarked or guessed URL
cannot reach a service the member has not been given. An unconfigured service
returns 503 with an explanation rather than a stack trace from an SDK.
"""

import json
import re
import time
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from .service_audit import record_service_audit

from .azure_ai import ServiceError, ServiceNotConfigured, service_status
from .chat_service import ask, chunk_size, corpus_summary, index_document
from .document_text import ExtractionError, SUPPORTED_SUFFIXES, extract_text
from .extensions import db
from .feature_access import has_feature_access
from .models import (
    FEATURE_CHAT,
    FEATURE_LABELS,
    FEATURE_SPEECH,
    FEATURE_VISION,
    ChatDocument,
    ChatDocumentChunk,
)
from .speech_service import (
    MAX_CHARACTERS,
    VOICE_GROUPS,
    VOICE_INDEX,
    audio_dir,
    default_voice,
    synthesize,
)
from .vision_service import analyze_image, load_mapping

bp = Blueprint("ai", __name__, url_prefix="/apps/ai")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"BM", b"GIF8", b"RIFF")

MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_HISTORY_CHARS = 8000
MAX_TURN_CHARS = 2000

#: A stored clip must look like "<member id>-<32 hex>.mp3"; anything else is refused.
_AUDIO_NAME = re.compile(r"^(\d+)-[0-9a-f]{32}\.mp3$")

#: feature key -> the endpoint name in this blueprint, for the bare-URL redirect.
_ROUTES = {FEATURE_VISION: "vision", FEATURE_SPEECH: "speech", FEATURE_CHAT: "chat"}


def _require(feature: str) -> None:
    """Abort unless the current member may use ``feature``."""
    if not has_feature_access(current_user, feature):
        abort(403)


def _unavailable(feature: str):
    """Render a service page that cannot run, with the reason spelled out."""
    status = service_status(feature)
    if not status["configured"]:
        flash(
            f"{FEATURE_LABELS[feature]} is not configured yet. "
            f"An administrator must set: {', '.join(status['missing'])}.",
            "error",
        )
    return render_template("ai_service_unavailable.html", title=FEATURE_LABELS[feature], service_status=status), 503


@bp.route("")
@login_required
def index():
    """Redirect the old landing URL to the first service the member can use.

    The landing page is gone - each service is a header link now - but the URL
    is in bookmarks and in the user guide, so it lands the member somewhere
    useful instead of a 404.
    """
    for feature in (FEATURE_VISION, FEATURE_SPEECH, FEATURE_CHAT):
        if has_feature_access(current_user, feature):
            return redirect(url_for(f"ai.{_ROUTES[feature]}"))
    flash("You have not been given access to any of the AI services yet.", "error")
    return redirect(url_for("main.project_list"))


# ─── Vision ─────────────────────────────────────────────────────────────────

def _looks_like_image(data: bytes) -> bool:
    return any(data.startswith(magic) for magic in IMAGE_MAGIC)


@bp.route("/vision", methods=["GET", "POST"])
@login_required
def vision():
    _require(FEATURE_VISION)
    status = service_status(FEATURE_VISION)
    if not status["configured"]:
        return _unavailable(FEATURE_VISION)

    custom_mapping_raw = request.form.get("mapping") if request.method == "POST" else None
    mapping, mapping_error = load_mapping(custom_mapping_raw)
    result = None
    upload_error = ""

    if request.method == "POST":
        upload = request.files.get("image")
        if upload is None or not upload.filename:
            flash("Choose an image to analyse.", "error")
            return redirect(url_for("ai.vision"))

        filename = secure_filename(upload.filename) or "upload.jpg"
        suffix = Path(filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            flash(f"'{suffix or filename}' is not an image. Upload a JPG, PNG, BMP, GIF or WEBP.", "error")
            return redirect(url_for("ai.vision"))

        data = upload.read()
        if len(data) > MAX_UPLOAD_BYTES:
            upload_error = "That image is larger than 12 MB."
        elif not _looks_like_image(data):
            upload_error = "That file does not look like an image."
        else:
            t0 = time.time()
            try:
                result = analyze_image(data, mapping)
                duration_ms = int((time.time() - t0) * 1000)
                record_service_audit(
                    service_key=FEATURE_VISION,
                    action="analyze_image",
                    request_data={"filename": filename, "size_bytes": len(data), "mapping": mapping},
                    response_data={
                        "caption": result.get("caption"),
                        "ocr_line_count": result.get("ocr_line_count"),
                        "fields": {k: v[0] for k, v in result.get("fields", {}).items()},
                    },
                    status="SUCCESS",
                    duration_ms=duration_ms,
                )
            except ServiceNotConfigured as exc:
                duration_ms = int((time.time() - t0) * 1000)
                record_service_audit(
                    service_key=FEATURE_VISION,
                    action="analyze_image",
                    request_data={"filename": filename, "size_bytes": len(data)},
                    response_data={"error": str(exc)},
                    status="ERROR",
                    duration_ms=duration_ms,
                )
                flash(str(exc), "error")
                return redirect(url_for("ai.vision"))
            except ServiceError as exc:
                duration_ms = int((time.time() - t0) * 1000)
                upload_error = str(exc)
                record_service_audit(
                    service_key=FEATURE_VISION,
                    action="analyze_image",
                    request_data={"filename": filename, "size_bytes": len(data)},
                    response_data={"error": upload_error},
                    status="ERROR",
                    duration_ms=duration_ms,
                )
            except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the page
                duration_ms = int((time.time() - t0) * 1000)
                current_app.logger.exception("Vision analysis failed")
                upload_error = "The image could not be analysed. Check the Vision settings and try again."
                record_service_audit(
                    service_key=FEATURE_VISION,
                    action="analyze_image",
                    request_data={"filename": filename, "size_bytes": len(data)},
                    response_data={"error": str(exc)},
                    status="ERROR",
                    duration_ms=duration_ms,
                )
            if result:
                result["filename"] = filename
                result["size_bytes"] = len(data)

    mapping_json = custom_mapping_raw if custom_mapping_raw is not None else json.dumps(mapping, indent=2)
    return render_template(
        "ai_vision.html",
        mapping=mapping,
        mapping_error=mapping_error,
        mapping_json=mapping_json,
        result=result,
        upload_error=upload_error,
    )


# ─── Speech ─────────────────────────────────────────────────────────────────

def _parse_history(raw: str) -> list[dict]:
    """Read prior conversation turns from the form, bounded in size.

    The value is the member's own conversation round-tripped through a hidden
    field so the page needs no JavaScript. It is parsed defensively and capped:
    an oversized or malformed value must not be able to blow up the request.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []

    history: list[dict] = []
    total = 0
    for turn in parsed:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        content = content[:MAX_TURN_CHARS]
        total += len(content)
        if total > MAX_HISTORY_CHARS:
            break
        history.append({"role": role, "content": content})
    return history


@bp.route("/speech", methods=["GET", "POST"])
@login_required
def speech():
    _require(FEATURE_SPEECH)
    status = service_status(FEATURE_SPEECH)
    if not status["configured"]:
        return _unavailable(FEATURE_SPEECH)

    clip = None
    spoken_text = ""
    voice = default_voice()

    if request.method == "POST":
        spoken_text = request.form.get("text", "")
        voice = (request.form.get("voice") or "").strip() or default_voice()
        t0 = time.time()
        try:
            clip = synthesize(spoken_text, current_user.id, voice)
            spoken_text = spoken_text.strip()
            duration_ms = int((time.time() - t0) * 1000)
            record_service_audit(
                service_key=FEATURE_SPEECH,
                action="synthesize",
                request_data={"text_length": len(spoken_text), "voice": voice, "text_preview": spoken_text[:100]},
                response_data={"clip": clip["name"] if isinstance(clip, dict) and "name" in clip else str(clip)},
                status="SUCCESS",
                duration_ms=duration_ms,
            )
        except ServiceNotConfigured as exc:
            duration_ms = int((time.time() - t0) * 1000)
            record_service_audit(
                service_key=FEATURE_SPEECH,
                action="synthesize",
                request_data={"text_length": len(spoken_text), "voice": voice},
                response_data={"error": str(exc)},
                status="ERROR",
                duration_ms=duration_ms,
            )
            flash(str(exc), "error")
            return redirect(url_for("ai.speech"))
        except ServiceError as exc:
            duration_ms = int((time.time() - t0) * 1000)
            record_service_audit(
                service_key=FEATURE_SPEECH,
                action="synthesize",
                request_data={"text_length": len(spoken_text), "voice": voice},
                response_data={"error": str(exc)},
                status="ERROR",
                duration_ms=duration_ms,
            )
            flash(str(exc), "error")
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the page
            duration_ms = int((time.time() - t0) * 1000)
            current_app.logger.exception("Speech synthesis failed")
            record_service_audit(
                service_key=FEATURE_SPEECH,
                action="synthesize",
                request_data={"text_length": len(spoken_text), "voice": voice},
                response_data={"error": str(exc)},
                status="ERROR",
                duration_ms=duration_ms,
            )
            flash("The audio could not be created. Check the Speech settings and try again.", "error")

    return render_template(
        "ai_speech.html",
        clip=clip,
        spoken_text=spoken_text,
        voice=voice,
        voice_groups=[
            {
                "accent": group["accent"],
                "voices": [
                    {"name": name, "display": display, "gender": gender}
                    for display, gender, name in group["voices"]
                ],
            }
            for group in VOICE_GROUPS
        ],
        voice_labels=VOICE_INDEX,
        max_characters=MAX_CHARACTERS,
    )


@bp.route("/speech/audio/<path:name>")
@login_required
def speech_audio(name: str):
    """Serve a synthesised clip, but only back to the member who made it."""
    _require(FEATURE_SPEECH)
    match = _AUDIO_NAME.match(name or "")
    if match is None or int(match.group(1)) != current_user.id:
        abort(404)
    target = audio_dir() / name
    if not target.is_file():
        abort(404)
    return send_from_directory(audio_dir(), name, mimetype="audio/mpeg", conditional=True)


# ─── Chat ───────────────────────────────────────────────────────────────────

@bp.route("/chat", methods=["GET", "POST"])
@login_required
def chat():
    _require(FEATURE_CHAT)
    status = service_status(FEATURE_CHAT)
    if not status["configured"]:
        return _unavailable(FEATURE_CHAT)

    question = ""
    answer = None
    history: list[dict] = []

    if request.method == "POST":
        question = request.form.get("question", "").strip()
        history = _parse_history(request.form.get("history", ""))
        t0 = time.time()
        try:
            answer = ask(question, history)
            duration_ms = int((time.time() - t0) * 1000)
            record_service_audit(
                service_key=FEATURE_CHAT,
                action="ask",
                request_data={"question": question, "history_turns": len(history)},
                response_data={"answer_preview": answer.get("text", "")[:200] if isinstance(answer, dict) else str(answer)[:200]},
                status="SUCCESS",
                duration_ms=duration_ms,
            )
        except ServiceNotConfigured as exc:
            duration_ms = int((time.time() - t0) * 1000)
            record_service_audit(
                service_key=FEATURE_CHAT,
                action="ask",
                request_data={"question": question, "history_turns": len(history)},
                response_data={"error": str(exc)},
                status="ERROR",
                duration_ms=duration_ms,
            )
            flash(str(exc), "error")
            return redirect(url_for("ai.chat"))
        except ServiceError as exc:
            duration_ms = int((time.time() - t0) * 1000)
            record_service_audit(
                service_key=FEATURE_CHAT,
                action="ask",
                request_data={"question": question, "history_turns": len(history)},
                response_data={"error": str(exc)},
                status="ERROR",
                duration_ms=duration_ms,
            )
            flash(str(exc), "error")
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the page
            duration_ms = int((time.time() - t0) * 1000)
            current_app.logger.exception("Chat question failed")
            record_service_audit(
                service_key=FEATURE_CHAT,
                action="ask",
                request_data={"question": question, "history_turns": len(history)},
                response_data={"error": str(exc)},
                status="ERROR",
                duration_ms=duration_ms,
            )
            flash("The question could not be answered. Check the OpenAI settings and try again.", "error")

    return render_template(
        "ai_chat.html",
        question=question,
        answer=answer,
        history=history,
        documents=ChatDocument.query.order_by(ChatDocument.created_at.desc()).all(),
        summary=corpus_summary(),
        accepted=",".join(sorted(SUPPORTED_SUFFIXES)),
    )


@bp.route("/chat/documents", methods=["POST"])
@login_required
def chat_document_upload():
    _require(FEATURE_CHAT)
    upload = request.files.get("document")
    if upload is None or not upload.filename:
        flash("Choose a document to upload.", "error")
        return redirect(url_for("ai.chat"))

    filename = secure_filename(upload.filename) or "document"
    data = upload.read()
    if len(data) > MAX_UPLOAD_BYTES:
        record_service_audit(
            service_key=FEATURE_CHAT,
            action="upload_document",
            request_data={"filename": filename, "size_bytes": len(data)},
            response_data={"error": "File larger than 12 MB"},
            status="ERROR",
        )
        flash(f"'{filename}' is larger than 12 MB.", "error")
        return redirect(url_for("ai.chat"))

    try:
        extracted = extract_text(filename, data)
    except ExtractionError as exc:
        record_service_audit(
            service_key=FEATURE_CHAT,
            action="upload_document",
            request_data={"filename": filename, "size_bytes": len(data)},
            response_data={"error": str(exc)},
            status="ERROR",
        )
        flash(str(exc), "error")
        return redirect(url_for("ai.chat"))
    except Exception as exc:  # noqa: BLE001 - a malformed file must not 500
        current_app.logger.exception("Document extraction failed for %s", filename)
        record_service_audit(
            service_key=FEATURE_CHAT,
            action="upload_document",
            request_data={"filename": filename, "size_bytes": len(data)},
            response_data={"error": str(exc)},
            status="ERROR",
        )
        flash(f"'{filename}' could not be read.", "error")
        return redirect(url_for("ai.chat"))

    from hashlib import sha256

    digest = sha256(data).hexdigest()
    existing = ChatDocument.query.filter_by(content_hash=digest).first()
    if existing is not None:
        record_service_audit(
            service_key=FEATURE_CHAT,
            action="upload_document",
            request_data={"filename": filename, "size_bytes": len(data)},
            response_data={"message": "Duplicate document"},
            status="SKIPPED",
        )
        flash(f"'{filename}' is already in the library. Nothing was added twice.", "error")
        return redirect(url_for("ai.chat"))

    chunks = index_document(extracted["text"])
    if not chunks:
        record_service_audit(
            service_key=FEATURE_CHAT,
            action="upload_document",
            request_data={"filename": filename, "size_bytes": len(data)},
            response_data={"error": "No searchable text found"},
            status="ERROR",
        )
        flash(f"No searchable text was found in '{filename}'.", "error")
        return redirect(url_for("ai.chat"))

    stored_name = f"chat-{digest[:16]}{Path(filename).suffix.lower()}"
    upload_root = Path(current_app.config["UPLOAD_FOLDER"])
    upload_root.mkdir(parents=True, exist_ok=True)
    (upload_root / stored_name).write_bytes(data)

    document = ChatDocument(
        filename=filename,
        stored_name=stored_name,
        content_hash=digest,
        size_bytes=len(data),
        chunk_count=len(chunks),
        text_length=len(extracted["text"]),
        uploaded_by_id=current_user.id,
        uploaded_by_name=current_user.name,
    )
    db.session.add(document)
    db.session.flush()
    for chunk in chunks:
        db.session.add(
            ChatDocumentChunk(
                document_id=document.id,
                ordinal=chunk["ordinal"],
                text=chunk["text"],
                heading=chunk["heading"],
            )
        )
    db.session.commit()

    record_service_audit(
        service_key=FEATURE_CHAT,
        action="upload_document",
        request_data={"filename": filename, "size_bytes": len(data)},
        response_data={"document_id": document.id, "chunk_count": len(chunks), "text_length": len(extracted["text"])},
        status="SUCCESS",
    )

    note = " Text was recovered approximately; PDF or DOCX extracts more reliably." if extracted["approximate"] else ""
    sections = f"{len(chunks):,} searchable section{'s' if len(chunks) != 1 else ''}"
    flash(
        f"Added '{filename}' - {sections} from {len(extracted['text']):,} characters.{note}",
        "success",
    )
    return redirect(url_for("ai.chat"))


@bp.route("/chat/documents/<int:document_id>/delete", methods=["POST"])
@login_required
def chat_document_delete(document_id: int):
    _require(FEATURE_CHAT)
    document = db.session.get(ChatDocument, document_id)
    if document is None:
        abort(404)
    filename = document.filename
    name = document.stored_name
    db.session.delete(document)
    db.session.commit()
    try:
        (Path(current_app.config["UPLOAD_FOLDER"]) / name).unlink(missing_ok=True)
    except OSError:
        pass
    record_service_audit(
        service_key=FEATURE_CHAT,
        action="delete_document",
        request_data={"document_id": document_id, "filename": filename},
        response_data={"deleted": True},
        status="SUCCESS",
    )
    flash(f"Removed '{filename}'.", "success")
    return redirect(url_for("ai.chat"))

