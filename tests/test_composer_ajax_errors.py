"""Tests for the composer AJAX contract: JSON in, JSON out.

The composers post with ``fetch`` and call ``response.json()`` unconditionally, so
any HTML answer surfaced as ``Unexpected token '<', "<!doctype "... is not valid
JSON`` and the real reason was lost. Four distinct paths produced that HTML:

* the Bulk Email form carried no CSRF token, so Flask-WTF rejected the POST;
* ``bulk_email_job.id`` and ``whatsapp_job.id`` are INTEGER primary keys but a
  string token was stored in them, so SQLite answered ``datatype mismatch`` and
  the request fell through to the HTML 500 page;
* an expired session was redirected to the login page, which ``fetch`` followed;
* the styled ``error.html`` page answered 403/404/413/500.

The first two are regressions with their own tests below. The last two are
covered by the JSON-error tests, which pin the contract that an AJAX request gets
JSON back no matter what went wrong.
"""

import io
import re

import pytest

from mizalla import create_app
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    FEATURE_BULK_EMAIL,
    FEATURE_BULK_SMS,
    FEATURE_BULK_WHATSAPP,
    AppSetting,
    Member,
)
from mizalla.settings import BOOLEAN_SETTING_KEYS, set_setting

PASSWORD = "composer-test-password-long"

#: The headers the fixed JS sends, so the server knows to answer in JSON.
AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}


def _csv_bytes(rows):
    import csv

    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _make_member(email, name="Tester", is_admin=False, features=()):
    member = Member(name=name, email=email, is_admin=is_admin)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    if features:
        sync_member_features(member, list(features))
        db.session.commit()
    return member


def _login(client, email):
    return client.post("/apps/login", data={"email": email, "password": PASSWORD})


def _token(client, path):
    """Pull a real CSRF token out of a rendered page."""
    body = client.get(path).get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    return match.group(1) if match else None


@pytest.fixture
def member(app):
    with app.app_context():
        _make_member("tester@example.com", features=[FEATURE_BULK_EMAIL, FEATURE_BULK_SMS, FEATURE_BULK_WHATSAPP])


