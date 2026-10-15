"""Tests for utility application feedback, rating, workflow, and admin moderation."""

import pytest
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    FEATURE_BULK_EMAIL,
    FEATURE_VISION,
    FEEDBACK_STATUS_ACTIVE,
    FEEDBACK_STATUS_HIDDEN,
    FEEDBACK_TARGET_EXISTING,
    FEEDBACK_TARGET_NEW,
    Member,
    UtilityAppFeedback,
)

PASSWORD = "test-password-123"


def make_member(name="Test User", email="user@example.com", is_admin=False, **kwargs):
    member = Member(name=name, email=email, is_admin=is_admin, **kwargs)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def login(client, email="user@example.com"):
    return client.post("/apps/login", data={"email": email, "password": PASSWORD})


# ─── Accessible Apps & Authorization ─────────────────────────────────────────

def test_accessible_apps_requires_login(app, client):
    """Anonymous visitors must be redirected to sign in."""
    resp = client.get("/apps/feedback/accessible-apps")
    assert resp.status_code == 302
    assert "/apps/login" in resp.headers["Location"]


def test_accessible_apps_returns_only_granted_apps_for_member(app, client):
    """Members only see the utility apps they have active access grants to."""
    with app.app_context():
        member = make_member()
        sync_member_features(member, [FEATURE_BULK_EMAIL])
        db.session.commit()

    login(client)
    resp = client.get("/apps/feedback/accessible-apps")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "apps" in data
    keys = [a["feature_key"] for a in data["apps"]]
    assert FEATURE_BULK_EMAIL in keys
    assert FEATURE_VISION not in keys


def test_admin_sees_all_apps_in_accessible_apps(app, client):
    """Admins pass all feature gates, so all utility apps are listed."""
    with app.app_context():
        make_member("Admin", "admin@example.com", is_admin=True)

    login(client, "admin@example.com")
    resp = client.get("/apps/feedback/accessible-apps")
    assert resp.status_code == 200
    data = resp.get_json()
    keys = [a["feature_key"] for a in data["apps"]]
    assert FEATURE_BULK_EMAIL in keys
    assert FEATURE_VISION in keys


# ─── Rating & Suggestion Submissions ─────────────────────────────────────────

def test_submit_rating_requires_login(app, client):
    resp = client.post("/apps/feedback/submit", data={"target_type": "existing"})
    assert resp.status_code == 302


def test_submit_rating_denies_unauthorized_app(app, client):
    """User cannot rate an app they do not have access to."""
    with app.app_context():
        make_member()
        # No grants assigned

    login(client)
    resp = client.post(
        "/apps/feedback/submit",
        data={
            "target_type": "existing",
            "feature_key": FEATURE_VISION,
            "rating": "8",
            "comments": "Great tool",
        },
    )
    assert resp.status_code == 403
    assert "access" in resp.get_json()["error"]


def test_submit_rating_enforces_1_to_10_bounds(app, client):
    """Ratings must be between 1 and 10."""
    with app.app_context():
        member = make_member()
        sync_member_features(member, [FEATURE_BULK_EMAIL])
        db.session.commit()

    login(client)

    # Below 1
    resp_zero = client.post(
        "/apps/feedback/submit",
        data={"target_type": "existing", "feature_key": FEATURE_BULK_EMAIL, "rating": "0"},
    )
    assert resp_zero.status_code == 400

    # Above 10
    resp_eleven = client.post(
        "/apps/feedback/submit",
        data={"target_type": "existing", "feature_key": FEATURE_BULK_EMAIL, "rating": "11"},
    )
    assert resp_eleven.status_code == 400

    # Valid rating (10)
    resp_ten = client.post(
        "/apps/feedback/submit",
        data={
            "target_type": "existing",
            "feature_key": FEATURE_BULK_EMAIL,
            "rating": "10",
            "comments": "Super clean interface!",
        },
    )
    assert resp_ten.status_code == 200
    assert resp_ten.get_json()["feedback_id"] is not None


