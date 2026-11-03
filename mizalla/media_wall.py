"""Media Walls and Totems Service.

Digital signage management for physical media walls, kiosks, and display totems.
Supports scheduling, priority reordering, public JSON API, and full-screen signage renderers.
"""

from datetime import date, datetime, timezone
from functools import wraps
import json
import logging
import os
from pathlib import Path
import random
import shutil
import struct
from uuid import uuid4

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required
from PIL import Image
from werkzeug.utils import secure_filename

from .extensions import db
from .feature_access import has_feature_access
from .models import FEATURE_MEDIA_WALL, MediaWallItem
from .service_audit import record_service_audit
from .settings import get_setting, save_setting

logger = logging.getLogger("mizalla.media_wall")

bp = Blueprint("media_wall", __name__, url_prefix="/apps/media-walls")

ALLOWED_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
ALLOWED_VIDEO_EXTS = frozenset({".mp4"})
ALLOWED_EXTS = ALLOWED_IMAGE_EXTS | ALLOWED_VIDEO_EXTS


def get_media_wall_storage_dir() -> Path:
    """Return the absolute Path to the media wall storage directory."""
    configured = get_setting("media_wall_folder", "").strip()
    if configured:
        p = Path(configured)
        if not p.is_absolute():
            p = Path(current_app.instance_path) / p
    else:
        p = Path(current_app.instance_path) / "media_walls"

    p.mkdir(parents=True, exist_ok=True)
    get_media_wall_subdirs(p)
    return p


def get_media_wall_subdirs(storage_dir: Path | None = None) -> dict[str, Path]:
    """Ensure and return structured subdirectories for Media Walls and Totems."""
    if storage_dir is None:
        storage_dir = get_media_wall_storage_dir()
    subdirs = {
        "wall_images": storage_dir / "media-wall" / "images",
        "wall_videos": storage_dir / "media-wall" / "videos",
        "totem_images": storage_dir / "totems" / "images",
        "totem_videos": storage_dir / "totems" / "videos",
    }
    for sub in subdirs.values():
        sub.mkdir(parents=True, exist_ok=True)
    return subdirs


def get_media_dimensions(file_path: Path, media_type: str) -> tuple[int | None, int | None]:
    """Extract width and height in pixels from an image or video file."""
    if media_type == "image":
        try:
            with Image.open(file_path) as img:
                return int(img.width), int(img.height)
        except Exception as exc:
            logger.warning("Could not extract image dimensions from %s: %s", file_path, exc)
            return None, None

    if media_type == "video":
        try:
            with open(file_path, "rb") as stream:
                return _parse_mp4_dimensions(stream)
        except Exception as exc:
            logger.warning("Could not extract MP4 dimensions from %s: %s", file_path, exc)
            return None, None

    return None, None


def _parse_mp4_dimensions(stream) -> tuple[int | None, int | None]:
    """Parse video width and height from MP4 ISO-BMFF box structure without external dependencies."""
    stream.seek(0, 2)
    file_length = stream.tell()
    stream.seek(0)

    def find_box(parent_end, target_type):
        while stream.tell() < parent_end:
            box_start = stream.tell()
            header = stream.read(8)
            if len(header) < 8:
                break
            size, btype = struct.unpack(">I4s", header)
            box_type = btype.decode("latin1", errors="replace")
            if size == 1:
                extended = stream.read(8)
                if len(extended) < 8:
                    break
                size = struct.unpack(">Q", extended)[0]
            elif size == 0:
                size = parent_end - box_start

            if size < 8:
                break
            box_end = box_start + size
            if box_type == target_type:
                return box_start, box_end
            stream.seek(box_end)
        return None, None

    moov_start, moov_end = find_box(file_length, "moov")
    if moov_start is None:
        return None, None

    stream.seek(moov_start + 8)
    while stream.tell() < moov_end:
        trak_start, trak_end = find_box(moov_end, "trak")
        if trak_start is None:
            break
        stream.seek(trak_start + 8)
        tkhd_start, tkhd_end = find_box(trak_end, "tkhd")
        if tkhd_start is not None:
            stream.seek(tkhd_start + 8)
            raw_vf = stream.read(4)
            if len(raw_vf) == 4:
                version_flags = struct.unpack(">I", raw_vf)[0]
                version = version_flags >> 24
                # Skip creation, modification, track_id, reserved, duration
                skip_bytes = 32 if version == 1 else 20
                stream.seek(stream.tell() + skip_bytes)
                # Skip reserved(8) + layer(2) + alt_group(2) + volume(2) + reserved(2) + matrix(36) = 52 bytes
                stream.seek(stream.tell() + 52)
                wh_data = stream.read(8)
                if len(wh_data) == 8:
                    w, h = struct.unpack(">II", wh_data)
                    width = w >> 16
                    height = h >> 16
                    if width > 0 and height > 0:
                        return int(width), int(height)
        stream.seek(trak_end)

    return None, None


