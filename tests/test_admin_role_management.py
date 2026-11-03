"""Tests for Administrator Role Assignment & Directory UI Simplification."""

import pytest
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import FEATURE_CHAT, FEATURE_VISION, Member

TEST_PASSWORD = "StrongPassword123!"


def create_member(email="user@example.com", name="Test User", is_admin=False):
    member = Member(
        name=name,
        email=email,
        is_admin=is_admin,
        is_pm=is_admin,
        must_set_password=False,
    )
    member.set_password(TEST_PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def test_create_member_with_admin_role(app, client):
    with app.app_context():
        create_member("superadmin@example.com", name="Super Admin", is_admin=True)

    client.post("/apps/login", data={"email": "superadmin@example.com", "password": TEST_PASSWORD})

    # Create a new member with Administrator checkbox checked
    res = client.post(
        "/apps/admin/members",
        data={
            "name": "New Admin",
            "email": "newadmin@example.com",
            "designation": "Lead Architect",
            "department_organization": "IT",
            "is_admin": "1",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200

    with app.app_context():
        m = db.session.scalar(db.select(Member).filter_by(email="newadmin@example.com"))
        assert m is not None
        assert m.is_admin is True
        assert m.is_pm is True


def test_create_member_without_admin_role(app, client):
    with app.app_context():
        create_member("superadmin@example.com", name="Super Admin", is_admin=True)

    client.post("/apps/login", data={"email": "superadmin@example.com", "password": TEST_PASSWORD})

    res = client.post(
        "/apps/admin/members",
        data={
            "name": "Regular User",
            "email": "regular@example.com",
            "designation": "Analyst",
            "department_organization": "Operations",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200

    with app.app_context():
        m = db.session.scalar(db.select(Member).filter_by(email="regular@example.com"))
        assert m is not None
        assert m.is_admin is False


def test_edit_member_promote_and_demote_admin(app, client):
    with app.app_context():
        admin = create_member("superadmin@example.com", name="Super Admin", is_admin=True)
        colleague = create_member("colleague@example.com", name="Colleague", is_admin=False)
        colleague_id = colleague.id

    client.post("/apps/login", data={"email": "superadmin@example.com", "password": TEST_PASSWORD})

    # Promote colleague to Admin
    res_promote = client.post(
        f"/apps/admin/members/{colleague_id}/edit",
        data={
            "designation": "Senior Engineer",
            "department_organization": "IT",
            "is_admin": "1",
        },
        follow_redirects=True,
    )
    assert res_promote.status_code == 200

    with app.app_context():
        m = db.session.get(Member, colleague_id)
        assert m.is_admin is True

    # Demote colleague back to regular user
    res_demote = client.post(
        f"/apps/admin/members/{colleague_id}/edit",
        data={
            "designation": "Senior Engineer",
            "department_organization": "IT",
        },
        follow_redirects=True,
    )
    assert res_demote.status_code == 200

    with app.app_context():
        m = db.session.get(Member, colleague_id)
        assert m.is_admin is False


def test_admin_cannot_revoke_own_admin_privileges(app, client):
    with app.app_context():
        admin = create_member("superadmin@example.com", name="Super Admin", is_admin=True)
        admin_id = admin.id

    client.post("/apps/login", data={"email": "superadmin@example.com", "password": TEST_PASSWORD})

    # Admin attempts to uncheck is_admin on own account
    res = client.post(
        f"/apps/admin/members/{admin_id}/edit",
        data={
            "designation": "Super Admin",
            "department_organization": "IT",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "You cannot revoke your own administrator privileges" in res.text

    with app.app_context():
        m = db.session.get(Member, admin_id)
        assert m.is_admin is True


def test_admin_directory_listing_omits_service_tags_for_admins(app, client):
    import re

    with app.app_context():
        admin = create_member("admin_viewer@example.com", name="Alice Admin", is_admin=True)
        regular = create_member("regular_viewer@example.com", name="Bob Regular", is_admin=False)
        sync_member_features(admin, [FEATURE_CHAT, FEATURE_VISION])
        sync_member_features(regular, [FEATURE_CHAT, FEATURE_VISION])
        db.session.commit()

    client.post("/apps/login", data={"email": "admin_viewer@example.com", "password": TEST_PASSWORD})
    res = client.get("/apps/admin/members")
    assert res.status_code == 200
    body = res.get_data(as_text=True)

    def row_for(name):
        rows = [row for row in re.findall(r"<article class=\"directory-row\">.*?</article>", body, re.S)
                if name in row]
        assert len(rows) == 1, f"expected one row for {name}, found {len(rows)}"
        return rows[0]

    admin_row = row_for("Alice Admin")
    regular_row = row_for("Bob Regular")

    # Admin row has Admin badge but no service tags
    assert "admin-tag" in admin_row
    assert "Admin" in admin_row
    assert "service-tag" not in admin_row

    # Non-admin row shows granted service tags
    assert "admin-tag" not in regular_row
    assert "service-tag" in regular_row
    assert "Chat Service" in regular_row
    assert "Vision Test Service" in regular_row
