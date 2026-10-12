"""Tests for the Bulk SMS service.

The one-SMS limit is the part of this service with real logic in it, and the
part most likely to be wrong in a way a page render would not catch, so it is
tested directly rather than only through the composer. ``GSM-7`` allows 160
characters and ``UCS-2`` only 70, the alphabet decides which, and the
substitution step can push a message that was within budget at submission time
over it for one recipient - all three are covered below.

Every test that would reach the network is stopped at :func:`_no_network`, so a
mistake here fails as an assertion rather than sending real SMS to a real
number.
"""

import io
from pathlib import Path

import pytest

from mizalla import create_app
from mizalla.bulk_sms import (
    GSM_7_PER_SEGMENT,
    UCS_2_PER_SEGMENT,
    fits_one_sms,
    limit_message,
    read_recipients,
    segment_count,
    sms_encoding,
)
from mizalla.extensions import db
from mizalla.models import FEATURE_BULK_SMS, Member, SmsJob

PASSWORD = "sms-test-password-long"


# ─── The one-SMS limit ────────────────────────────────────────────────────────

def test_latin_text_uses_the_gsm7_alphabet():
    assert sms_encoding("Your appointment is tomorrow at 3pm") == "GSM-7"


def test_arabic_text_falls_back_to_the_wide_alphabet():
    # Arabic is the case this limit exists for: the same sentence costs more
    # than twice as many characters.
    assert sms_encoding("موعدك غدا الساعة") == "UCS-2"


def test_an_emoji_alone_is_enough_to_force_the_wide_alphabet():
    # A single character can flip the encoding for the whole message, which is
    # why the limit cannot be a fixed number.
    assert sms_encoding("Thanks! 🎉") == "UCS-2"


def test_latin_text_fits_one_sms_at_the_full_budget():
    assert fits_one_sms("a" * GSM_7_PER_SEGMENT) is True
    assert fits_one_sms("a" * (GSM_7_PER_SEGMENT + 1)) is False


def test_arabic_text_fits_one_sms_at_only_seventy_characters():
    assert fits_one_sms("ا" * UCS_2_PER_SEGMENT) is True
    assert fits_one_sms("ا" * (UCS_2_PER_SEGMENT + 1)) is False


def test_seventy_one_latin_characters_are_still_one_sms():
    # The point of encoding-aware measurement: the same character count that is
    # fine for Latin is too long for Arabic.
    assert fits_one_sms("a" * 100) is True


def test_an_extension_character_costs_a_second_septet():
    # '{}' and friends encode as an escape plus the character, so they eat into
    # the 160-character budget. Counting them as one character each would let a
    # message through that the network splits into two.
    #
    # 159 plain characters fit the budget exactly; adding an extension character
    # is the 160th character but the 161st septet, so it overflows even though
    # len() still reads 160.
    fits = "a" * (GSM_7_PER_SEGMENT - 1)
    assert len(fits) == 159
    assert fits_one_sms(fits) is True

    overflows = fits + "{"
    assert sms_encoding(overflows) == "GSM-7"
    assert len(overflows) == GSM_7_PER_SEGMENT
    assert fits_one_sms(overflows) is False

    # Dropping one plain character makes room for the two-septet one.
    assert fits_one_sms(fits[:-1] + "{") is True


def test_an_empty_message_is_not_a_valid_sms():
    # Nothing to send is a different failure from "too long", and the caller
    # checks for it separately.
    assert fits_one_sms("") is False
    assert fits_one_sms("   ") is False


def test_segment_count_reports_what_a_longer_message_would_cost():
    assert segment_count("") == 0
    assert segment_count("a" * 160) == 1
    assert segment_count("a" * 161) == 2
    assert segment_count("ا" * 140) == 2


def test_limit_message_names_the_encoding_and_the_budget():
    latin = limit_message("a" * 200)
    assert "2 SMS" in latin
    assert "160" in latin

    arabic = limit_message("ا" * 80)
    assert "70" in arabic
    assert "Arabic" in arabic


def test_limit_message_is_empty_for_a_message_that_fits():
    assert limit_message("Your code is 4821") == ""


# ─── Variable replacement ────────────────────────────────────────────────────

def _csv_bytes(rows):
    """An uploaded CSV list, as the composer receives it.

    Values are written as text, which is the point of the format: the same list
    opened as a spreadsheet hands ``97411111111`` back as ``97411111111.0``.
    """
    lines = [",".join("" if cell is None else str(cell) for cell in row) for row in rows]
    return io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


