"""Tests for per-member service access grants.

Access used to be a single ``Member.can_bulk_email`` boolean. It now lives in
``AppFeatureAccess`` so a new service needs no schema change and every service
is granted the same way. These tests cover the grant helpers, the legacy
backfill, and the fact that the gates are enforced server-side rather than
only hidden in the navigation.
"""

import pytest

from mizalla.extensions import db
from mizalla.feature_access import (
    backfill_legacy_access,
    granted_member_ids,
    has_feature_access,
    member_feature_keys,
    sync_member_features,
)
from mizalla.models import (
    FEATURE_BULK_EMAIL,
    FEATURE_CHAT,
    FEATURE_KEYS,
    FEATURE_SPEECH,
    FEATURE_VISION,
    AppFeatureAccess,
    Member,
)

PASSWORD = "member-password-long"


def make_member(name="User", email="user@example.com", **kwargs):
    member = Member(name=name, email=email, **kwargs)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def login(client, email="user@example.com"):
    return client.post("/apps/login", data={"email": email, "password": PASSWORD})


# ─── Grant helpers ──────────────────────────────────────────────────────────

def test_grant_is_idempotent(app):
    with app.app_context():
        member = make_member()
        from mizalla.feature_access import _grant

        assert _grant(FEATURE_VISION, member.id) is True
        assert _grant(FEATURE_VISION, member.id) is False
        db.session.commit()
        assert AppFeatureAccess.query.filter_by(member_id=member.id).count() == 1


def test_sync_ignores_an_unknown_feature(app):
    """A tampered form value must not create a grant for a non-existent service.

    ``sync_member_features`` is fed ``request.form.getlist`` directly, so
    filtering unknown keys here is what stops a crafted POST from inventing a
    service. ``_grant`` raises as the backstop.
    """
    with app.app_context():
        member = make_member()
        sync_member_features(member, [FEATURE_VISION, "not-a-service"])
        db.session.commit()
        assert member_feature_keys(member) == [FEATURE_VISION]

        from mizalla.feature_access import _grant

        with pytest.raises(ValueError):
            _grant("not-a-service", member.id)


def test_admins_pass_every_gate_without_a_grant(app):
    with app.app_context():
        admin = make_member("Admin", "admin@example.com", is_admin=True)
        assert has_feature_access(admin, FEATURE_VISION) is True
        assert has_feature_access(admin, FEATURE_CHAT) is True
        assert granted_member_ids(FEATURE_VISION) == set()


def test_anonymous_is_never_granted(app):
    with app.app_context():
        from werkzeug.local import LocalProxy

        assert has_feature_access(LocalProxy(lambda: None), FEATURE_VISION) is False


def test_sync_adds_and_removes_exactly_the_requested_keys(app):
    with app.app_context():
        member = make_member()
        sync_member_features(member, [FEATURE_VISION, FEATURE_SPEECH])
        db.session.commit()
        assert set(member_feature_keys(member)) == {FEATURE_VISION, FEATURE_SPEECH}

        sync_member_features(member, [FEATURE_SPEECH])
        db.session.commit()
        assert member_feature_keys(member) == [FEATURE_SPEECH]


def test_sync_records_who_granted_access(app):
    with app.app_context():
        admin = make_member("Admin", "admin@example.com", is_admin=True)
        member = make_member()
        sync_member_features(member, [FEATURE_CHAT], granted_by_id=admin.id)
        db.session.commit()
        grant = AppFeatureAccess.query.filter_by(member_id=member.id, feature_key=FEATURE_CHAT).one()
        assert grant.granted_by_id == admin.id


def test_every_known_feature_has_a_label():
    from mizalla.models import FEATURE_LABELS

    assert set(FEATURE_KEYS) == set(FEATURE_LABELS)


# ─── Legacy backfill ────────────────────────────────────────────────────────

def test_backfill_seeds_grants_from_the_legacy_flag(app):
    with app.app_context():
        legacy = make_member("Legacy", "legacy@example.com", can_bulk_email=True)
        plain = make_member("Plain", "plain@example.com", can_bulk_email=False)
        db.session.commit()

        assert backfill_legacy_access() == 1
        assert has_feature_access(legacy, FEATURE_BULK_EMAIL) is True
        assert has_feature_access(plain, FEATURE_BULK_EMAIL) is False


def test_backfill_running_twice_creates_nothing_new(app):
    with app.app_context():
        make_member("Legacy", "legacy@example.com", can_bulk_email=True)
        db.session.commit()
        assert backfill_legacy_access() == 1
        assert backfill_legacy_access() == 0
        assert AppFeatureAccess.query.count() == 1


