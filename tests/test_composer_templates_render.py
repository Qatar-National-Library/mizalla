"""Smoke tests that render the campaign composer templates.

These catch Jinja syntax errors at import/render time. The literal
`@@ COLUMN_NAME @@` and `@@ @@` strings in the composer copy are the
documented placeholder syntax, so the templates must escape them with
{% raw %} — otherwise Jinja evaluates them as expressions and the page
fails to render.
"""

from mizalla.extensions import db
from mizalla.models import Member

PASSWORD = "composer-password-long"


def make_admin(email="admin@example.com"):
    admin = Member(name="Admin", email=email, is_admin=True, can_bulk_email=True)
    admin.set_password(PASSWORD)
    db.session.add(admin)
    db.session.commit()
    return admin


def login(client, email="admin@example.com"):
    return client.post("/apps/login", data={"email": email, "password": PASSWORD})


def test_bulk_emailer_page_renders(app, client):
    """The emailer composer renders without a Jinja error."""
    with app.app_context():
        make_admin()

    login(client)
    response = client.get("/apps/emailer")
    assert response.status_code == 200


def test_whatsapp_composer_page_renders(app, client):
    """The WhatsApp composer renders without a Jinja error."""
    with app.app_context():
        make_admin()

    login(client)
    response = client.get("/apps/whatsapp")
    assert response.status_code == 200


def test_emailer_renders_placeholder_syntax_verbatim(app, client):
    """`@@ COLUMN_NAME @@` reaches the browser as literal text, not an empty string."""
    with app.app_context():
        make_admin()

    login(client)
    body = client.get("/apps/emailer").data.decode()
    assert "@@ COLUMN_NAME @@" in body


def test_emailer_renders_empty_brace_syntax_in_script(app, client):
    """The JS substitution tag keeps its literal `@@ @@` delimiters."""
    with app.app_context():
        make_admin()

    login(client)
    body = client.get("/apps/emailer").data.decode()
    assert '"@@ @@ supported"' in body


def test_whatsapp_renders_placeholder_syntax_verbatim(app, client):
    """`@@ COLUMN_NAME @@` reaches the browser as literal text on WhatsApp too."""
    with app.app_context():
        make_admin()

    login(client)
    body = client.get("/apps/whatsapp").data.decode()
    assert "@@ COLUMN_NAME @@" in body


def test_every_composer_page_renders(app, client):
    """Sweep the campaign app pages to catch syntax errors on any of them."""
    from mizalla.models import GowaDevice

    with app.app_context():
        admin = make_admin()
        device = GowaDevice(
            name="Line 1",
            device_id="dev-1",
            status="active",
            registered_by=admin,
            registered_by_name=admin.name,
        )
        db.session.add(device)
        db.session.commit()
        device_id = device.id

    login(client)
    for path in [
        "/apps/emailer",
        "/apps/whatsapp",
        "/apps/whatsapp/devices",
        f"/apps/whatsapp/devices/{device_id}/access",
        "/apps/admin/bulk-email-audit",
        "/apps/admin/whatsapp-audit",
    ]:
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"


def test_composer_scripts_are_actually_emitted(app, client):
    """base.html must render the {% block scripts %} the composers rely on.

    The campaign composers put all of their behaviour (drag-and-drop, test
    send, campaign submit, progress polling, export) in a scripts block.
    If base.html omits that block the pages render fine but are completely
    inert in the browser, so assert the wiring is present.
    """
    with app.app_context():
        make_admin()

    login(client)
    body = client.get("/apps/emailer").data.decode()
    assert "{% raw %}" not in body  # raw tags are directives, not output
    assert "subTag.textContent" in body
    assert "/apps/emailer/send" in body
    assert 'id="test_recipient"' in body


def test_composer_test_recipient_fields_present_in_all_composers(app, client, credentials):
    """Ensure test_recipient textfield and disabled buttons are present on email, SMS, and WhatsApp forms."""
    credentials.set()
    with app.app_context():
        make_admin()

    login(client)
    for path in ["/apps/emailer", "/apps/sms", "/apps/whatsapp"]:
        body = client.get(path).data.decode()
        assert 'id="test_recipient"' in body, f"test_recipient input missing in {path}"
        assert 'id="test-button"' in body, f"test-button missing in {path}"
        assert 'id="send-button"' in body, f"send-button missing in {path}"


def test_lock_pdf_checkbox_present_and_checked_in_composers(app, client):
    """Ensure lock_substituted_pdf checkbox is present, under attachments, and checked by default."""
    with app.app_context():
        make_admin()

    login(client)
    for path in ["/apps/emailer", "/apps/whatsapp"]:
        body = client.get(path).data.decode()
        assert 'id="lock_substituted_pdf"' in body, f"lock_substituted_pdf checkbox missing in {path}"
        assert 'name="lock_substituted_pdf"' in body
        # Verified checked by default
        assert 'id="lock_substituted_pdf" name="lock_substituted_pdf" value="true" checked' in body
        # Ensure it appears after drop-zone-attachments
        drop_idx = body.find('id="drop-zone-attachments"')
        lock_idx = body.find('id="lock_substituted_pdf"')
        assert drop_idx != -1 and lock_idx != -1 and lock_idx > drop_idx, f"Checkbox must be under attachments panel in {path}"


def test_pdf_text_alignment_selector_present_in_composers(app, client):
    """Ensure pdf_text_alignment select dropdown is present under attachments and defaults to center."""
    with app.app_context():
        make_admin()

    login(client)
    for path in ["/apps/emailer", "/apps/whatsapp"]:
        body = client.get(path).data.decode()
        assert 'id="pdf_text_alignment"' in body, f"pdf_text_alignment select missing in {path}"
        assert 'name="pdf_text_alignment"' in body
        assert '<option value="center" selected>' in body
        assert '<option value="directional">' in body
        assert '<option value="left">' in body
        assert '<option value="right">' in body
        # Ensure it is placed under attachments panel
        drop_idx = body.find('id="drop-zone-attachments"')
        align_idx = body.find('id="pdf_text_alignment"')
        assert drop_idx != -1 and align_idx != -1 and align_idx > drop_idx, f"Alignment select must be under attachments panel in {path}"