def test_read_recipients_returns_phones_and_row_data():
    stream = _csv_bytes([
        ["PHONE", "NAME", "CODE"],
        ["+97411111111", "Amina", "A1"],
        ["+97422222222", "Bilal", "B2"],
    ])
    recipients, rows = read_recipients(stream)
    assert recipients == ["+97411111111", "+97422222222"]
    assert rows[0]["name"] == "Amina"
    assert rows[0]["code"] == "A1"


def test_read_recipients_needs_a_phone_column():
    with pytest.raises(ValueError, match="PHONE"):
        read_recipients(_csv_bytes([["EMAIL"], ["a@b.com"]]))


def test_read_recipients_keeps_a_long_number_intact():
    """A number this long is exactly what a spreadsheet would round."""
    digits = "9" * 17
    recipients, _ = read_recipients(_csv_bytes([["PHONE"], [digits]]))
    assert recipients == [digits]


def test_read_recipients_accepts_the_delimiter_excel_actually_writes():
    for delimiter in (",", ";", "\t"):
        stream = io.BytesIO(f"PHONE{delimiter}NAME\n+97411111111{delimiter}Amina\n".encode())
        recipients, rows = read_recipients(stream)
        assert recipients == ["+97411111111"], delimiter
        assert rows[0]["name"] == "Amina", delimiter


def test_read_recipients_accepts_the_encodings_excel_actually_writes():
    text = "PHONE,NAME\n+97411111111,Søren\n"
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp1252"):
        stream = io.BytesIO(text.encode(encoding))
        recipients, rows = read_recipients(stream)
        assert recipients == ["+97411111111"], encoding
        assert rows[0]["name"] == "Søren", encoding


def test_read_recipients_does_not_mistake_a_semicolon_inside_a_field_for_a_delimiter():
    stream = _csv_bytes([["PHONE", "NOTE"], ["+97411111111", "a;b;c"]])
    recipients, rows = read_recipients(stream)
    assert recipients == ["+97411111111"]
    assert rows[0]["note"] == "a;b;c"


