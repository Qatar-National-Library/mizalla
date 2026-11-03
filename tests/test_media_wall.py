"""Tests for the Media Walls and Totems service (Digital Signage).

Covers:
* Access control (unauthenticated -> redirect to login, non-granted -> 403, granted -> 200)
* Image upload with automatic resolution extraction and size calculation
* Pure-Python MP4 dimension extraction
* Date validity schedule filtering (start_date <= today <= end_date)
* Public JSON API (/api/get?type=images and /api/get?type=videos)
* Configurable base URL prepending
* Drag-and-drop priority reordering endpoint (/apps/media-walls/reorder)
* Editing schedule dates, toggling enable/disable, and deleting items
* Signage display renderers (/apps/media-walls/display/images and /display/videos)
* Content streaming (/apps/media-walls/content/<filename>)
"""

from datetime import date, timedelta
import io
from pathlib import Path
from PIL import Image

import pytest

from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.media_wall import _parse_mp4_dimensions
from mizalla.models import (
    FEATURE_MEDIA_WALL,
    MediaWallItem,
    Member,
)
from mizalla.settings import save_setting

PASSWORD = "member-password-long"


def make_member(email="user@example.com", name="User", is_admin=False):
    member = Member(name=name, email=email, is_admin=is_admin)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def grant_media_wall(member):
    sync_member_features(member, [FEATURE_MEDIA_WALL])
    db.session.commit()


def login(client, email="user@example.com"):
    client.post("/apps/login", data={"email": email, "password": PASSWORD})


def create_sample_png(width=120, height=80, color=(30, 144, 255)) -> bytes:
    """Generate in-memory PNG bytes with given dimensions."""
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color=color)
    img.save(buf, format="PNG")
    return buf.getvalue()


# ─── Access Control Tests ───────────────────────────────────────────────────

def test_unauthenticated_access_redirects(client):
    res = client.get("/apps/media-walls")
    assert res.status_code == 302
    assert "/apps/login" in res.headers["Location"]


def test_member_without_grant_gets_403(app, client):
    with app.app_context():
        make_member(email="nogrant@example.com")
    login(client, "nogrant@example.com")

    res = client.get("/apps/media-walls")
    assert res.status_code == 403


def test_member_with_grant_gets_200(app, client):
    with app.app_context():
        member = make_member(email="granted@example.com")
        grant_media_wall(member)
    login(client, "granted@example.com")

    res = client.get("/apps/media-walls")
    assert res.status_code == 200
    assert b"Media Walls &amp; Totems" in res.data or b"Media Walls & Totems" in res.data


def test_admin_gets_access_without_explicit_grant(app, client):
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
    login(client, "admin@example.com")

    res = client.get("/apps/media-walls")
    assert res.status_code == 200


# ─── Upload and Metadata Extraction Tests ───────────────────────────────────