@pytest.fixture
def logged_in(client, member):
    _login(client, "tester@example.com")
    return client


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Stop the background worker from reaching the network.

    ``send`` returns 202 before the worker runs, so the response is what is under
    test - but the worker would otherwise try SendGrid or Pingram for real. The
    SMS module is patched where the composer imported it, not where it is defined,
    because the composer holds its own binding to the name.
    """
    import mizalla.bulk_email as email_module
    import mizalla.bulk_sms as sms_module
    import mizalla.whatsapp as whatsapp_module

    def refuse(*args, **kwargs):
        raise AssertionError("A test attempted a live network call.")

    monkeypatch.setattr(email_module, "process_job", lambda *a, **k: None)
    monkeypatch.setattr(whatsapp_module, "process_job", lambda *a, **k: None)
    monkeypatch.setattr(sms_module, "send_sms", refuse)


# ─── The CSRF token field ────────────────────────────────────────────────────

def test_every_composer_form_carries_a_csrf_token(logged_in, credentials):
    """Without the field, FormData carries no token and Flask-WTF rejects the POST.

    Bulk SMS and Bulk Messenger already had this, with a comment saying exactly
    that. Bulk Email did not, which is why only it broke.

    The check is scoped to the composer's own form: the base layout carries a
    csrf_token field of its own, so searching the whole page would pass even
    with the composer's field missing.

    Pingram credentials are set because the SMS composer renders an error page
    instead of the form when they are absent.
    """
    credentials.set()
    for path, form_id in (
        ("/apps/emailer", "email-form"),
        ("/apps/sms", "sms-form"),
        ("/apps/whatsapp", "whatsapp-form"),
    ):
        body = logged_in.get(path).get_data(as_text=True)
        form = re.search(r'<form id="%s".*?</form>' % form_id, body, re.S)
        assert form, f"{path} did not render the {form_id} form"
        assert 'name="csrf_token"' in form.group(0), f"{path} form has no csrf_token field"


# ─── The integer job id ──────────────────────────────────────────────────────

def test_bulk_email_send_returns_an_integer_job_id(logged_in):
    """``bulk_email_job.id`` is INTEGER; a string token broke every send.

    SQLite answers ``datatype mismatch``, the request 500s, and the HTML error
    page reaches ``response.json()`` - the reported error.
    """
    response = logged_in.post(
        "/apps/emailer/send",
        data={
            "subject": "Hello",
            "body": "Hi {{ NAME }}",
            "sender": "noreply@example.com",
            "email_list": (io.BytesIO(_csv_bytes([["EMAIL", "NAME"], ["a@b.com", "Amina"]])), "list.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202, response.get_data(as_text=True)
    job_id = response.get_json()["job_id"]
    assert isinstance(job_id, int), f"job_id must be an integer, got {type(job_id).__name__}"
    assert job_id < 2**53 - 1, f"job_id {job_id} exceeds JS Number.MAX_SAFE_INTEGER"


def test_bulk_messenger_send_returns_an_integer_job_id(logged_in, app):
    """``whatsapp_job.id`` is INTEGER too, and had the same string-token bug."""
    from mizalla.models import GowaDevice, GowaDeviceAccess, Member

    with app.app_context():
        member = db.session.scalar(db.select(Member).filter_by(email="tester@example.com"))
        device = GowaDevice(name="Test device", device_id="97411111111", registered_by_id=member.id)
        db.session.add(device)
        db.session.flush()
        db.session.add(GowaDeviceAccess(device_id=device.id, member_id=member.id, granted_by_id=member.id))
        db.session.commit()
        device_id = device.id

    response = logged_in.post(
        "/apps/whatsapp/send",
        data={
            "device_id": str(device_id),
            "message": "Hi {{ NAME }}",
            "phone_list": (io.BytesIO(_csv_bytes([["PHONE", "NAME"], ["97411111111", "Amina"]])), "list.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202, response.get_data(as_text=True)
    job_id = response.get_json()["job_id"]
    assert isinstance(job_id, int), f"job_id must be an integer, got {type(job_id).__name__}"
    assert job_id < 2**53 - 1, f"job_id {job_id} exceeds JS Number.MAX_SAFE_INTEGER"


def test_bulk_sms_send_still_returns_a_string_job_id(logged_in, credentials):
    """``sms_job.id`` is VARCHAR(48), so SMS was never broken by this.

    Pinned so a future "fix" that makes every job id an integer does not silently
    change the SMS contract.
    """
    credentials.set()
    response = logged_in.post(
        "/apps/sms/send",
        data={
            "message": "Hello",
            "phone_list": (io.BytesIO(_csv_bytes([["PHONE"], ["97411111111"]])), "list.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202, response.get_data(as_text=True)
    job_id = response.get_json()["job_id"]
    assert isinstance(job_id, str) and job_id


# ─── AJAX requests get JSON ───────────────────────────────────────────────────

def test_a_missing_csrf_token_gets_json_not_html(logged_in, app):
    """The original symptom: Flask-WTF's HTML 400 page.

    CSRF is disabled in the shared ``app`` fixture, so this builds an app with it
    on - the production configuration, and the one that produced the report.
    """
    client = _csrf_client(app)
    response = client.post(
        "/apps/emailer/send",
        data={"subject": "Hi", "body": "Hi", "sender": "noreply@example.com"},
        content_type="multipart/form-data",
        headers=AJAX,
    )
    assert response.status_code == 400
    assert response.is_json, "an AJAX request must get JSON, not an HTML error page"
    assert "session" in response.get_json()["error"].casefold()


def test_an_expired_session_gets_json_not_a_login_redirect(app, member):
    """``fetch`` follows the redirect to the login page and reads it as the body."""
    client = app.test_client()
    response = client.post(
        "/apps/emailer/send",
        data={"subject": "Hi", "body": "Hi", "sender": "noreply@example.com"},
        content_type="multipart/form-data",
        headers=AJAX,
    )
    assert response.status_code == 401
    assert response.is_json
    assert "session" in response.get_json()["error"].casefold()


def test_a_missing_job_gets_json_not_html(logged_in):
    response = logged_in.get("/apps/emailer/jobs/999999", headers=AJAX)
    assert response.status_code == 404
    assert response.is_json
    assert response.get_json()["error"]


def test_an_oversized_upload_gets_json_not_html(logged_in):
    big = io.BytesIO(b"x" * (17 * 1024 * 1024))
    response = logged_in.post(
        "/apps/emailer/send",
        data={
            "subject": "Hi",
            "body": "Hi",
            "sender": "noreply@example.com",
            "email_list": (io.BytesIO(_csv_bytes([["EMAIL"], ["a@b.com"]])), "list.csv"),
            "attachments": (big, "big.pdf"),
        },
        content_type="multipart/form-data",
        headers=AJAX,
    )
    assert response.status_code == 413
    assert response.is_json
    assert "16 MB" in response.get_json()["error"]


# ─── Browsers still get the styled page ───────────────────────────────────────

def test_a_browser_still_gets_the_styled_error_page(logged_in):
    """The JSON contract must not cost a browser the branded page.

    A non-integer job id is used because a valid integer reaches the view, which
    answers its own JSON 404 - the error handler is a different path.
    """
    response = logged_in.get("/apps/emailer/jobs/abc")
    assert response.status_code == 404
    assert not response.is_json
    assert b"<!doctype" in response.data


# ─── The TLS verification flag ────────────────────────────────────────────────

def test_the_tls_flag_is_a_boolean_setting():
    assert "azure_skip_tls_verify" in BOOLEAN_SETTING_KEYS


def test_tls_verification_disabled_reads_the_setting(app):
    from mizalla.azure_ai import tls_verification_disabled

    with app.app_context():
        # Default is "true" - skip verification - so a network with a
        # TLS-inspecting proxy works out of the box.
        db.session.add(AppSetting(key="azure_skip_tls_verify", value="true"))
        db.session.commit()
        assert tls_verification_disabled() is True

        set_setting("azure_skip_tls_verify", "false")
        assert tls_verification_disabled() is False


def test_openai_client_kwargs_follow_the_setting(app):
    """The OpenAI SDK speaks HTTP through httpx."""
    from mizalla.azure_ai import openai_client_kwargs

    with app.app_context():
        set_setting("azure_skip_tls_verify", "false")
        assert openai_client_kwargs() == {}

        set_setting("azure_skip_tls_verify", "true")
        kwargs = openai_client_kwargs()
        assert "http_client" in kwargs


def test_azure_core_transport_follows_the_setting(app):
    """The azure-core SDKs (Vision, Speech) speak HTTP through requests."""
    from mizalla.azure_ai import azure_core_transport

    with app.app_context():
        set_setting("azure_skip_tls_verify", "false")
        assert azure_core_transport() is None

        set_setting("azure_skip_tls_verify", "true")
        transport = azure_core_transport()
        assert transport is not None
        assert transport.session.verify is False


def test_the_settings_page_renders_the_flag_as_a_checkbox(app, client):
    """An admin edits this in the UI, so it has to be editable there."""
    with app.app_context():
        _make_member("admin@example.com", name="Admin", is_admin=True)

    _login(client, "admin@example.com")
    body = client.get("/apps/admin/settings").get_data(as_text=True)
    assert "azure_skip_tls_verify" in body
    assert 'type="checkbox"' in body


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _csrf_client(app):
    """A logged-in client for an app with CSRF protection on.

    The shared ``app`` fixture disables CSRF so the rest of the suite can post
    without a token; these tests are about what happens when it is on.
    """
    from mizalla.models import FEATURE_BULK_EMAIL as FEATURE

    csrf_app = create_app({
        "TESTING": True,
        "SECRET_KEY": "csrf-test-key",
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "UPLOAD_FOLDER": str(app.config["UPLOAD_FOLDER"]),
        "WTF_CSRF_ENABLED": True,
    })
    with csrf_app.app_context():
        db.create_all()
        _make_member("csrf@example.com", features=[FEATURE])
    client = csrf_app.test_client()
    _login(client, "csrf@example.com")
    return client