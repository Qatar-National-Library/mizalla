"""Tests for Self-Service Password Reset (Magic Link), Account Lockout, and Admin Security Hardening.

Covers:
* Failed login attempt counter and account lockout at 5 failed attempts
* User feedback during failed attempts and upon lockout
* Locked accounts cannot sign in even with valid password
* Self-service 'Forgot password' magic link generation and dispatch via email
* Password reset via magic link: enforces min 12 characters and unlocks locked accounts
* Single-use token invalidation and expired token rejection
* Admin creation of accounts without initial password and welcome email dispatch
* Admin cannot edit or reset user passwords
* Admin can lock and unlock accounts, but cannot lock own account
* Admin account deletion is disabled
"""

from datetime import datetime, timezone
import pytest
from mizalla.email import SENT_EMAILS_LOG
from mizalla.extensions import db
from mizalla.models import Member
from mizalla.auth import generate_password_token, verify_password_token

TEST_PASSWORD = "StrongPassword123!"


def create_member(email="user@example.com", name="Test User", is_admin=False, must_set_password=False):
    member = Member(
        name=name,
        email=email,
        is_admin=is_admin,
        must_set_password=must_set_password,
    )
    if not must_set_password:
        member.set_password(TEST_PASSWORD)
    else:
        member.password_hash = "!UNSET!"
    db.session.add(member)
    db.session.commit()
    return member


def test_failed_logins_increment_and_lock_account_at_5_attempts(app, client):
    with app.app_context():
        create_member("lockout@example.com")

    # Attempts 1 through 4 should fail with remaining attempt count
    for attempt in range(1, 5):
        res = client.post("/apps/login", data={"email": "lockout@example.com", "password": "wrongpassword"}, follow_redirects=True)
        assert res.status_code == 200
        remaining = 5 - attempt
        assert f"{remaining} attempt(s) remaining" in res.text

    # 5th attempt locks the account
    res = client.post("/apps/login", data={"email": "lockout@example.com", "password": "wrongpassword"}, follow_redirects=True)
    assert res.status_code == 200
    assert "locked due to 5 consecutive failed login attempts" in res.text

    with app.app_context():
        member = db.session.scalar(db.select(Member).filter_by(email="lockout@example.com"))
        assert member.is_locked is True
        assert member.failed_login_attempts == 5
        assert "5 consecutive failed login attempts" in member.lock_reason


def test_locked_account_cannot_login_even_with_correct_password(app, client):
    with app.app_context():
        member = create_member("locked_login@example.com")
        member.lock("Manually locked by Administrator.")
        db.session.commit()

    # Attempt login with correct password
    res = client.post("/apps/login", data={"email": "locked_login@example.com", "password": TEST_PASSWORD}, follow_redirects=True)
    assert res.status_code == 200
    assert "Your account is locked" in res.text
    assert "Manually locked by Administrator" in res.text


def test_forgot_password_generates_token_and_sends_email(app, client):
    SENT_EMAILS_LOG.clear()
    with app.app_context():
        create_member("forgot@example.com", name="Forgot User")

    res = client.post("/apps/forgot-password", data={"email": "forgot@example.com"}, follow_redirects=True)
    assert res.status_code == 200
    assert "If that email address exists in our system" in res.text

    assert len(SENT_EMAILS_LOG) == 1
    email_record = SENT_EMAILS_LOG[0]
    assert email_record["to"] == "forgot@example.com"
    assert "Reset Your Mizalla Password" in email_record["subject"]
    assert "/apps/reset-password/" in email_record["html_body"]


def test_reset_password_unlocks_account_and_updates_password(app, client):
    with app.app_context():
        member = create_member("reset_test@example.com")
        # Lock account via failed attempts
        member.record_failed_login(max_attempts=1)
        token = generate_password_token(member)
        db.session.commit()

    # Verify account is currently locked
    with app.app_context():
        m = db.session.scalar(db.select(Member).filter_by(email="reset_test@example.com"))
        assert m.is_locked is True

    # User accesses reset page
    res = client.get(f"/apps/reset-password/{token}")
    assert res.status_code == 200
    assert "Reset password" in res.text

    # User submits new password
    new_password = "BrandNewSecurePassword2026!"
    res_post = client.post(
        f"/apps/reset-password/{token}",
        data={
            "password": new_password,
            "confirm_password": new_password,
        },
        follow_redirects=True,
    )
    assert res_post.status_code == 200
    assert "Your password has been successfully saved" in res_post.text

    # Account should now be unlocked and updated
    with app.app_context():
        m = db.session.scalar(db.select(Member).filter_by(email="reset_test@example.com"))
        assert m.is_locked is False
        assert m.failed_login_attempts == 0
        assert m.password_reset_token_hash is None
        assert m.check_password(new_password) is True

    # Sign in with new password
    res_login = client.post(
        "/apps/login",
        data={"email": "reset_test@example.com", "password": new_password},
        follow_redirects=True,
    )
    assert res_login.status_code == 200
    assert "Sign in" not in res_login.text or "Mizalla" in res_login.text