def test_submit_rating_updates_existing_review_without_duplicate(app, client):
    """One current rating per user per utility app; updating updates the existing record."""
    with app.app_context():
        member = make_member()
        sync_member_features(member, [FEATURE_BULK_EMAIL])
        db.session.commit()

    login(client)

    # First submission
    r1 = client.post(
        "/apps/feedback/submit",
        data={
            "target_type": "existing",
            "feature_key": FEATURE_BULK_EMAIL,
            "rating": "7",
            "comments": "Initial thought",
        },
    )
    assert r1.status_code == 200
    fb_id_1 = r1.get_json()["feedback_id"]

    # Second submission for the same app
    r2 = client.post(
        "/apps/feedback/submit",
        data={
            "target_type": "existing",
            "feature_key": FEATURE_BULK_EMAIL,
            "rating": "9",
            "comments": "Much improved!",
        },
    )
    assert r2.status_code == 200
    fb_id_2 = r2.get_json()["feedback_id"]

    assert fb_id_1 == fb_id_2

    with app.app_context():
        total_rows = UtilityAppFeedback.query.filter_by(
            feature_key=FEATURE_BULK_EMAIL
        ).count()
        assert total_rows == 1
        entry = UtilityAppFeedback.query.get(fb_id_1)
        assert entry.rating == 9
        assert entry.comments == "Much improved!"