def media_wall_required(f):
    """Decorator ensuring member has access to the Media Walls feature."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.url))
        if not has_feature_access(current_user, FEATURE_MEDIA_WALL):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def get_public_media_url(filename: str) -> str:
    """Return the absolute or public web URL for a media wall filename."""
    base_url = get_setting("media_wall_base_url", "").strip()
    if base_url:
        return base_url.rstrip("/") + "/" + filename
    return url_for("media_wall.serve_content", filename=filename, _external=True)


# ─── Management Dashboard Routes ─────────────────────────────────────────────

@bp.route("")
@media_wall_required
def index():
    """Management dashboard with separate tabs for Totem Images, Totem Videos, Media Wall Images, and Media Wall Videos."""
    images = (
        MediaWallItem.query.filter_by(media_type="image")
        .order_by(MediaWallItem.priority.asc(), MediaWallItem.id.asc())
        .all()
    )
    videos = (
        MediaWallItem.query.filter_by(media_type="video")
        .order_by(MediaWallItem.priority.asc(), MediaWallItem.id.asc())
        .all()
    )

    totem_images = [i for i in images if i.target_totem]
    totem_videos = [v for v in videos if v.target_totem]
    wall_images = [i for i in images if i.target_wall]
    wall_videos = [v for v in videos if v.target_wall]

    base_url = get_setting("media_wall_base_url", "")
    folder = get_setting("media_wall_folder", "") or str(get_media_wall_storage_dir())

    active_tab = request.args.get("tab", "totem-images").strip().lower()
    valid_tabs = {"totem-images", "totem-videos", "wall-images", "wall-videos"}
    if active_tab not in valid_tabs:
        if active_tab == "images":
            active_tab = "totem-images"
        elif active_tab == "videos":
            active_tab = "totem-videos"
        else:
            active_tab = "totem-images"

    today_str = date.today().isoformat()

    return render_template(
        "media_wall.html",
        totem_images=totem_images,
        totem_videos=totem_videos,
        wall_images=wall_images,
        wall_videos=wall_videos,
        images=images,
        videos=videos,
        base_url=base_url,
        folder=folder,
        storage_folder=folder,
        active_tab=active_tab,
        today_str=today_str,
    )


@bp.post("/upload")
@media_wall_required
def upload():
    """Upload a new image or video asset."""
    current_tab = request.form.get("current_tab", "").strip()
    file = request.files.get("file") or request.files.get("media_file")
    if not file or not file.filename:
        flash("Please select a file to upload.", "error")
        return redirect(url_for("media_wall.index", tab=current_tab or "totem-images") + (f"#{current_tab}" if current_tab else ""))

    friendly_name = request.form.get("friendly_name", "").strip()
    if not friendly_name:
        friendly_name = Path(file.filename).stem

    try:
        priority = int(request.form.get("priority", "10"))
    except ValueError:
        priority = 10

    start_date_str = request.form.get("start_date", "").strip()
    end_date_str = request.form.get("end_date", "").strip()

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date() if start_date_str else date.today()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date() if end_date_str else date(2099, 12, 31)
    except ValueError:
        flash("Invalid start or end date format. Use YYYY-MM-DD.", "error")
        return redirect(url_for("media_wall.index", tab=current_tab or "totem-images") + (f"#{current_tab}" if current_tab else ""))

    if start_date > end_date:
        flash("Start date cannot be after end date.", "error")
        return redirect(url_for("media_wall.index", tab=current_tab or "totem-images") + (f"#{current_tab}" if current_tab else ""))

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        flash(f"Unsupported file format '{ext}'. Allowed formats: {', '.join(sorted(ALLOWED_EXTS))}", "error")
        return redirect(url_for("media_wall.index", tab=current_tab or "totem-images") + (f"#{current_tab}" if current_tab else ""))

    media_type = "video" if ext in ALLOWED_VIDEO_EXTS else "image"

    storage_dir = get_media_wall_storage_dir()
    subdirs = get_media_wall_subdirs(storage_dir)
    unique_token = uuid4().hex[:12]
    clean_stem = secure_filename(Path(file.filename).stem)[:40] or "media"
    stored_filename = f"mw_{media_type}_{clean_stem}_{unique_token}{ext}"
    dest_path = storage_dir / stored_filename

    target_wall = request.form.get("target_wall") in ("true", "1", "on", "yes")
    target_totem = request.form.get("target_totem") in ("true", "1", "on", "yes")
    if not target_wall and not target_totem:
        target_wall = True
        target_totem = True

    if not current_tab:
        if media_type == "video":
            current_tab = "totem-videos" if target_totem else "wall-videos"
        else:
            current_tab = "totem-images" if target_totem else "wall-images"

    try:
        file.save(dest_path)
        # Mirror asset into target structured subdirectories
        if media_type == "video":
            if target_wall:
                shutil.copy2(dest_path, subdirs["wall_videos"] / stored_filename)
            if target_totem:
                shutil.copy2(dest_path, subdirs["totem_videos"] / stored_filename)
        else:
            if target_wall:
                shutil.copy2(dest_path, subdirs["wall_images"] / stored_filename)
            if target_totem:
                shutil.copy2(dest_path, subdirs["totem_images"] / stored_filename)
    except Exception as exc:
        logger.error("Failed to save media wall upload %s: %s", stored_filename, exc)
        flash(f"Failed to save file: {exc}", "error")
        return redirect(url_for("media_wall.index", tab=current_tab) + f"#{current_tab}")

    file_size_bytes = dest_path.stat().st_size
    width, height = get_media_dimensions(dest_path, media_type)

    if target_totem and media_type == "video":
        primary_rel = f"totems/videos/{stored_filename}"
    elif target_wall and media_type == "video":
        primary_rel = f"media-wall/videos/{stored_filename}"
    elif target_totem:
        primary_rel = f"totems/images/{stored_filename}"
    else:
        primary_rel = f"media-wall/images/{stored_filename}"

    item = MediaWallItem(
        friendly_name=friendly_name,
        media_type=media_type,
        filename=stored_filename,
        original_filename=file.filename,
        relative_path=primary_rel,
        priority=priority,
        start_date=start_date,
        end_date=end_date,
        file_size_bytes=file_size_bytes,
        width=width,
        height=height,
        is_enabled=True,
        target_wall=target_wall,
        target_totem=target_totem,
        uploaded_by=current_user,
        uploaded_by_name=current_user.name,
    )
    db.session.add(item)
    db.session.commit()

    record_service_audit(
        service_key=FEATURE_MEDIA_WALL,
        action="upload",
        request_data={"filename": file.filename, "media_type": media_type, "friendly_name": friendly_name, "target_wall": target_wall, "target_totem": target_totem},
        response_data={"id": item.id, "stored_filename": stored_filename, "size_mb": item.size_mb},
        status="SUCCESS",
    )

    flash(f"Successfully uploaded {media_type} '{friendly_name}'.", "success")
    return redirect(url_for("media_wall.index", tab=current_tab) + f"#{current_tab}")


@bp.post("/edit")
@bp.post("/<int:item_id>/edit")
@media_wall_required
def edit(item_id: int | None = None):
    """Update start_date, end_date, and display targets for a media item."""
    if item_id is None:
        raw_id = request.form.get("item_id")
        if not raw_id:
            abort(400)
        try:
            item_id = int(raw_id)
        except ValueError:
            abort(400)
    item = db.get_or_404(MediaWallItem, item_id)
    current_tab = request.form.get("current_tab") or request.args.get("tab") or ("totem-videos" if item.media_type == "video" else "totem-images")

    start_date_str = request.form.get("start_date", "").strip()
    end_date_str = request.form.get("end_date", "").strip()

    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        flash("Invalid start or end date format.", "error")
        return redirect(url_for("media_wall.index", tab=current_tab) + f"#{current_tab}")

    if start_date > end_date:
        flash("Start date cannot be after end date.", "error")
        return redirect(url_for("media_wall.index", tab=current_tab) + f"#{current_tab}")

    target_wall = request.form.get("target_wall") in ("true", "1", "on", "yes")
    target_totem = request.form.get("target_totem") in ("true", "1", "on", "yes")
    if not target_wall and not target_totem:
        target_wall = True
        target_totem = True

    item.start_date = start_date
    item.end_date = end_date
    item.target_wall = target_wall
    item.target_totem = target_totem
    db.session.commit()

    record_service_audit(
        service_key=FEATURE_MEDIA_WALL,
        action="edit_dates",
        request_data={"item_id": item.id, "start_date": str(start_date), "end_date": str(end_date), "target_wall": target_wall, "target_totem": target_totem},
        response_data={"status": "updated"},
        status="SUCCESS",
    )

    flash(f"Updated schedule for '{item.friendly_name}'.", "success")
    return redirect(url_for("media_wall.index", tab=current_tab) + f"#{current_tab}")


@bp.post("/<int:item_id>/toggle")
@media_wall_required
def toggle(item_id: int):
    """Toggle enabled status of a media item."""
    item = db.get_or_404(MediaWallItem, item_id)
    current_tab = request.form.get("current_tab") or request.args.get("tab") or ("totem-videos" if item.media_type == "video" else "totem-images")
    item.is_enabled = not item.is_enabled
    db.session.commit()

    record_service_audit(
        service_key=FEATURE_MEDIA_WALL,
        action="toggle_status",
        request_data={"item_id": item.id, "is_enabled": item.is_enabled},
        response_data={"is_enabled": item.is_enabled},
        status="SUCCESS",
    )

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "status": "success",
            "is_enabled": item.is_enabled,
            "status_label": item.status_label,
            "is_active": item.is_active,
        })

    state_str = "enabled" if item.is_enabled else "disabled"
    flash(f"Media '{item.friendly_name}' is now {state_str}.", "success")
    return redirect(url_for("media_wall.index", tab=current_tab) + f"#{current_tab}")


@bp.post("/<int:item_id>/delete")
@media_wall_required
def delete(item_id: int):
    """Delete a media item from the database and remove file from disk."""
    item = db.get_or_404(MediaWallItem, item_id)
    media_type = item.media_type
    name = item.friendly_name
    current_tab = request.form.get("current_tab") or request.args.get("tab") or ("totem-videos" if item.media_type == "video" else "totem-images")

    storage_dir = get_media_wall_storage_dir()
    candidates = [
        storage_dir / item.filename,
        storage_dir / item.relative_path if item.relative_path else None,
        storage_dir / "media-wall" / "images" / item.filename,
        storage_dir / "media-wall" / "videos" / item.filename,
        storage_dir / "totems" / "images" / item.filename,
        storage_dir / "totems" / "videos" / item.filename,
        storage_dir / "images" / item.filename,
        storage_dir / "videos" / item.filename,
    ]
    for file_path in candidates:
        if file_path and file_path.is_file():
            try:
                file_path.unlink()
            except OSError as exc:
                logger.warning("Could not unlink media wall file %s: %s", file_path, exc)

    db.session.delete(item)
    db.session.commit()

    record_service_audit(
        service_key=FEATURE_MEDIA_WALL,
        action="delete",
        request_data={"item_id": item_id, "name": name},
        response_data={"status": "deleted"},
        status="SUCCESS",
    )

    flash(f"Deleted {media_type} '{name}'.", "success")
    return redirect(url_for("media_wall.index", tab=current_tab) + f"#{current_tab}")


@bp.post("/order")
@media_wall_required
def update_order():
    """Save the default display ordering for image or video list."""
    raw_media_type = (request.form.get("media_type") or "image").strip().lower()
    media_type = "video" if "video" in raw_media_type else "image"
    order_val = (request.form.get("order") or request.form.get("order_by") or "priority").strip().lower()

    if order_val in ("randomized", "random"):
        order_val = "random"
    elif order_val not in ("priority", "start_date", "end_date"):
        order_val = "priority"

    if media_type == "video":
        save_setting("media_wall_video_order", order_val)
        tab_name = "videos"
    else:
        save_setting("media_wall_image_order", order_val)
        tab_name = "images"

    flash(f"Saved {media_type} display order preference ({order_val}).", "success")
    return redirect(url_for("media_wall.index", tab=tab_name))


@bp.post("/reorder")
@media_wall_required
def reorder():
    """Update priority order from drag-and-drop table rows."""
    data = request.get_json(silent=True) or request.form
    item_ids = data.get("item_ids")
    if not item_ids and "item_ids[]" in data:
        item_ids = data.getlist("item_ids[]")

    if not isinstance(item_ids, list):
        return jsonify({"error": "item_ids list required", "ok": False}), 400

    clean_ids = []
    for raw in item_ids:
        try:
            clean_ids.append(int(raw))
        except (ValueError, TypeError):
            pass

    if not clean_ids:
        return jsonify({"error": "No valid IDs provided", "ok": False}), 400

    items = MediaWallItem.query.filter(MediaWallItem.id.in_(clean_ids)).all()
    item_map = {item.id: item for item in items}

    for idx, item_id in enumerate(clean_ids, start=1):
        if item_id in item_map:
            item_map[item_id].priority = idx

    db.session.commit()
    return jsonify({"status": "success", "ok": True, "count": len(clean_ids)})


# ─── Content Serving ────────────────────────────────────────────────────────

@bp.route("/content/<path:filename>")
def serve_content(filename: str):
    """Safely stream or serve media wall images and videos with byte range support."""
    storage_dir = get_media_wall_storage_dir()
    clean_name = secure_filename(Path(filename).name)
    sub_paths = [
        storage_dir / "media-wall" / "images",
        storage_dir / "media-wall" / "videos",
        storage_dir / "totems" / "images",
        storage_dir / "totems" / "videos",
        storage_dir,
        storage_dir / "images",
        storage_dir / "videos",
    ]
    for directory in sub_paths:
        candidate = directory / clean_name
        if candidate.is_file():
            return send_from_directory(str(directory), clean_name, as_attachment=False)

    abort(404)


# ─── Public JSON API ────────────────────────────────────────────────────────

def _build_api_response(
    media_type: str,
    requested_order: str | None = None,
    requested_target: str | None = None,
) -> dict:
    """Core builder for JSON API responses for images or videos."""
    today = date.today()

    query = MediaWallItem.query.filter(
        MediaWallItem.media_type == media_type,
        MediaWallItem.is_enabled.is_(True),
        MediaWallItem.start_date <= today,
        MediaWallItem.end_date >= today,
    )

    clean_target = (requested_target or "").lower().strip()
    if clean_target in ("wall", "media_wall"):
        query = query.filter(MediaWallItem.target_wall.is_(True))
    elif clean_target == "totem":
        query = query.filter(MediaWallItem.target_totem.is_(True))

    setting_key = "media_wall_video_order" if media_type == "video" else "media_wall_image_order"
    configured_order = get_setting(setting_key, "priority")
    effective_order = (requested_order or configured_order).lower()

    if effective_order == "start_date":
        items = query.order_by(MediaWallItem.start_date.asc(), MediaWallItem.id.asc()).all()
    elif effective_order == "end_date":
        items = query.order_by(MediaWallItem.end_date.asc(), MediaWallItem.id.asc()).all()
    elif effective_order == "random":
        items = query.all()
        random.shuffle(items)
    else:
        # Default priority
        effective_order = "priority"
        items = query.order_by(MediaWallItem.priority.asc(), MediaWallItem.id.asc()).all()

    storage_dir = get_media_wall_storage_dir()
    configured_base = get_setting("media_wall_base_url", "").strip()

    serialized = []
    for item in items:
        file_path = storage_dir / item.filename
        full_url = get_public_media_url(item.filename)

        serialized.append({
            "id": item.id,
            "name": item.friendly_name,
            "friendly_name": item.friendly_name,
            "filename": item.filename,
            "filepath": str(file_path).replace("\\", "/"),
            "url": full_url,
            "priority": item.priority,
            "target_wall": item.target_wall,
            "target_totem": item.target_totem,
            "targets": item.targets_list,
            "start_date": item.start_date.isoformat(),
            "end_date": item.end_date.isoformat(),
            "size_mb": item.size_mb,
            "width": item.width,
            "height": item.height,
        })

    slide_duration = int(get_setting("media_wall_slide_duration", "10") or 10)

    return {
        "type": f"{media_type}s",
        "target": clean_target or "all",
        "order": effective_order,
        "base_url": configured_base or url_for("media_wall.serve_content", filename="", _external=True),
        "slide_interval_seconds": slide_duration,
        "count": len(serialized),
        "items": serialized,
    }


@bp.route("/api/get")
def api_get():
    """JSON API endpoint accessible via /apps/media-walls/api/get."""
    raw_type = request.args.get("type", "images").lower().strip()
    media_type = "video" if "video" in raw_type else "image"
    requested_order = request.args.get("order")
    requested_target = request.args.get("target") or request.args.get("display")
    return jsonify(_build_api_response(media_type, requested_order, requested_target))


# ─── Digital Signage Display Renderers ──────────────────────────────────────

@bp.route("/display/images")
def display_images():
    """Full-screen image carousel adapted from scratch/index.php and scratch/index_totem.php."""
    today = date.today()
    target = (request.args.get("target") or request.args.get("mode") or request.args.get("display") or "all").lower().strip()
    order_type = request.args.get("order") or get_setting("media_wall_image_order", "priority")
    query = MediaWallItem.query.filter(
        MediaWallItem.media_type == "image",
        MediaWallItem.is_enabled.is_(True),
        MediaWallItem.start_date <= today,
        MediaWallItem.end_date >= today,
    )

    if target in ("wall", "media_wall"):
        query = query.filter(MediaWallItem.target_wall.is_(True))
    elif target == "totem":
        query = query.filter(MediaWallItem.target_totem.is_(True))

    if order_type == "start_date":
        items = query.order_by(MediaWallItem.start_date.asc(), MediaWallItem.id.asc()).all()
    elif order_type == "end_date":
        items = query.order_by(MediaWallItem.end_date.asc(), MediaWallItem.id.asc()).all()
    elif order_type == "random":
        items = query.all()
        random.shuffle(items)
    else:
        items = query.order_by(MediaWallItem.priority.asc(), MediaWallItem.id.asc()).all()

    default_interval = 12 if target == "totem" else 10
    slide_duration = int(get_setting("media_wall_slide_duration", str(default_interval)) or default_interval)

    return render_template(
        "media_wall_display_images.html",
        items=items,
        target=target,
        slide_interval=slide_duration * 1000,
        get_public_media_url=get_public_media_url,
    )


@bp.route("/display/videos")
def display_videos():
    """Full-screen looping video player adapted from scratch/index-video.php."""
    today = date.today()
    target = (request.args.get("target") or request.args.get("mode") or request.args.get("display") or "all").lower().strip()
    order_type = request.args.get("order") or get_setting("media_wall_video_order", "priority")
    query = MediaWallItem.query.filter(
        MediaWallItem.media_type == "video",
        MediaWallItem.is_enabled.is_(True),
        MediaWallItem.start_date <= today,
        MediaWallItem.end_date >= today,
    )

    if target in ("wall", "media_wall"):
        query = query.filter(MediaWallItem.target_wall.is_(True))
    elif target == "totem":
        query = query.filter(MediaWallItem.target_totem.is_(True))

    if order_type == "start_date":
        items = query.order_by(MediaWallItem.start_date.asc(), MediaWallItem.id.asc()).all()
    elif order_type == "end_date":
        items = query.order_by(MediaWallItem.end_date.asc(), MediaWallItem.id.asc()).all()
    elif order_type == "random":
        items = query.all()
        random.shuffle(items)
    else:
        items = query.order_by(MediaWallItem.priority.asc(), MediaWallItem.id.asc()).all()

    video_urls = [get_public_media_url(item.filename) for item in items]
    return render_template(
        "media_wall_display_videos.html",
        items=items,
        target=target,
        video_urls_str=";".join(video_urls),
        video_urls=video_urls,
    )
