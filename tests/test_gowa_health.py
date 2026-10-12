"""Tests for the GOWA health probe and the offline behaviour it drives.

The devices page must report when GOWA is unreachable and must refuse to
register new devices while it is down. Registration is only possible with a
live GOWA server, so the guard is enforced server-side; the disabled button
is a convenience, not the control.
"""

import io
import urllib.error

import pytest

from mizalla.extensions import db
from mizalla.models import Member

PASSWORD = "health-password-long"
URL = "http://127.0.0.1:3000"


class FakeResponse:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_admin(email="admin@example.com"):
    admin = Member(name="Admin", email=email, is_admin=True)
    admin.set_password(PASSWORD)
    db.session.add(admin)
    db.session.commit()
    return admin


def login(client, email="admin@example.com"):
    return client.post("/apps/login", data={"email": email, "password": PASSWORD})


def offline(**overrides):
    payload = {
        "online": False,
        "status_code": None,
        "url": URL,
        "error": f"Could not reach GOWA at {URL} (connection refused).",
    }
    payload.update(overrides)
    return payload


# ─── check_health() ──────────────────────────────────────────────────────────
# These call get_mcp_base_url(), which reads hot settings from the database,
# so each one needs an application context.

def test_health_is_online_when_base_url_returns_200(app, monkeypatch):
    """A 200 from the base URL means GOWA is up."""
    from mizalla import gowa

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return FakeResponse(200)

    monkeypatch.setattr(gowa.urllib.request, "urlopen", fake_urlopen)

    with app.app_context():
        result = gowa.check_health()
    assert result["online"] is True
    assert result["status_code"] == 200
    assert result["error"] is None
    assert seen["url"] == URL
    # Must be a GET: /mcp is POST-only and would answer 405.
    assert seen["method"] == "GET"


def test_health_is_online_for_other_2xx(app, monkeypatch):
    """Any 2xx counts as reachable."""
    from mizalla import gowa

    monkeypatch.setattr(gowa.urllib.request, "urlopen", lambda req, timeout=None: FakeResponse(204))
    with app.app_context():
        assert gowa.check_health()["online"] is True


def test_health_is_offline_when_connection_refused(app, monkeypatch):
    """A refused connection is reported as offline, not raised."""
    from mizalla import gowa

    def refuse(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(gowa.urllib.request, "urlopen", refuse)

    with app.app_context():
        result = gowa.check_health()
    assert result["online"] is False
    assert result["status_code"] is None
    assert "Could not reach GOWA" in result["error"]
    assert result["url"] == URL


def test_health_is_offline_on_http_error_status(app, monkeypatch):
    """A non-2xx response is offline and carries the status code."""
    from mizalla import gowa

    def http_error(req, timeout=None):
        raise urllib.error.HTTPError(URL, 502, "Bad Gateway", {}, io.BytesIO(b""))

    monkeypatch.setattr(gowa.urllib.request, "urlopen", http_error)

    with app.app_context():
        result = gowa.check_health()
    assert result["online"] is False
    assert result["status_code"] == 502
    assert "502" in result["error"]


def test_health_is_offline_on_timeout(app, monkeypatch):
    """Timeouts are surfaced as offline rather than propagating."""
    from mizalla import gowa

    def timeout(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(gowa.urllib.request, "urlopen", timeout)
    with app.app_context():
        assert gowa.check_health()["online"] is False


# ─── Devices page reporting ─────────────────────────────────────────────────

def test_devices_page_reports_gowa_online(app, client, monkeypatch):
    with app.app_context():
        make_admin()

    monkeypatch.setattr(
        "mizalla.whatsapp.check_health",
        lambda *a, **k: {"online": True, "status_code": 200, "url": URL, "error": None},
    )
    monkeypatch.setattr(
        "mizalla.whatsapp.get_connection_status",
        lambda *a, **k: {"error": "Not connected"},
    )

    login(client)
    body = client.get("/apps/whatsapp/devices").data.decode()
    assert '<p class="flash flash-success">' in body
    assert "GOWA is online" in body
    assert '<p class="flash flash-error">' not in body


def test_devices_page_reports_gowa_offline(app, client, monkeypatch):
    with app.app_context():
        make_admin()

    monkeypatch.setattr("mizalla.whatsapp.check_health", lambda *a, **k: offline())

    login(client)
    response = client.get("/apps/whatsapp/devices")
    body = response.data.decode()

    assert response.status_code == 200
    assert '<p class="flash flash-error">' in body
    assert "GOWA is offline" in body
    assert "Could not reach GOWA" in body


def test_devices_page_disables_fetch_when_offline(app, client, monkeypatch):
    """The fetch button is disabled while GOWA is down."""
    with app.app_context():
        make_admin()

    monkeypatch.setattr("mizalla.whatsapp.check_health", lambda *a, **k: offline())

    login(client)
    body = client.get("/apps/whatsapp/devices").data.decode()

    assert "Fetch Devices from GOWA" in body
    assert 'button class="button button-quiet button-small" type="submit" disabled' in body


def test_devices_page_keeps_fetch_enabled_when_online(app, client, monkeypatch):
    with app.app_context():
        make_admin()

    monkeypatch.setattr(
        "mizalla.whatsapp.check_health",
        lambda *a, **k: {"online": True, "status_code": 200, "url": URL, "error": None},
    )

    login(client)
    body = client.get("/apps/whatsapp/devices").data.decode()
    assert "Fetch Devices from GOWA" in body
    assert 'button class="button button-quiet button-small" type="submit"' in body
    assert 'type="submit" disabled' not in body