def test_submit_new_utility_app_proposal(app, client):
    """Users can propose an entirely new utility app."""
    with app.app_context():
        make_member()

    login(client)
    resp = client.post(
        "/apps/feedback/submit",
        data={
            "target_type": "new",
            "app_title": "OCR Document Parser",
            "comments": "Needs to parse scanned Arabic PDF documents and extract tables.",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["feedback_id"] is not None

    with app.app_context():
        fb = UtilityAppFeedback.query.get(data["feedback_id"])
        assert fb.target_type == FEEDBACK_TARGET_NEW
        assert fb.app_title == "OCR Document Parser"
        assert fb.rating is None
        assert "Arabic PDF" in fb.comments


# ─── Admin Dashboard & Moderation Workflow ──────────────────────────────────

def test_admin_feedback_dashboard_access_control(app, client):
    """Non-admins cannot access the admin feedback dashboard."""
    with app.app_context():
        make_member("Regular", "user@example.com")
        make_member("Admin", "admin@example.com", is_admin=True)

    login(client, "user@example.com")
    assert client.get("/apps/admin/feedback").status_code == 403

    login(client, "admin@example.com")
    resp = client.get("/apps/admin/feedback")
    assert resp.status_code == 200
    assert "Feedback & Suggestions" in resp.get_data(as_text=True)


def test_admin_toggle_visibility_status(app, client):
    """Admin can hide or keep active a feedback rating, but not delete it."""
    with app.app_context():
        admin = make_member("Admin", "admin@example.com", is_admin=True)
        admin_id = admin.id
        user = make_member("User", "user@example.com")
        fb = UtilityAppFeedback(
            target_type=FEEDBACK_TARGET_EXISTING,
            feature_key=FEATURE_BULK_EMAIL,
            app_title="Bulk Emailer",
            rating=6,
            comments="Needs better drafts",
            member_id=user.id,
            status=FEEDBACK_STATUS_ACTIVE,
        )
        db.session.add(fb)
        db.session.commit()
        fb_id = fb.id

    login(client, "admin@example.com")

    # Toggle to hidden
    resp = client.post(
        f"/apps/admin/feedback/{fb_id}/status",
        data={"status": "hidden"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == FEEDBACK_STATUS_HIDDEN

    with app.app_context():
        fb_updated = db.session.get(UtilityAppFeedback, fb_id)
        assert fb_updated.status == FEEDBACK_STATUS_HIDDEN
        assert fb_updated.is_hidden is True
        assert fb_updated.admin_reviewed_by_id == admin_id

    # Toggle back to active
    resp2 = client.post(
        f"/apps/admin/feedback/{fb_id}/status",
        data={"status": "active"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp2.status_code == 200
    assert resp2.get_json()["status"] == FEEDBACK_STATUS_ACTIVE


def test_admin_save_notes_against_feedback(app, client):
    """Admin can record internal notes / comments against any user rating or suggestion."""
    with app.app_context():
        admin = make_member("Admin", "admin@example.com", is_admin=True)
        admin_id = admin.id
        user = make_member("User", "user@example.com")
        fb = UtilityAppFeedback(
            target_type=FEEDBACK_TARGET_NEW,
            app_title="Audio Transcriber",
            comments="Convert meeting voice recordings to text notes.",
            member_id=user.id,
            status=FEEDBACK_STATUS_ACTIVE,
        )
        db.session.add(fb)
        db.session.commit()
        fb_id = fb.id

    login(client, "admin@example.com")

    resp = client.post(
        f"/apps/admin/feedback/{fb_id}/notes",
        data={"admin_notes": "Added to Q3 product backlog for Whisper model evaluation."},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "Whisper" in data["admin_notes"]

    with app.app_context():
        fb_updated = db.session.get(UtilityAppFeedback, fb_id)
        assert fb_updated.admin_notes == "Added to Q3 product backlog for Whisper model evaluation."
        assert fb_updated.admin_reviewed_by_id == admin_id
        assert fb_updated.admin_reviewed_at is not None


def test_admin_export_excel_and_csv(app, client):
    """Admin can export ratings and feedback to Excel (.xlsx) and CSV."""
    with app.app_context():
        make_member("Admin", "admin@example.com", is_admin=True)
        user = make_member("User", "user@example.com")
        fb = UtilityAppFeedback(
            target_type=FEEDBACK_TARGET_EXISTING,
            feature_key=FEATURE_BULK_EMAIL,
            app_title="Bulk Emailer",
            rating=10,
            comments="Flawless delivery",
            member_id=user.id,
            status=FEEDBACK_STATUS_ACTIVE,
        )
        db.session.add(fb)
        db.session.commit()

    login(client, "admin@example.com")

    # CSV Export
    resp_csv = client.get("/apps/admin/feedback/export?format=csv")
    assert resp_csv.status_code == 200
    assert resp_csv.mimetype == "text/csv"
    assert "attachment; filename=utility_app_feedback.csv" in resp_csv.headers.get("Content-Disposition", "")
    assert "Flawless delivery" in resp_csv.get_data(as_text=True)

    # Excel Export
    resp_xlsx = client.get("/apps/admin/feedback/export?format=xlsx")
    assert resp_xlsx.status_code == 200
    assert "application/vnd.openxmlformats" in resp_xlsx.mimetype
    assert "attachment; filename=utility_app_feedback.xlsx" in resp_xlsx.headers.get("Content-Disposition", "")


def test_feedback_index_redirects_member_and_admin(app, client):
    """Navigating to /apps/feedback directs regular users to open modal, and admins to moderation."""
    with app.app_context():
        make_member("User", "regular_fb@example.com")
        make_member("Admin", "admin_fb@example.com", is_admin=True)

    # Anonymous -> login
    res_anon = client.get("/apps/feedback")
    assert res_anon.status_code == 302
    assert "/apps/login" in res_anon.headers["Location"]

    # Member -> projects with ?open_feedback=true
    login(client, "regular_fb@example.com")
    res_member = client.get("/apps/feedback")
    assert res_member.status_code == 302
    assert "open_feedback=true" in res_member.headers["Location"]

    # Admin -> /apps/admin/feedback
    login(client, "admin_fb@example.com")
    res_admin = client.get("/apps/feedback")
    assert res_admin.status_code == 302
    assert "/apps/admin/feedback" in res_admin.headers["Location"]


def test_feedback_button_rendered_in_header_menu(app, client):
    """The feedback button and modal must be rendered in the top header menu on authenticated pages."""
    with app.app_context():
        make_member("Regular", "header_fb@example.com")

    login(client, "header_fb@example.com")
    resp = client.get("/apps/mizalla/", follow_redirects=True)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Top header feedback button
    assert 'id="open-feedback-btn"' in html
    assert 'class="nav-feedback-btn"' in html
    assert "Feedback" in html

    # User dropdown feedback trigger placed right before Sign Out
    assert 'id="dropdown-feedback-btn"' in html
    assert "Give Feedback & Suggestions" in html
    assert html.index('id="dropdown-feedback-btn"') < html.index('Sign Out')

    # Hamburger menu is removed
    assert 'id="hamburger"' not in html
    assert 'id="hamburger-menu"' not in html
    assert 'id="hamburger-feedback-btn"' not in html

    # Dialog modal and form
    assert 'id="feedback-modal"' in html
    assert 'id="feedback-form"' in html
    assert 'action="/apps/feedback/submit"' in html
