"""Tests for the AI services index page and the disabled-link behaviour.

The requirement is specific: when a service's keys are not defined, its form
must be shown as a disabled link rather than an openable page. These tests
cover both halves of that - the card state on the index, and the 503 the
service page itself returns.
"""

import pytest

from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    FEATURE_CHAT,
    FEATURE_SPEECH,
    FEATURE_VISION,
    Member,
)

PASSWORD = "member-password-long"

CREDENTIALS = {
    "azure_vision_endpoint": "https://vision.cognitiveservices.azure.com",
    "azure_vision_key": "vision-key",
    "azure_speech_key": "speech-key",
    "azure_speech_region": "eastus",
    "azure_openai_endpoint": "https://openai.openai.azure.com",
    "azure_openai_key": "openai-key",
    "azure_openai_deployment": "gpt-deployment",
}

# Map internal credential keys to the env var names the code reads.
_ENV_MAP = {
    "azure_vision_endpoint": "MS_VISION_ENDPOINT",
    "azure_vision_key": "MS_VISION_KEY",
    "azure_speech_key": "MS_SPEECH_KEY",
    "azure_speech_region": "MS_REGION",
    "azure_openai_endpoint": "AZURE_OPENAI_ENDPOINT",
    "azure_openai_key": "AZURE_OPENAI_KEY",
    "azure_openai_deployment": "AZURE_OPENAI_DEPLOYMENT_NAME",
}


def set_credentials(app, **overrides):
    import os

    values = {**CREDENTIALS, **overrides}
    for key, value in values.items():
        os.environ[_ENV_MAP[key]] = value


def clear_credentials(app):
    import os

    for key in CREDENTIALS:
        os.environ.pop(_ENV_MAP[key], None)


def grant_all(app, member):
    sync_member_features(member, [FEATURE_VISION, FEATURE_SPEECH, FEATURE_CHAT])
    db.session.commit()


def login_user(client, email="user@example.com"):
    client.post("/apps/login", data={"email": email, "password": PASSWORD})


def make_user(app, email="user@example.com", **kwargs):
    member = Member(name="User", email=email, **kwargs)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


# ─── Header nav (replaces the old index page) ────────────────────────────────

def test_the_header_shows_services_to_a_granted_member(app, client):
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    login_user(client)
    body = client.get("/apps/mizalla/").get_data(as_text=True)
    assert 'href="/apps/ai/vision"' in body
    assert 'href="/apps/ai/speech"' in body
    assert 'href="/apps/ai/chat"' in body


def test_a_service_without_keys_is_shown_disabled_in_header(app, client):
    with app.app_context():
        member = make_user(app)
        set_credentials(app, azure_vision_key="", azure_openai_key="")
        grant_all(app, member)

    login_user(client)
    body = client.get("/apps/mizalla/").get_data(as_text=True)

    # Vision and Chat are granted but not configured: shown as disabled spans.
    assert 'app-switcher-link-disabled' in body
    assert 'href="/apps/ai/speech"' in body
    assert 'href="/apps/ai/vision"' not in body
    assert 'href="/apps/ai/chat"' not in body
    # The reason names the env var, so the admin knows what to fill in.
    assert "MS_VISION_KEY" in body
    assert "AZURE_OPENAI_KEY" in body


def test_a_granted_service_is_still_disabled_without_keys(app, client):
    """Being granted access does not conjure missing credentials."""
    with app.app_context():
        member = make_user(app)
        clear_credentials(app)
        grant_all(app, member)

    login_user(client)
    body = client.get("/apps/mizalla/").get_data(as_text=True)
    assert 'app-switcher-link-disabled' in body
    assert 'href="/apps/ai/vision"' not in body


def test_an_ungranted_service_is_hidden_entirely(app, client):
    with app.app_context():
        make_user(app)
        set_credentials(app)

    login_user(client)
    body = client.get("/apps/mizalla/").get_data(as_text=True)
    assert 'href="/apps/ai/vision"' not in body
    assert 'href="/apps/ai/speech"' not in body
    assert 'href="/apps/ai/chat"' not in body


def test_the_index_does_not_leak_configuration_state_to_strangers(app, client):
    """A member with no grants should see nothing about missing keys."""
    with app.app_context():
        make_user(app)
        set_credentials(app, azure_vision_key="")

    login_user(client)
    body = client.get("/apps/ai").get_data(as_text=True)
    # No grant, so the card reports the access reason, not the key names.
    assert "Azure Vision key" not in body


@pytest.mark.parametrize("feature", [FEATURE_VISION, FEATURE_SPEECH, FEATURE_CHAT])
def test_the_index_survives_a_broken_settings_table(app, client, monkeypatch, feature):
    """One unreadable service must not take down the whole page."""
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    def explode(key):
        # Break the credential for this specific feature key
        return ""

    monkeypatch.setattr("mizalla.azure_ai.credential", explode)
    login_user(client)
    # /apps/ai now redirects to the first available service; a 302 is a valid response.
    assert client.get("/apps/ai").status_code == 302


# ─── Service pages ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path, missing_key",
    [
        ("/apps/ai/vision", "azure_vision_key"),
        ("/apps/ai/speech", "azure_speech_region"),
        ("/apps/ai/chat", "azure_openai_deployment"),
    ],
)
def test_a_service_page_returns_503_and_names_the_missing_setting(app, client, path, missing_key):
    with app.app_context():
        member = make_user(app)
        set_credentials(app, **{missing_key: ""})
        grant_all(app, member)

    login_user(client)
    response = client.get(path)
    assert response.status_code == 503
    body = response.get_data(as_text=True)
    assert "not available" in body.lower()
    # The upload form must not be rendered, because submitting it could only fail.
    assert 'type="file"' not in body


