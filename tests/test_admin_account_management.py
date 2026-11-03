"""
Tests covering administrative account management, action buttons replication,
self-account safety guards, and self-service password changes from the profile view.
"""

from mizalla.extensions import db
from mizalla.models import Member
from tests.test_admin_role_management import TEST_PASSWORD, create_member


def test_admin_can_delete_other_member_account(app, client):
    with app.app_context():
        admin = create_member("admin1@example.com", name="Admin One", is_admin=True)
        target = create_member("target1@example.com", name="Target User", is_admin=False)
        target_id = target.id

    client.post("/apps/login", data={"email": "admin1@example.com", "password": TEST_PASSWORD})

    res = client.post(f"/apps/admin/members/{target_id}/delete", follow_redirects=True)
    assert res.status_code == 200
    assert "permanently deleted" in res.text

    with app.app_context():
        assert db.session.get(Member, target_id) is None


def test_admin_cannot_delete_own_account(app, client):
    with app.app_context():
        admin = create_member("admin2@example.com", name="Admin Two", is_admin=True)
        admin_id = admin.id

    client.post("/apps/login", data={"email": "admin2@example.com", "password": TEST_PASSWORD})

    res = client.post(f"/apps/admin/members/{admin_id}/delete", follow_redirects=True)
    assert res.status_code == 200
    assert "You cannot delete your own administrator account" in res.text

    with app.app_context():
        assert db.session.get(Member, admin_id) is not None


def test_admin_cannot_lock_own_account(app, client):
    with app.app_context():
        admin = create_member("admin3@example.com", name="Admin Three", is_admin=True)
        admin_id = admin.id

    client.post("/apps/login", data={"email": "admin3@example.com", "password": TEST_PASSWORD})

    res = client.post(f"/apps/admin/members/{admin_id}/lock", follow_redirects=True)
    assert res.status_code == 200
    assert "You cannot lock your own administrator account" in res.text

    with app.app_context():
        m = db.session.get(Member, admin_id)
        assert m.is_locked is False


def test_admin_can_lock_and_unlock_other_member_account(app, client):
    with app.app_context():
        admin = create_member("admin4@example.com", name="Admin Four", is_admin=True)
        target = create_member("target4@example.com", name="Target Four", is_admin=False)
        target_id = target.id

    client.post("/apps/login", data={"email": "admin4@example.com", "password": TEST_PASSWORD})

    # Lock
    res = client.post(f"/apps/admin/members/{target_id}/lock", follow_redirects=True)
    assert res.status_code == 200
    assert "has been locked" in res.text

    with app.app_context():
        assert db.session.get(Member, target_id).is_locked is True

    # Unlock
    res = client.post(f"/apps/admin/members/{target_id}/unlock", follow_redirects=True)
    assert res.status_code == 200
    assert "has been unlocked" in res.text

    with app.app_context():
        assert db.session.get(Member, target_id).is_locked is False


def test_directory_renders_proper_controls_for_self_and_others(app, client):
    with app.app_context():
        admin = create_member("admin5@example.com", name="Admin Five", is_admin=True)
        other = create_member("other5@example.com", name="Other User", is_admin=False)
        admin_id = admin.id
        other_id = other.id

    client.post("/apps/login", data={"email": "admin5@example.com", "password": TEST_PASSWORD})
    res = client.get("/apps/admin/members")
    assert res.status_code == 200

    # Self row should have Change password link
    assert f"/apps/admin/members/{admin_id}/edit#change-password" in res.text
    # Other row should have active Edit link and delete form
    assert f"/apps/admin/members/{other_id}/edit" in res.text
    assert f"/apps/admin/members/{other_id}/delete" in res.text


def test_admin_self_change_password(app, client):
    with app.app_context():
        admin = create_member("admin6@example.com", name="Admin Six", is_admin=True)
        admin_id = admin.id

    client.post("/apps/login", data={"email": "admin6@example.com", "password": TEST_PASSWORD})

    # 1. Incorrect current password
    res = client.post(
        f"/apps/admin/members/{admin_id}/edit",
        data={
            "change_password": "1",
            "current_password": "WrongPassword123!",
            "new_password": "BrandNewSecurePassword123!",
            "confirm_password": "BrandNewSecurePassword123!",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "Incorrect current password" in res.text

    # 2. Too short new password (< 12)
    res = client.post(
        f"/apps/admin/members/{admin_id}/edit",
        data={
            "change_password": "1",
            "current_password": TEST_PASSWORD,
            "new_password": "Short123!",
            "confirm_password": "Short123!",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "at least 12 characters" in res.text

    # 3. Password mismatch
    res = client.post(
        f"/apps/admin/members/{admin_id}/edit",
        data={
            "change_password": "1",
            "current_password": TEST_PASSWORD,
            "new_password": "BrandNewSecurePassword123!",
            "confirm_password": "DifferentPassword123!",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "do not match" in res.text

    # 4. Successful password update
    res = client.post(
        f"/apps/admin/members/{admin_id}/edit",
        data={
            "change_password": "1",
            "current_password": TEST_PASSWORD,
            "new_password": "BrandNewSecurePassword123!",
            "confirm_password": "BrandNewSecurePassword123!",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "Your password has been successfully updated" in res.text

    # Verify new password works for login
    client.post("/apps/logout", follow_redirects=True)
    login_res = client.post(
        "/apps/login",
        data={"email": "admin6@example.com", "password": "BrandNewSecurePassword123!"},
        follow_redirects=True,
    )
    assert login_res.status_code == 200
    assert "Sign Out" in login_res.text

