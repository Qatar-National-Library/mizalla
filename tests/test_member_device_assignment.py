"""Tests for assigning WhatsApp devices to members from the admin member form."""

from mizalla.extensions import db
from mizalla.models import GowaDevice, GowaDeviceAccess, Member


PASSWORD = "member-password-long"


def make_admin(email="admin@example.com"):
    admin = Member(name="Admin", email=email, is_admin=True)
    admin.set_password(PASSWORD)
    db.session.add(admin)
    db.session.commit()
    return admin


def make_member(app, email="member@example.com", name="Member"):
    member = Member(name=name, email=email)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def make_device(app, name, device_id):
    admin = make_admin(f"owner-{device_id}@example.com")
    device = GowaDevice(
        name=name,
        device_id=device_id,
        status="active",
        registered_by=admin,
        registered_by_name=admin.name,
    )
    db.session.add(device)
    db.session.commit()
    return device


def login(client, email):
    return client.post("/apps/login", data={"email": email, "password": PASSWORD})


def test_admin_sees_device_checkboxes_on_member_edit(app, client):
    """Admin sees every registered device with a checkbox, pre-checked if assigned."""
    with app.app_context():
        make_admin()
        unassigned = make_device(app, "Sales Line", "dev-sales")
        member = make_member(app)
        assigned = make_device(app, "Support Line", "dev-support")
        db.session.add(
            GowaDeviceAccess(
                device_id=assigned.id, member_id=member.id, granted_by_id=unassigned.registered_by_id
            )
        )
        db.session.commit()
        member_id = member.id
        assigned_id = assigned.id
        unassigned_id = unassigned.id

    login(client, "admin@example.com")
    response = client.get(f"/apps/admin/members/{member_id}/edit")
    assert response.status_code == 200
    body = response.data.decode()

    assert "WhatsApp Device Access" in body
    assert body.count('name="device_ids"') == 2
    assert "Sales Line" in body
    assert "Support Line" in body
    # Assigned device is pre-checked, unassigned one is not
    assert f'value="{assigned_id}" checked' in body
    assert f'value="{unassigned_id}" checked' not in body


def test_member_edit_saves_device_assignment(app, client):
    """Saving the form creates GowaDeviceAccess rows for the checked devices."""
    with app.app_context():
        make_admin()
        sales = make_device(app, "Sales Line", "dev-sales")
        support = make_device(app, "Support Line", "dev-support")
        member = make_member(app)
        member_id = member.id
        sales_id, support_id = sales.id, support.id

    login(client, "admin@example.com")
    response = client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={
            "name": "Member",
            "email": "member@example.com",
            "device_ids": [str(support_id)],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        grants = {
            g.device_id
            for g in GowaDeviceAccess.query.filter_by(member_id=member_id).all()
        }
        assert grants == {support_id}


def test_member_edit_replaces_device_assignment(app, client):
    """Submitting a different selection revokes the old grants."""
    with app.app_context():
        admin = make_admin()
        sales = make_device(app, "Sales Line", "dev-sales")
        support = make_device(app, "Support Line", "dev-support")
        member = make_member(app)
        db.session.add(
            GowaDeviceAccess(device_id=sales.id, member_id=member.id, granted_by_id=admin.id)
        )
        db.session.commit()
        member_id, sales_id, support_id = member.id, sales.id, support.id

    login(client, "admin@example.com")
    client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={
            "name": "Member",
            "email": "member@example.com",
            "device_ids": [str(support_id)],
        },
    )

    with app.app_context():
        grants = {g.device_id for g in GowaDeviceAccess.query.filter_by(member_id=member_id).all()}
        assert grants == {support_id}
        assert sales_id not in grants