def test_a_configured_service_page_renders_its_form(app, client):
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    login_user(client)
    assert client.get("/apps/ai/vision").status_code == 200
    assert client.get("/apps/ai/speech").status_code == 200
    assert client.get("/apps/ai/chat").status_code == 200


def test_the_vision_form_uses_a_drop_target(app, client):
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    login_user(client)
    body = client.get("/apps/ai/vision").get_data(as_text=True)
    assert 'class="file-picker drop-zone"' in body
    assert 'id="drop-zone-image"' in body
    assert "dragover" in body


def test_the_chat_upload_form_uses_a_drop_target(app, client):
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    login_user(client)
    body = client.get("/apps/ai/chat").get_data(as_text=True)
    assert 'id="drop-zone-document"' in body
    assert "dragover" in body


def test_the_speech_page_replays_the_last_clip(app, client, monkeypatch):
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    login_user(client)
    assert 'class="audio-player"' not in client.get("/apps/ai/speech").get_data(as_text=True)

    clip = {"name": f"{1}-{'a' * 32}.mp3", "voice": "en-US-AvaMultilingualNeural", "characters": 5, "bytes": 2048, "content_type": "audio/mpeg"}
    monkeypatch.setattr("mizalla.ai.synthesize", lambda *a, **k: clip)
    body = client.post("/apps/ai/speech", data={"text": "Hello", "voice": ""}).get_data(as_text=True)
    assert "<audio" in body
    assert "en-US-AvaMultilingualNeural" in body


# ─── Vision upload validation ───────────────────────────────────────────────

def test_a_non_image_upload_is_refused_before_any_azure_call(app, client, monkeypatch):
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    def explode(*_a, **_k):
        raise AssertionError("Azure must not be called for a non-image")

    monkeypatch.setattr("mizalla.ai.analyze_image", explode)

    login_user(client)
    response = client.post(
        "/apps/ai/vision",
        data={"image": (open(__file__, "rb"), "notes.txt")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"is not an image" in response.data


def test_a_file_that_only_pretends_to_be_an_image_is_refused(app, client, monkeypatch):
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    def explode(*_a, **_k):
        raise AssertionError("Azure must not be called for a fake image")

    monkeypatch.setattr("mizalla.ai.analyze_image", explode)

    login_user(client)
    response = client.post(
        "/apps/ai/vision",
        data={"image": (__import__("io").BytesIO(b"MZ not really a jpeg"), "photo.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"does not look like an image" in response.data


def test_an_azure_failure_is_shown_not_raised(app, client, monkeypatch):
    from mizalla.azure_ai import ServiceError

    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    monkeypatch.setattr(
        "mizalla.ai.analyze_image",
        lambda *a, **k: (_ for _ in ()).throw(ServiceError("Azure rejected the key. Check the Azure Vision key in Settings.")),
    )

    login_user(client)
    response = client.post(
        "/apps/ai/vision",
        data={"image": (__import__("io").BytesIO(b"\xff\xd8\xffrealjpegbytes"), "card.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"rejected the key" in response.data


def test_an_unexpected_error_does_not_leak_a_stack_trace(app, client, monkeypatch):
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    def explode(*_a, **_k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr("mizalla.ai.analyze_image", explode)

    login_user(client)
    response = client.post(
        "/apps/ai/vision",
        data={"image": (__import__("io").BytesIO(b"\xff\xd8\xffrealjpegbytes"), "card.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"secret internal detail" not in response.data


def test_the_vision_result_shows_description_json_and_mapped_fields(app, client, monkeypatch):
    import json

    payload = {
        "readResult": {"blocks": [{"lines": [{"text": "ID: A-1042"}]}]},
    }
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    monkeypatch.setattr(
        "mizalla.ai.analyze_image",
        lambda data, mapping: {
            "caption": "",
            "caption_confidence": None,
            "payload": payload,
            "json_text": json.dumps(payload, indent=2),
            "ocr_text": "ID: A-1042",
            "ocr_line_count": 1,
            "fields": {"ID": ("A-1042", "label")},
        },
    )

    login_user(client)
    body = client.post(
        "/apps/ai/vision",
        data={"image": (__import__("io").BytesIO(b"\xff\xd8\xffrealjpegbytes"), "card.jpg")},
        content_type="multipart/form-data",
    ).get_data(as_text=True)

    assert "A-1042" in body
    assert "readResult" in body
    assert "label" in body


# ─── Settings page ──────────────────────────────────────────────────────────

def test_the_settings_page_offers_the_new_azure_settings(app, client):
    with app.app_context():
        make_user(app, "admin@example.com", is_admin=True)

    login_user(client, "admin@example.com")
    body = client.get("/apps/admin/settings").get_data(as_text=True)
    # Non-credential settings are still editable in the DB.
    for key in (
        "chat_chunk_chars",
        "chat_max_chunks",
    ):
        assert key in body
    # vision_id_mapping is managed directly on the Vision service page, not settings.
    assert "vision_id_mapping" not in body
    # Azure credentials are NOT editable here - they are env-only.
    for key in (
        "azure_vision_endpoint",
        "azure_vision_key",
        "azure_speech_key",
        "azure_speech_region",
        "azure_speech_voice",
        "azure_openai_endpoint",
        "azure_openai_key",
        "azure_openai_deployment",
    ):
        assert key not in body


def test_a_broken_vision_mapping_textarea_does_not_break_the_page(app, client):
    with app.app_context():
        member = make_user(app)
        set_credentials(app)
        grant_all(app, member)

    login_user(client)
    response = client.post(
        "/apps/ai/vision",
        data={
            "image": (__import__("io").BytesIO(b"\xff\xd8\xffrealjpegbytes"), "card.jpg"),
            "mapping": "{ this is not json",
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert b"not valid JSON" in response.data