def test_send_rejects_a_spreadsheet_list(app, logged_in):
    """The composers take CSV now, so an .xlsx upload is refused by name."""
    response = logged_in.post(
        "/apps/sms/send",
        data={
            "message": "Hello",
            # BytesIO, not raw bytes: Werkzeug drops a multipart part whose stream
            # is a bytes object, so it would never reach request.files at all.
            "phone_list": (io.BytesIO(b"PK\x03\x04not really a csv"), "list.xlsx"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "The phone list must be a .csv file."


def test_substitution_replaces_known_columns_and_blanks_the_rest():
    from mizalla.bulk_sms import _substitute_template

    text = _substitute_template("Hi {{ name }}, your code is {{ code }} {{ missing }}.",
                                {"name": "Amina", "code": "4821"})
    assert text == "Hi Amina, your code is 4821 ."


# ─── The web layer ───────────────────────────────────────────────────────────

def _make_member(email, is_admin=False, granted=False):
    """Create a member. ``granted`` and ``is_admin`` are separate concerns."""
    from mizalla.feature_access import sync_member_features

    member = Member(name="SMS Tester", email=email, is_admin=is_admin)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    if granted:
        sync_member_features(member, [FEATURE_BULK_SMS])
        db.session.commit()
    return member


@pytest.fixture
def granted(app):
    """A plain member holding the Bulk SMS grant — not an admin.

    Deliberately not an admin: ``has_feature_access`` short-circuits to True for
    administrators, so an admin cannot exercise the grant path at all.
    """
    with app.app_context():
        _make_member("sms@example.com", granted=True)


@pytest.fixture
def ungranted(app):
    """A plain member with no grant. Used to prove the guard actually guards."""
    with app.app_context():
        _make_member("nogive@example.com")


@pytest.fixture
def admin(app):
    """An administrator, who sees every service regardless of grants."""
    with app.app_context():
        _make_member("admin@example.com", is_admin=True)


def _login(client, email):
    return client.post("/apps/login", data={"email": email, "password": PASSWORD})


@pytest.fixture
def logged_in(client, granted, credentials):
    credentials.set()
    _login(client, "sms@example.com")
    return client


@pytest.fixture
def admin_client(client, admin, credentials):
    credentials.set()
    _login(client, "admin@example.com")
    return client


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if any test tries to reach Pingram.

    Patched on ``bulk_sms`` rather than on ``pingram`` because the composer
    imports ``send_sms`` into its own namespace, so replacing the name in
    ``pingram`` alone would leave the real function bound in place and the guard
    would never fire.
    """
    import mizalla.bulk_sms as sms_module

    def refuse(*args, **kwargs):
        raise AssertionError("A test attempted a live Pingram call.")

    monkeypatch.setattr(sms_module, "send_sms", refuse)


def _phone_list(phones=(("+97411111111", "Amina"),)):
    return _csv_bytes([["PHONE", "NAME"]] + [list(row) for row in phones])


def test_composer_renders(app, logged_in):
    response = logged_in.get("/apps/sms")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Bulk SMS" in body
    assert 'id="sms-form"' in body


def test_page_is_refused_without_the_grant(app, client, ungranted, credentials):
    credentials.set()
    _login(client, "nogive@example.com")
    assert client.get("/apps/sms").status_code == 403


def test_page_reports_missing_credentials_rather_than_failing(app, client, granted, credentials):
    """No .env keys means a 503 explaining what is missing, not a blank page.

    A member with the grant would otherwise reach a composer whose every send
    fails, with no indication that the deployment is the problem.
    """
    credentials.set(pingram_api_key="")
    _login(client, "sms@example.com")
    response = client.get("/apps/sms")
    assert response.status_code == 503
    assert "PINGRAM_API_KEY" in response.get_data(as_text=True)


def _nav_labels(client, page="/apps/admin/sms-audit"):
    """The service labels the header actually rendered.

    Driven through a real page rather than calling ``nav_services`` directly,
    because that calls ``url_for`` and needs a request context to resolve.
    """
    import re

    body = client.get(page).get_data(as_text=True)
    switcher = re.search(r'<div class="app-switcher">(.*?)</div>', body, re.DOTALL)
    assert switcher, "no app switcher in the rendered header"
    return switcher.group(1)


def test_header_shows_the_link_to_a_granted_member(app, logged_in):
    header = _nav_labels(logged_in)
    assert "/apps/sms" in header
    assert ">SMS<" in header


def test_header_omits_the_link_without_the_grant(app, client, ungranted, credentials):
    credentials.set()
    _login(client, "nogive@example.com")
    # Any page will do; the audit page is admin-only so use the project list.
    header = _nav_labels(client, page="/")
    assert "/apps/sms" not in header


def test_send_rejects_a_message_longer_than_one_sms(app, logged_in):
    response = logged_in.post(
        "/apps/sms/send",
        data={
            "message": "ا" * (UCS_2_PER_SEGMENT + 1),
            "phone_list": (_phone_list(), "list.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "SMS" in response.get_json()["error"]


def test_send_requires_a_message(app, logged_in):
    response = logged_in.post(
        "/apps/sms/send",
        data={"message": "   ", "phone_list": (_phone_list(), "list.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "Message is required."


def test_send_requires_a_csv_list_with_phones(app, logged_in):
    empty = _csv_bytes([["PHONE"], [None]])
    response = logged_in.post(
        "/apps/sms/send",
        data={"message": "Hello", "phone_list": (empty, "list.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_send_rejects_an_invalid_phone_without_contacting_pingram(app, logged_in, monkeypatch):
    """A malformed number is caught locally and the campaign still completes."""
    import mizalla.bulk_sms as sms_module

    sent = []

    def record(phone, message, sender=None, **kwargs):
        sent.append(phone)
        return {"tracking_id": "track-1"}

    monkeypatch.setattr(sms_module, "send_sms", record)

    response = logged_in.post(
        "/apps/sms/send",
        data={
            "message": "Hello {{ name }}",
            "phone_list": (_phone_list([("not-a-number", "X")]), "list.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]

    _wait_for(sms_module, job_id)
    assert sent == []
    with app.app_context():
        recipient = db.session.get(SmsJob, job_id).recipients[0]
        assert recipient.status == "ERROR"
        assert "Invalid phone number" in recipient.reason


def test_a_substituted_message_that_overflows_one_sms_is_reported_not_sent(app, logged_in, monkeypatch):
    """The limit is per recipient, because substitution can lengthen the text.

    The template itself is comfortably inside one SMS, so only the rendered
    message is over the budget. Sending it would bill the recipient for two.
    """
    import mizalla.bulk_sms as sms_module

    sent = []

    def record(phone, message, sender=None, **kwargs):
        sent.append(phone)
        return {"tracking_id": "track-1"}

    monkeypatch.setattr(sms_module, "send_sms", record)

    # A short template whose one placeholder expands past 160 characters.
    template = "Code: {{ long }}"
    assert fits_one_sms(template) is True

    response = logged_in.post(
        "/apps/sms/send",
        data={
            "message": template,
            "phone_list": (_csv_bytes([
                ["PHONE", "LONG"],
                ["+97411111111", "x" * 200],
            ]), "list.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]

    _wait_for(sms_module, job_id)
    assert sent == []
    with app.app_context():
        recipient = db.session.get(SmsJob, job_id).recipients[0]
        assert recipient.status == "ERROR"
        assert "rendered message" in recipient.reason


def test_a_successful_campaign_records_one_result_per_recipient(app, logged_in, monkeypatch):
    import mizalla.bulk_sms as sms_module

    def record(phone, message, sender=None, **kwargs):
        assert "{{" not in message, "the template must be substituted before sending"
        return {"tracking_id": f"track-{phone[-4:]}"}

    monkeypatch.setattr(sms_module, "send_sms", record)

    response = logged_in.post(
        "/apps/sms/send",
        data={
            "message": "Hello {{ name }}",
            "sender": "+97430000000",
            "phone_list": (_phone_list([("+97411111111", "Amina"), ("+97422222222", "Bilal")]), "list.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    job_id = response.get_json()["job_id"]

    snapshot = _wait_for(sms_module, job_id)
    assert snapshot["complete"] is True
    assert snapshot["sent"] == 2
    assert snapshot["errors"] == 0

    with app.app_context():
        job = db.session.get(SmsJob, job_id)
        assert job.sender == "+97430000000"
        assert job.region == "us"
        assert [r.status for r in job.recipients] == ["SENT", "SENT"]
        assert job.recipients[0].tracking_id == "track-1111"


def test_one_failed_recipient_does_not_stop_the_campaign(app, logged_in, monkeypatch):
    import mizalla.bulk_sms as sms_module

    def record(phone, message, sender=None, **kwargs):
        if phone.endswith("2222"):
            return {"error": "Destination is not reachable."}
        return {"tracking_id": "track-ok"}

    monkeypatch.setattr(sms_module, "send_sms", record)

    response = logged_in.post(
        "/apps/sms/send",
        data={
            "message": "Hello {{ name }}",
            "phone_list": (_phone_list([("+97411111111", "Amina"), ("+97422222222", "Bilal")]), "list.csv"),
        },
        content_type="multipart/form-data",
    )
    job_id = response.get_json()["job_id"]
    snapshot = _wait_for(sms_module, job_id)

    assert snapshot["sent"] == 1
    assert snapshot["errors"] == 1
    with app.app_context():
        job = db.session.get(SmsJob, job_id)
        failures = [r for r in job.recipients if r.status == "ERROR"]
        assert "not reachable" in failures[0].reason


def test_a_member_without_the_grant_cannot_start_a_campaign(app, client, ungranted, credentials):
    credentials.set()
    _login(client, "nogive@example.com")
    response = client.post(
        "/apps/sms/send",
        data={"message": "Hello", "phone_list": (_phone_list(), "list.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 403


def test_job_status_is_not_readable_without_the_grant(app, client, ungranted, credentials):
    credentials.set()
    _login(client, "nogive@example.com")
    assert client.get("/apps/sms/jobs/anything").status_code == 403


def test_a_member_without_the_grant_cannot_export_a_campaign(app, client, ungranted, credentials):
    credentials.set()
    _login(client, "nogive@example.com")
    assert client.get("/apps/sms/jobs/anything/export").status_code == 403


def test_unknown_job_is_reported_as_missing(app, logged_in):
    assert logged_in.get("/apps/sms/jobs/nope").status_code == 404


def test_export_downloads_a_workbook_of_results(app, logged_in, monkeypatch):
    import mizalla.bulk_sms as sms_module
    monkeypatch.setattr(sms_module, "send_sms", lambda *a, **k: {"tracking_id": "t1"})

    job_id = logged_in.post(
        "/apps/sms/send",
        data={"message": "Hello", "phone_list": (_phone_list(), "list.csv")},
        content_type="multipart/form-data",
    ).get_json()["job_id"]
    _wait_for(sms_module, job_id)

    response = logged_in.get(f"/apps/sms/jobs/{job_id}/export")
    assert response.status_code == 200
    assert "spreadsheetml" in response.mimetype
    assert response.headers["Content-Disposition"].endswith(".xlsx")
    # Asserting only the status code and filename is how a 0-byte workbook
    # passes: openpyxl builds the file in memory, so forgetting to save it
    # still produces a well-formed response. The bytes are the assertion.
    assert int(response.headers["Content-Length"]) > 0
    assert response.get_data()[:2] == b"PK"


def test_export_workbook_contains_the_recipient_rows(app, logged_in, monkeypatch):
    """Opens the exported file and reads it back, rather than trusting bytes."""
    import mizalla.bulk_sms as sms_module
    from openpyxl import load_workbook

    monkeypatch.setattr(sms_module, "send_sms", lambda *a, **k: {"tracking_id": "tk-xyz"})

    job_id = logged_in.post(
        "/apps/sms/send",
        data={"message": "Hello", "phone_list": (_phone_list(), "list.csv")},
        content_type="multipart/form-data",
    ).get_json()["job_id"]
    _wait_for(sms_module, job_id)

    raw = logged_in.get(f"/apps/sms/jobs/{job_id}/export").get_data()
    sheet = load_workbook(io.BytesIO(raw)).active
    cells = [str(c.value) for row in sheet.iter_rows() for c in row if c.value]
    assert any("+97411111111" in value for value in cells)
    assert "tk-xyz" in cells


def _csrf(client):
    """The token the rendered composer would submit.

    ``/test`` validates the token explicitly, so a test has to send a real one.
    Read from the page rather than generated, which also proves the template
    actually ships the field the JS posts.
    """
    import re

    body = client.get("/apps/sms").get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert match, "the composer form does not carry a csrf_token field"
    return match.group(1)


def test_the_composer_form_carries_a_csrf_token(app, logged_in):
    # The Test button posts the form via JS; without the field it would always
    # be rejected, so this guards a real failure rather than an implementation
    # detail.
    assert _csrf(logged_in)


def test_test_send_needs_a_configured_test_number(app, logged_in):
    response = logged_in.post("/apps/sms/test", data={
        "message": "Hello", "csrf_token": _csrf(logged_in),
    })
    assert response.status_code == 400
    assert "test phone number" in response.get_json()["error"]


def test_test_send_rejects_a_missing_token(app, logged_in):
    response = logged_in.post("/apps/sms/test", data={"message": "Hello"})
    assert response.status_code == 400
    assert "CSRF" in response.get_json()["error"]


def test_test_send_rejects_an_over_long_message_before_sending(app, logged_in):
    """The test button is the cheapest place to catch an over-budget message."""
    from mizalla.settings import set_setting

    with app.app_context():
        set_setting("test_sms_number", "+97455555555")

    response = logged_in.post("/apps/sms/test", data={
        "message": "a" * 200, "csrf_token": _csrf(logged_in),
    })
    assert response.status_code == 400
    assert "SMS" in response.get_json()["error"]


def test_test_send_sends_and_reports_the_tracking_id(app, logged_in, monkeypatch):
    from mizalla.settings import set_setting

    with app.app_context():
        set_setting("test_sms_number", "+97455555555")

    seen = {}

    def record(phone, message, sender=None, **kwargs):
        seen.update(phone=phone, message=message, sender=sender)
        return {"tracking_id": "track-test"}

    import mizalla.bulk_sms as sms_module
    monkeypatch.setattr(sms_module, "send_sms", record)

    response = logged_in.post("/apps/sms/test", data={
        "message": "Code: 4821", "csrf_token": _csrf(logged_in),
    })
    assert response.status_code == 200
    assert "track-test" in response.get_json()["message"]
    assert seen["phone"] == "+97455555555"


def test_test_send_reports_a_pingram_failure_as_a_bad_gateway(app, logged_in, monkeypatch):
    from mizalla.settings import set_setting

    with app.app_context():
        set_setting("test_sms_number", "+97455555555")

    import mizalla.bulk_sms as sms_module
    monkeypatch.setattr(sms_module, "send_sms", lambda *a, **k: {"error": "Destination is not reachable."})

    response = logged_in.post("/apps/sms/test", data={
        "message": "Hello", "csrf_token": _csrf(logged_in),
    })
    assert response.status_code == 502
    assert response.get_json()["error"] == "Destination is not reachable."


# ─── Navigation and administration ───────────────────────────────────────────

@pytest.mark.parametrize("template,prefix", [
    ("bulk_sms.html", "/apps/sms"),
    ("whatsapp.html", "/apps/whatsapp"),
])
def test_composer_js_posts_only_to_registered_routes(app, template, prefix):
    """Every URL a composer fetches must be a route the app actually serves.

    Catches the bug class, not one instance: a composer whose JS posts to a path
    nothing serves returns 404 at runtime, and the page renders perfectly in a
    template smoke test. ``/whatsapp/send`` sat next to a blueprint mounted at
    ``/apps/whatsapp`` for exactly this long because nothing checked.
    """
    import re

    from mizalla import bulk_sms

    source = (Path(bulk_sms.__file__).parent / "templates" / template).read_text(encoding="utf-8")
    # Both spellings are matched: the composers post through ``composerFetch``, a
    # shared helper that marks the request as AJAX and reads the body as text
    # first, but a bare ``fetch`` is equally valid and must stay checked.
    fetched = re.findall(r'(?:composerFetch|fetch)\(\s*[`"\'](/[^`"\']*)', source)
    assert fetched, f"{template} fetches nothing, so this test proves nothing"

    # A request-path pattern is a URL with any interpolated job id collapsed to
    # a placeholder. Converter segments are collapsed to that same placeholder so
    # a pattern matches a route regardless of whether the service routed the
    # segment as ``<job_id>`` or ``<int:job_id>``.
    def _normalise(path):
        return re.sub(r"<[^>]*>", "<id>", path)

    routes = [(_normalise(rule.rule), rule.methods) for rule in app.url_map.iter_rules()]

    for url in fetched:
        pattern = _normalise(re.sub(r"\$\{[^}]*\}", "<id>", url))
        method = "POST" if pattern.endswith(("/send", "/test")) else "GET"
        assert any(path == pattern and method in methods for path, methods in routes), (
            f"{template} fetches {pattern}, which is not a registered route"
        )
        assert pattern.startswith(prefix), (
            f"{template} fetches {pattern}, outside its own blueprint prefix {prefix}"
        )


def test_admin_audit_page_lists_campaigns(app, admin_client, monkeypatch):
    import mizalla.bulk_sms as sms_module
    monkeypatch.setattr(sms_module, "send_sms", lambda *a, **k: {"tracking_id": "t1"})

    job_id = admin_client.post(
        "/apps/sms/send",
        data={"message": "Hello", "phone_list": (_phone_list(), "list.csv")},
        content_type="multipart/form-data",
    ).get_json()["job_id"]
    _wait_for(sms_module, job_id)

    response = admin_client.get("/apps/admin/sms-audit")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "SMS Audit" in body
    assert job_id in body


def test_admin_audit_exports_to_both_formats(app, admin_client):
    """Both formats must produce a real file.

    These assertions cover two bugs that a status-code check waves through: an
    unsaved openpyxl workbook returns 200 with zero bytes, and writing ``str``
    through ``csv.writer`` into a ``BytesIO`` raises ``TypeError`` outright.
    """
    xlsx = admin_client.get("/apps/admin/sms-audit/export?format=xlsx")
    assert xlsx.status_code == 200
    assert int(xlsx.headers["Content-Length"]) > 0
    assert xlsx.get_data()[:2] == b"PK"

    csv_response = admin_client.get("/apps/admin/sms-audit/export?format=csv")
    assert csv_response.status_code == 200
    assert b"Job ID" in csv_response.get_data()


def test_the_audit_page_is_admin_only(app, logged_in):
    """The audit trail lists every member's recipients, so it is not member-visible."""
    assert logged_in.get("/apps/admin/sms-audit").status_code == 403


def _wait_for(sms_module, job_id, attempts=200):
    """Poll the in-memory job until the worker thread finishes."""
    import time

    for _ in range(attempts):
        snapshot = sms_module.jobs.get(job_id)
        if snapshot and snapshot["complete"]:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"Job {job_id} did not complete.")