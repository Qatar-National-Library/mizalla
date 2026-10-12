"""Tests for service audit logging and audit trail admin features."""

import json
from mizalla.extensions import db
from mizalla.models import ServiceAuditLog, Member, FEATURE_VISION, FEATURE_SPEECH
from mizalla.service_audit import record_service_audit

PASSWORD = "admin-password-123"


def make_admin(app, email="admin@example.com"):
    member = Member(name="Admin User", email=email, is_admin=True)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def test_record_service_audit_creates_entry(app, client):
    """Test that record_service_audit creates a DB record with actor and json payload."""
    with app.app_context():
        member = make_admin(app)
        record_service_audit(
            service_key=FEATURE_VISION,
            action="analyze_image",
            actor=member,
            request_data={"filename": "test.jpg", "size": 100},
            response_data={"caption": "A test image"},
            status="SUCCESS",
            duration_ms=42,
        )
        log = ServiceAuditLog.query.order_by(ServiceAuditLog.id.desc()).first()
        assert log is not None
        assert log.service_key == FEATURE_VISION
        assert log.action == "analyze_image"
        assert log.actor_id == member.id
        assert log.actor_name == member.name
        assert log.actor_email == member.email
        assert log.status == "SUCCESS"
        assert log.duration_ms == 42
        req_json = json.loads(log.request_data)
        assert req_json["filename"] == "test.jpg"
        res_json = json.loads(log.response_data)
        assert res_json["caption"] == "A test image"


def test_admin_service_audit_view_and_export(app, client):
    """Test the admin service audit page and CSV/Excel export routes."""
    with app.app_context():
        admin = make_admin(app)
        record_service_audit(
            service_key=FEATURE_SPEECH,
            action="synthesize",
            actor=admin,
            request_data={"text": "Hello world"},
            response_data={"clip": "clip.mp3"},
            status="SUCCESS",
            duration_ms=15,
        )

    # Sign in as admin
    client.post("/apps/login", data={"email": "admin@example.com", "password": PASSWORD})

    # Access /apps/admin/service-audit
    res = client.get("/apps/admin/service-audit")
    assert res.status_code == 200
    assert b"Service Audit Log" in res.data
    assert b"synthesize" in res.data

    # Export CSV
    res_csv = client.get("/apps/admin/service-audit/export?format=csv")
    assert res_csv.status_code == 200
    assert res_csv.mimetype == "text/csv"
    assert b"Timestamp" in res_csv.data
    assert b"synthesize" in res_csv.data

    # Export Excel
    res_xlsx = client.get("/apps/admin/service-audit/export?format=xlsx")
    assert res_xlsx.status_code == 200
    assert res_xlsx.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