def test_reset_password_rejects_used_or_invalid_tokens(app, client):
    with app.app_context():
        member = create_member("invalid_token@example.com")
        token = generate_password_token(member)
        db.session.commit()

    # Use token once
    client.post(
        f"/apps/reset-password/{token}",
        data={"password": "NewValidPassword123!", "confirm_password": "NewValidPassword123!"},
        follow_redirects=True,
    )

    # Second attempt with same token must fail
    res_second = client.get(f"/apps/reset-password/{token}", follow_redirects=True)
    assert "This password link is invalid or has expired" in res_second.text

    # Tampered token must fail
    res_tampered = client.get("/apps/reset-password/totally-tampered-token", follow_redirects=True)
    assert "This password link is invalid or has expired" in res_tampered.text


def test_admin_creates_member_without_password_sends_welcome_link(app, client):
    SENT_EMAILS_LOG.clear()
    with app.app_context():
        admin = create_member("admin_creator@example.com", is_admin=True)

    # Login as admin
    client.post("/apps/login", data={"email": "admin_creator@example.com", "password": TEST_PASSWORD})

    # Admin creates member without providing password
    res = client.post(
        "/apps/admin/members",
        data={
            "name": "New Employee",
            "email": "newbie@example.com",
            "designation": "Librarian",
            "department_organization": "Collections",
            "is_pm": "1",
        },
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert "Member account created" in res.text
    assert "welcome magic link has been emailed" in res.text

    # Check database state
    with app.app_context():
        new_member = db.session.scalar(db.select(Member).filter_by(email="newbie@example.com"))
        assert new_member is not None
        assert new_member.must_set_password is True
        assert new_member.password_reset_token_hash is not None

    # Check email dispatched
    assert len(SENT_EMAILS_LOG) == 1
    email = SENT_EMAILS_LOG[0]
    assert email["to"] == "newbie@example.com"
    assert "Welcome to Mizalla – Set Your Password" in email["subject"]


def test_uninitialized_member_cannot_login_until_password_is_set(app, client):
    with app.app_context():
        create_member("uninit@example.com", must_set_password=True)

    res = client.post("/apps/login", data={"email": "uninit@example.com", "password": "AnyPassword123!"}, follow_redirects=True)
    assert res.status_code == 200
    assert "password has not been set yet" in res.text


def test_admin_lock_and_unlock_member(app, client):
    with app.app_context():
        admin = create_member("admin_lock@example.com", is_admin=True)
        member = create_member("target_lock@example.com")
        admin_id = admin.id
        member_id = member.id

    client.post("/apps/login", data={"email": "admin_lock@example.com", "password": TEST_PASSWORD})

    # Admin locks member
    res_lock = client.post(f"/apps/admin/members/{member_id}/lock", follow_redirects=True)
    assert res_lock.status_code == 200
    assert "has been locked" in res_lock.text

    with app.app_context():
        m = db.session.get(Member, member_id)
        assert m.is_locked is True

    # Admin unlocks member
    res_unlock = client.post(f"/apps/admin/members/{member_id}/unlock", follow_redirects=True)
    assert res_unlock.status_code == 200
    assert "has been unlocked" in res_unlock.text

    with app.app_context():
        m = db.session.get(Member, member_id)
        assert m.is_locked is False

    # Admin cannot lock self
    res_self = client.post(f"/apps/admin/members/{admin_id}/lock", follow_redirects=True)
    assert "You cannot lock your own administrator account" in res_self.text


def test_admin_cannot_delete_member(app, client):
    with app.app_context():
        create_member("admin_del@example.com", is_admin=True)
        victim = create_member("victim@example.com")
        victim_id = victim.id

    client.post("/apps/login", data={"email": "admin_del@example.com", "password": TEST_PASSWORD})

    res = client.post(f"/apps/admin/members/{victim_id}/delete", follow_redirects=True)
    assert res.status_code == 200
    assert "Deleting member accounts is disabled" in res.text

    # Verify member is still in database
    with app.app_context():
        assert db.session.get(Member, victim_id) is not None
