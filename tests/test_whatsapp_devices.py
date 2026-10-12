"""Tests for WhatsApp device registration and listing."""

import pytest
from mizalla.extensions import db
from mizalla.models import GowaDevice, Member


ONLINE = {
    "online": True,
    "status_code": 200,
    "url": "http://127.0.0.1:3000",
    "error": None,
}


@pytest.fixture(autouse=True)
def gowa_online(monkeypatch):
    """Assume GOWA is up by default so these tests never touch the network.

    The devices page probes GOWA on every request; without this the suite
    would pass or fail depending on whether the real server happens to be
    running on the developer machine. Offline behaviour is covered in
    test_gowa_health.py.
    """
    monkeypatch.setattr("mizalla.whatsapp.check_health", lambda *a, **k: dict(ONLINE))


def make_admin(name="Admin", email="admin@example.com"):
    admin = Member(name=name, email=email, is_admin=True)
    admin.set_password("admin-password-long")
    db.session.add(admin)
    return admin


def test_device_list_requires_admin(app, client):
    """Non-admin users cannot access the device list."""
    with app.app_context():
        user = Member(name="User", email="user@example.com")
        user.set_password("user-password-long")
        db.session.add(user)
        db.session.commit()

    client.post("/apps/login", data={"email": "user@example.com", "password": "user-password-long"})
    response = client.get("/apps/whatsapp/devices")
    assert response.status_code == 403


def test_device_list_shows_empty_state(app, client, monkeypatch):
    """Admin sees empty state when no devices are registered."""
    monkeypatch.setattr("mizalla.whatsapp.get_gowa_devices", lambda: [])

    with app.app_context():
        admin = make_admin()
        db.session.commit()

    client.post("/apps/login", data={"email": "admin@example.com", "password": "admin-password-long"})
    response = client.get("/apps/whatsapp/devices")
    assert response.status_code == 200
    assert b"No devices found in GOWA yet" in response.data


def test_device_delete_requires_admin(app, client):
    """Non-admin users cannot delete devices."""
    with app.app_context():
        admin = make_admin()
        db.session.commit()

        device = GowaDevice(
            name="Test Device",
            device_id="test-device-123",
            status="active",
            registered_by=admin,
            registered_by_name=admin.name,
        )
        db.session.add(device)
        db.session.commit()
        device_id = device.id

        user = Member(name="User", email="user@example.com")
        user.set_password("user-password-long")
        db.session.add(user)
        db.session.commit()

    client.post("/apps/login", data={"email": "user@example.com", "password": "user-password-long"})
    response = client.post(f"/apps/whatsapp/devices/{device_id}/delete")
    assert response.status_code == 403


def test_device_delete_removes_device(app, client):
    """Admin can delete a device."""
    with app.app_context():
        admin = make_admin()
        db.session.commit()

        device = GowaDevice(
            name="Test Device",
            device_id="test-device-123",
            status="active",
            registered_by=admin,
            registered_by_name=admin.name,
        )
        db.session.add(device)
        db.session.commit()
        device_id = device.id

    client.post("/apps/login", data={"email": "admin@example.com", "password": "admin-password-long"})
    response = client.post(f"/apps/whatsapp/devices/{device_id}/delete")
    assert response.status_code == 200

    with app.app_context():
        assert db.session.get(GowaDevice, device_id) is None


def test_device_fetch_success(app, client, monkeypatch):
    """Admin can fetch devices from GOWA without NameError."""
    monkeypatch.setattr("mizalla.whatsapp.get_gowa_devices", lambda: [{"device_id": "dev1", "name": "GOWA Dev 1", "status": "active"}])

    with app.app_context():
        admin = make_admin()
        db.session.commit()

    client.post("/apps/login", data={"email": "admin@example.com", "password": "admin-password-long"})
    response = client.post("/apps/whatsapp/devices/fetch", follow_redirects=True)
    assert response.status_code == 200
    assert b"Successfully fetched and synced 1 device(s) from GOWA Service." in response.data
    assert b"GOWA Dev 1" in response.data


def test_device_fetch_gowa_offline(app, client, monkeypatch):
    """Fetch redirects and flashes error when GOWA is offline."""
    monkeypatch.setattr("mizalla.whatsapp.check_health", lambda *a, **k: {"online": False, "error": "Connection refused"})

    with app.app_context():
        admin = make_admin()
        db.session.commit()

    client.post("/apps/login", data={"email": "admin@example.com", "password": "admin-password-long"})
    response = client.post("/apps/whatsapp/devices/fetch", follow_redirects=True)
    assert response.status_code == 200
    assert b"GOWA is unavailable" in response.data