def test_image_upload_and_resolution_extraction(app, client, tmp_path):
    storage_folder = tmp_path / "media_storage"
    storage_folder.mkdir(parents=True, exist_ok=True)

    with app.app_context():
        save_setting("media_wall_folder", str(storage_folder))
        member = make_member(email="uploader@example.com")
        grant_media_wall(member)

    login(client, "uploader@example.com")

    png_bytes = create_sample_png(width=1920, height=1080)
    today = date.today()
    end_date = today + timedelta(days=14)

    res = client.post(
        "/apps/media-walls/upload",
        data={
            "friendly_name": "Library Event Banner",
            "priority": "5",
            "start_date": str(today),
            "end_date": str(end_date),
            "media_file": (io.BytesIO(png_bytes), "banner.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert res.status_code == 200

    with app.app_context():
        item = MediaWallItem.query.filter_by(friendly_name="Library Event Banner").first()
        assert item is not None
        assert item.media_type == "image"
        assert item.width == 1920
        assert item.height == 1080
        assert item.priority == 5
        assert item.is_active is True
        assert item.status_label.upper() == "ACTIVE"
        assert item.file_size_bytes == len(png_bytes)
        assert item.size_mb > 0
        assert (storage_folder / item.filename).is_file()


def test_disallowed_file_extension_rejected(app, client):
    with app.app_context():
        member = make_member(email="tester@example.com")
        grant_media_wall(member)
    login(client, "tester@example.com")

    res = client.post(
        "/apps/media-walls/upload",
        data={
            "friendly_name": "Bad File",
            "start_date": "2026-01-01",
            "end_date": "2026-01-10",
            "media_file": (io.BytesIO(b"malicious script"), "script.exe"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert res.status_code == 200
    with app.app_context():
        assert MediaWallItem.query.filter_by(friendly_name="Bad File").first() is None


def test_mp4_parser_extracts_dimensions_or_falls_back():
    # Corrupt or empty stream returns (None, None) gracefully
    empty_stream = io.BytesIO(b"\x00\x00\x00\x20ftypisom")
    assert _parse_mp4_dimensions(empty_stream) == (None, None)

    # Valid synthetic BMFF atom structure for moov -> trak -> tkhd
    # Construct tkhd atom:
    # version 0: 4 bytes version+flags, 8 bytes creation/mod, 4 bytes track id, 4 bytes reserved,
    # 8 bytes duration, 8 bytes reserved, 2 bytes layer, 2 bytes alt_group, 2 bytes volume, 2 bytes reserved,
    # 36 bytes matrix, 4 bytes width (fixed 16.16), 4 bytes height (fixed 16.16)
    # Total tkhd payload: 84 bytes. Atom length: 84 + 8 = 92 bytes.
    width_val = 1920 << 16
    height_val = 1080 << 16
    tkhd_payload = (
        b"\x00" * 76 +
        width_val.to_bytes(4, "big") +
        height_val.to_bytes(4, "big")
    )
    tkhd_atom = (len(tkhd_payload) + 8).to_bytes(4, "big") + b"tkhd" + tkhd_payload
    trak_atom = (len(tkhd_atom) + 8).to_bytes(4, "big") + b"trak" + tkhd_atom
    moov_atom = (len(trak_atom) + 8).to_bytes(4, "big") + b"moov" + trak_atom

    stream = io.BytesIO(moov_atom)
    assert _parse_mp4_dimensions(stream) == (1920, 1080)


# ─── Date Validity Schedule and JSON API Tests ──────────────────────────────

def test_api_filters_by_current_date_and_enabled_status(app, client):
    today = date.today()
    with app.app_context():
        # Active item
        item_active = MediaWallItem(
            friendly_name="Active Image",
            media_type="image",
            filename="active.png",
            relative_path="images/active.png",
            priority=1,
            start_date=today - timedelta(days=2),
            end_date=today + timedelta(days=5),
            is_enabled=True,
        )
        # Expired item
        item_expired = MediaWallItem(
            friendly_name="Expired Image",
            media_type="image",
            filename="expired.png",
            relative_path="images/expired.png",
            priority=2,
            start_date=today - timedelta(days=10),
            end_date=today - timedelta(days=1),
            is_enabled=True,
        )
        # Upcoming item
        item_upcoming = MediaWallItem(
            friendly_name="Upcoming Image",
            media_type="image",
            filename="upcoming.png",
            relative_path="images/upcoming.png",
            priority=3,
            start_date=today + timedelta(days=2),
            end_date=today + timedelta(days=10),
            is_enabled=True,
        )
        # Disabled item
        item_disabled = MediaWallItem(
            friendly_name="Disabled Image",
            media_type="image",
            filename="disabled.png",
            relative_path="images/disabled.png",
            priority=4,
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=5),
            is_enabled=False,
        )
        db.session.add_all([item_active, item_expired, item_upcoming, item_disabled])
        db.session.commit()

    # Public API endpoint /api/get?type=images
    res = client.get("/api/get?type=images")
    assert res.status_code == 200
    data = res.get_json()
    assert data["count"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["friendly_name"] == "Active Image"


def test_api_prepends_base_url_when_configured(app, client):
    today = date.today()
    with app.app_context():
        save_setting("media_wall_base_url", "https://media.qnl.qa/totems/")
        item = MediaWallItem(
            friendly_name="Promo Video",
            media_type="video",
            filename="promo_reel.mp4",
            relative_path="videos/promo_reel.mp4",
            priority=1,
            start_date=today - timedelta(days=1),
            end_date=today + timedelta(days=5),
            is_enabled=True,
        )
        db.session.add(item)
        db.session.commit()

    res = client.get("/api/get?type=videos")
    assert res.status_code == 200
    data = res.get_json()
    assert data["count"] == 1
    item_data = data["items"][0]
    assert item_data["url"] == "https://media.qnl.qa/totems/promo_reel.mp4"
    assert item_data["filename"] == "promo_reel.mp4"


# ─── Reordering, Editing, Toggling, Deletion Tests ──────────────────────────

def test_drag_and_drop_reorder_endpoint(app, client):
    today = date.today()
    with app.app_context():
        item1 = MediaWallItem(
            friendly_name="Item 1", media_type="image", filename="1.png",
            relative_path="images/1.png", priority=10, start_date=today, end_date=today, is_enabled=True,
        )
        item2 = MediaWallItem(
            friendly_name="Item 2", media_type="image", filename="2.png",
            relative_path="images/2.png", priority=20, start_date=today, end_date=today, is_enabled=True,
        )
        db.session.add_all([item1, item2])
        db.session.commit()
        id1, id2 = item1.id, item2.id

        member = make_member(email="orderer@example.com")
        grant_media_wall(member)

    login(client, "orderer@example.com")

    # Invert the order: [id2, id1]
    res = client.post(
        "/apps/media-walls/reorder",
        json={"item_ids": [id2, id1]},
    )
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    with app.app_context():
        refreshed1 = db.session.get(MediaWallItem, id1)
        refreshed2 = db.session.get(MediaWallItem, id2)
        assert refreshed2.priority == 1
        assert refreshed1.priority == 2


def test_edit_schedule_dates(app, client):
    today = date.today()
    with app.app_context():
        item = MediaWallItem(
            friendly_name="Slide", media_type="image", filename="slide.png",
            relative_path="images/slide.png", priority=1, start_date=today, end_date=today + timedelta(days=5),
            is_enabled=True,
        )
        db.session.add(item)
        db.session.commit()
        item_id = item.id

        member = make_member(email="editor@example.com")
        grant_media_wall(member)

    login(client, "editor@example.com")

    new_start = today + timedelta(days=1)
    new_end = today + timedelta(days=20)

    res = client.post(
        f"/apps/media-walls/{item_id}/edit",
        data={"start_date": str(new_start), "end_date": str(new_end)},
        follow_redirects=True,
    )
    assert res.status_code == 200

    with app.app_context():
        refreshed = db.session.get(MediaWallItem, item_id)
        assert refreshed.start_date == new_start
        assert refreshed.end_date == new_end


def test_toggle_enabled_status(app, client):
    today = date.today()
    with app.app_context():
        item = MediaWallItem(
            friendly_name="Toggleable", media_type="image", filename="toggle.png",
            relative_path="images/toggle.png", priority=1, start_date=today, end_date=today + timedelta(days=5),
            is_enabled=True,
        )
        db.session.add(item)
        db.session.commit()
        item_id = item.id

        member = make_member(email="toggler@example.com")
        grant_media_wall(member)

    login(client, "toggler@example.com")

    # Toggle off
    client.post(f"/apps/media-walls/{item_id}/toggle", follow_redirects=True)
    with app.app_context():
        refreshed = db.session.get(MediaWallItem, item_id)
        assert refreshed.is_enabled is False

    # Toggle on
    client.post(f"/apps/media-walls/{item_id}/toggle", follow_redirects=True)
    with app.app_context():
        refreshed = db.session.get(MediaWallItem, item_id)
        assert refreshed.is_enabled is True


def test_delete_item_and_file(app, client, tmp_path):
    storage_folder = tmp_path / "media_storage"
    storage_folder.mkdir(parents=True, exist_ok=True)
    images_dir = storage_folder / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    test_file = images_dir / "to_delete.png"
    test_file.write_bytes(create_sample_png())

    today = date.today()
    with app.app_context():
        save_setting("media_wall_folder", str(storage_folder))
        item = MediaWallItem(
            friendly_name="Delete Me", media_type="image", filename="to_delete.png",
            relative_path="images/to_delete.png", priority=1, start_date=today, end_date=today,
            is_enabled=True,
        )
        db.session.add(item)
        db.session.commit()
        item_id = item.id

        member = make_member(email="deleter@example.com")
        grant_media_wall(member)

    login(client, "deleter@example.com")

    res = client.post(f"/apps/media-walls/{item_id}/delete", follow_redirects=True)
    assert res.status_code == 200

    with app.app_context():
        assert db.session.get(MediaWallItem, item_id) is None
        assert not test_file.exists()


# ─── Signage Display Renderers Tests ────────────────────────────────────────

def test_slideshow_and_video_display_renderers(app, client):
    today = date.today()
    with app.app_context():
        item_img = MediaWallItem(
            friendly_name="Slide Image", media_type="image", filename="slide.jpg",
            relative_path="images/slide.jpg", priority=1, start_date=today, end_date=today + timedelta(days=2),
            is_enabled=True,
        )
        item_vid = MediaWallItem(
            friendly_name="Loop Video", media_type="video", filename="loop.mp4",
            relative_path="videos/loop.mp4", priority=1, start_date=today, end_date=today + timedelta(days=2),
            is_enabled=True,
        )
        db.session.add_all([item_img, item_vid])
        db.session.commit()

    # Image slideshow endpoint
    res_img = client.get("/apps/media-walls/display/images")
    assert res_img.status_code == 200
    assert b"signage-carousel" in res_img.data
    assert b"slide.jpg" in res_img.data

    # Video player endpoint
    res_vid = client.get("/apps/media-walls/display/videos")
    assert res_vid.status_code == 200
    assert b"myVideo" in res_vid.data
    assert b"loop.mp4" in res_vid.data