# ─── Route enforcement ──────────────────────────────────────────────────────

def test_bulk_email_gate_uses_the_grant_not_the_legacy_flag(app, client):
    """A member with the new grant but no legacy flag gets in, and vice versa."""
    with app.app_context():
        member = make_member()
        sync_member_features(member, [FEATURE_BULK_EMAIL])
        db.session.commit()

    login(client)
    assert client.get("/apps/emailer").status_code == 200

    with app.app_context():
        db.session.execute(db.delete(AppFeatureAccess))
        db.session.commit()
        member = Member.query.filter_by(email="user@example.com").one()
        member.can_bulk_email = True
        db.session.commit()

    # The flag alone must no longer open the door.
    assert client.get("/apps/emailer").status_code == 403


def test_whatsapp_gate_requires_its_own_grant(app, client):
    with app.app_context():
        member = make_member()
        sync_member_features(member, [FEATURE_BULK_EMAIL])
        db.session.commit()

    login(client)
    assert client.get("/apps/whatsapp").status_code == 403

    with app.app_context():
        member = Member.query.filter_by(email="user@example.com").one()
        sync_member_features(member, [FEATURE_BULK_EMAIL, "bulk_whatsapp"])
        db.session.commit()

    assert client.get("/apps/whatsapp").status_code == 200


@pytest.mark.parametrize(
    "path", ["/apps/ai/vision", "/apps/ai/speech", "/apps/ai/chat"]
)
def test_ai_services_are_closed_to_ungranted_members(app, client, path):
    with app.app_context():
        make_member()
    login(client)
    assert client.get(path).status_code == 403


# ─── Admin UI ───────────────────────────────────────────────────────────────

def test_admin_grants_and_revokes_services_from_the_member_form(app, client):
    with app.app_context():
        make_member("Admin", "admin@example.com", is_admin=True)
        member = make_member()
        member_id = member.id
        db.session.commit()

    login(client, "admin@example.com")
    response = client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={
            "name": "User",
            "email": "user@example.com",
            "feature_ids": [FEATURE_VISION, FEATURE_SPEECH],
        },
    )
    assert response.status_code == 302

    with app.app_context():
        member = db.session.get(Member, member_id)
        assert set(member_feature_keys(member)) == {FEATURE_VISION, FEATURE_SPEECH}
        # The legacy flag is kept in step so the backfill stays idempotent.
        assert member.can_bulk_email is False

    client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={"name": "User", "email": "user@example.com", "feature_ids": [FEATURE_VISION]},
    )
    with app.app_context():
        member = db.session.get(Member, member_id)
        assert member_feature_keys(member) == [FEATURE_VISION]


def test_admin_member_list_names_only_the_granted_services(app, client):
    """The tags are scoped to the member's own row.

    Asserting the label appears somewhere in the page would pass off the
    "Add member" form, which lists every service regardless of grants, and would
    not catch a revoked grant still being shown.
    """
    import re

    with app.app_context():
        make_member("Admin", "admin@example.com", is_admin=True)
        granted = make_member("Alice Granted", "grants@example.com")
        bare = make_member("Bob Bare", "bare@example.com")
        sync_member_features(granted, [FEATURE_CHAT, FEATURE_VISION])
        db.session.commit()

    login(client, "admin@example.com")
    body = client.get("/apps/admin/members").get_data(as_text=True)

    def row_for(name):
        rows = [row for row in re.findall(r"<article class=\"directory-row\">.*?</article>", body, re.S)
                if name in row]
        assert len(rows) == 1, f"expected one row for {name}, found {len(rows)}"
        return rows[0]

    granted_row, bare_row = row_for("Alice Granted"), row_for("Bob Bare")
    assert "Chat Service" in granted_row and "Vision Test Service" in granted_row
    # Services that were not granted must not appear on this member's row.
    assert "Speech Test Service" not in granted_row
    assert "Bulk Emailer" not in granted_row
    # And a member with no grants shows no tags at all.
    assert "service-tag" not in bare_row


def test_non_admin_cannot_grant_access(app, client):
    with app.app_context():
        make_member("Admin", "admin@example.com", is_admin=True)
        victim = make_member("Victim", "victim@example.com")
        victim_id = victim.id
        db.session.commit()

    login(client)
    client.post(
        f"/apps/admin/members/{victim_id}/edit",
        data={"name": "Victim", "email": "victim@example.com", "feature_ids": [FEATURE_VISION]},
    )

    with app.app_context():
        victim = db.session.get(Member, victim_id)
        assert has_feature_access(victim, FEATURE_VISION) is False