def test_member_edit_can_clear_all_devices(app, client):
    """Submitting with no devices selected revokes every grant."""
    with app.app_context():
        admin = make_admin()
        sales = make_device(app, "Sales Line", "dev-sales")
        member = make_member(app)
        db.session.add(
            GowaDeviceAccess(device_id=sales.id, member_id=member.id, granted_by_id=admin.id)
        )
        db.session.commit()
        member_id = member.id

    login(client, "admin@example.com")
    client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={"name": "Member", "email": "member@example.com"},
    )

    with app.app_context():
        assert GowaDeviceAccess.query.filter_by(member_id=member_id).count() == 0


def test_member_edit_does_not_duplicate_existing_grant(app, client):
    """Re-saving an unchanged selection does not create duplicate grants."""
    with app.app_context():
        admin = make_admin()
        sales = make_device(app, "Sales Line", "dev-sales")
        member = make_member(app)
        db.session.add(
            GowaDeviceAccess(device_id=sales.id, member_id=member.id, granted_by_id=admin.id)
        )
        db.session.commit()
        member_id, sales_id = member.id, sales.id

    login(client, "admin@example.com")
    client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={
            "name": "Member",
            "email": "member@example.com",
            "device_ids": [str(sales_id)],
        },
    )

    with app.app_context():
        assert GowaDeviceAccess.query.filter_by(member_id=member_id).count() == 1


def test_non_admin_cannot_edit_member_or_assign_devices(app, client):
    """Non-admins are blocked from the member edit page and its POST handler."""
    with app.app_context():
        make_admin()
        make_device(app, "Sales Line", "dev-sales")
        member = make_member(app)
        member_id, sales_id = member.id, 1

    login(client, "member@example.com")
    assert client.get(f"/apps/admin/members/{member_id}/edit").status_code == 403
    assert (
        client.post(
            f"/apps/admin/members/{member_id}/edit",
            data={
                "name": "Member",
                "email": "member@example.com",
                "device_ids": [str(sales_id)],
            },
        ).status_code
        == 403
    )

    with app.app_context():
        assert GowaDeviceAccess.query.filter_by(member_id=member_id).count() == 0


def test_device_section_shows_empty_message_when_no_devices_registered(app, client):
    """The fieldset shows an informative empty message when no devices are registered."""
    with app.app_context():
        make_admin()
        member = make_member(app)
        member_id = member.id

    login(client, "admin@example.com")
    response = client.get(f"/apps/admin/members/{member_id}/edit")
    body = response.data.decode()
    assert response.status_code == 200
    assert "No registered devices available" in body


def test_unchecking_bulk_messenger_revokes_device_access(app, client):
    """Unchecking bulk_whatsapp feature revokes device access for the member."""
    with app.app_context():
        admin = make_admin()
        sales = make_device(app, "Sales Line", "dev-sales")
        member = make_member(app)
        db.session.add(
            GowaDeviceAccess(device_id=sales.id, member_id=member.id, granted_by_id=admin.id)
        )
        db.session.commit()
        member_id, sales_id = member.id, sales.id

    login(client, "admin@example.com")
    # Post form with feature_ids but NOT bulk_whatsapp
    client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={
            "name": "Member",
            "email": "member@example.com",
            "feature_ids": ["bulk_email"],
            "device_ids": [str(sales_id)],
        },
    )

    with app.app_context():
        assert GowaDeviceAccess.query.filter_by(member_id=member_id).count() == 0


def test_member_creation_with_devices(app, client):
    """Creating a member with bulk_whatsapp and device_ids grants device access."""
    with app.app_context():
        make_admin()
        sales = make_device(app, "Sales Line", "dev-sales")
        sales_id = sales.id

    login(client, "admin@example.com")
    client.post(
        "/apps/admin/members",
        data={
            "name": "New Member",
            "email": "newmember@example.com",
            "password": "new-password-123",
            "feature_ids": ["bulk_whatsapp"],
            "device_ids": [str(sales_id)],
        },
        follow_redirects=True,
    )

    with app.app_context():
        new_member = Member.query.filter_by(email="newmember@example.com").first()
        assert new_member is not None
        grants = {g.device_id for g in GowaDeviceAccess.query.filter_by(member_id=new_member.id).all()}
        assert grants == {sales_id}

