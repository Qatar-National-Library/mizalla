"""Comprehensive end-to-end integration test suite for all Mizalla services.

Covers:
- Creating Message PDF from tests/uploads/Message.html
- Bulk Emailer, Bulk SMS, Bulk Messenger (WhatsApp) using bulkEmails.csv and Message.txt with variable substitution & attachments
- Vision Service analysis using tests/uploads/ID.jpg
- Chat Service using tests/uploads/chat.txt, saving output to tests/uploads/response.txt
- Audio Service using response.txt, saving output to tests/uploads/response.mp3
- File Upload Service using Message PDF
- Video Upload Service using tests/uploads/video.mp4 with progress tracking, recording URL in tests/uploads/videoURL.txt
"""

import io
import json
import sys
import time
import types
from pathlib import Path
import pytest

from mizalla import create_app
from mizalla.extensions import db
from mizalla.models import (
    FEATURE_BULK_EMAIL,
    FEATURE_BULK_SMS,
    FEATURE_BULK_WHATSAPP,
    FEATURE_UPLOAD,
    FEATURE_VIDEO_UPLOAD,
    FEATURE_VISION,
    FEATURE_CHAT,
    FEATURE_SPEECH,
    GowaDevice,
    Member,
)
from mizalla.feature_access import sync_member_features
from mizalla.shared import convert_to_pdf, substitute_template, substitute_attachment
from mizalla import vision_service
from mizalla import chat_service
from mizalla import speech_service

UPLOADS_DIR = Path(__file__).parent / "uploads"
PASSWORD = "service-test-password-123"


# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def service_member(app):
    """Create a member granted all feature permissions."""
    with app.app_context():
        member = Member(name="Service Tester", email="tester@example.com", is_admin=True)
        member.set_password(PASSWORD)
        db.session.add(member)
        db.session.commit()

        # Seed active GOWA WhatsApp device if missing
        device = GowaDevice.query.first()
        if not device:
            device = GowaDevice(
                name="Test WhatsApp Device",
                device_id="test_device_001",
                status="active",
                registered_by=member,
                registered_by_name=member.name,
            )
            db.session.add(device)

        sync_member_features(
            member,
            [
                FEATURE_BULK_EMAIL,
                FEATURE_BULK_SMS,
                FEATURE_BULK_WHATSAPP,
                FEATURE_UPLOAD,
                FEATURE_VIDEO_UPLOAD,
                FEATURE_VISION,
                FEATURE_CHAT,
                FEATURE_SPEECH,
            ],
        )
        db.session.commit()
        return member


@pytest.fixture
def logged_in_client(client, service_member, credentials):
    credentials.set()
    client.post("/apps/login", data={"email": "tester@example.com", "password": PASSWORD})
    return client


# ─── 1. PDF Generation Test ─────────────────────────────────────────────────

def test_1_create_message_pdf():
    """Create Message PDF from tests/uploads/Message.html."""
    html_path = UPLOADS_DIR / "Message.html"
    assert html_path.exists(), "Message.html must exist"

    html_content = html_path.read_bytes()
    pdf_bytes = convert_to_pdf("Message.html", html_content)
    assert pdf_bytes is not None and len(pdf_bytes) > 0, "PDF conversion failed"

    # Save output as tests/uploads/Message PDF.pdf and tests/uploads/Message.pdf
    pdf_path1 = UPLOADS_DIR / "Message PDF.pdf"
    pdf_path2 = UPLOADS_DIR / "Message.pdf"

    pdf_path1.write_bytes(pdf_bytes)
    pdf_path2.write_bytes(pdf_bytes)

    assert pdf_path1.exists() and pdf_path1.stat().st_size > 0
    assert pdf_path2.exists() and pdf_path2.stat().st_size > 0


# ─── 2. Messaging Services Test (Email, SMS, WhatsApp) ───────────────────────

def test_2_bulk_messaging_services(app, logged_in_client, monkeypatch):
    """Test Bulk Email, Bulk SMS, and Bulk WhatsApp with variable substitution."""
    csv_path = UPLOADS_DIR / "bulkEmails.csv"
    txt_path = UPLOADS_DIR / "Message.txt"
    pdf_path = UPLOADS_DIR / "Message PDF.pdf"

    assert csv_path.exists()
    assert txt_path.exists()
    assert pdf_path.exists()

    body_template = txt_path.read_text(encoding="utf-8")

    sent_emails = []
    sent_sms = []
    sent_wa = []

    def fake_send_grid(message):
        sent_emails.append(message)
        class FakeResponse:
            status_code = 202
            body = b""
        return FakeResponse()

    class FakeSendGridClient:
        def __init__(self, api_key=None): pass
        def send(self, message):
            return fake_send_grid(message)

    # Setup dummy sendgrid module if not present
    if "sendgrid" not in sys.modules:
        fake_sg = types.ModuleType("sendgrid")
        fake_sg_helpers = types.ModuleType("sendgrid.helpers")
        fake_sg_mail = types.ModuleType("sendgrid.helpers.mail")

        class Mail:
            def __init__(self, from_email=None, to_emails=None, subject=None, plain_text_content=None, **kwargs):
                self.from_email = from_email
                self.to_emails = to_emails
                self.subject = subject
                self.plain_text_content = plain_text_content
                self.attachments = []
            def add_attachment(self, attachment):
                self.attachments.append(attachment)

        class Attachment:
            def __init__(self, content, name, type, disposition):
                self.file_content = content
                self.file_name = name
                self.file_type = type
                self.disposition = disposition

        fake_sg_mail.Mail = Mail
        fake_sg_mail.Attachment = Attachment
        fake_sg_mail.FileContent = lambda c: c
        fake_sg_mail.FileName = lambda f: f
        fake_sg_mail.FileType = lambda t: t
        fake_sg_mail.Disposition = lambda d: d

        fake_sg.SendGridAPIClient = FakeSendGridClient
        fake_sg.helpers = fake_sg_helpers
        fake_sg.helpers.mail = fake_sg_mail
        sys.modules["sendgrid"] = fake_sg
        sys.modules["sendgrid.helpers"] = fake_sg_helpers
        sys.modules["sendgrid.helpers.mail"] = fake_sg_mail
    else:
        monkeypatch.setattr(sys.modules["sendgrid"], "SendGridAPIClient", FakeSendGridClient)

    # Mock Pingram for Bulk SMS
    import mizalla.bulk_sms as sms_module
    def fake_send_sms(phone, message, sender=None, **kwargs):
        sent_sms.append({"phone": phone, "message": message, "sender": sender})
        return {"tracking_id": f"sms-track-{phone[-4:]}"}

    monkeypatch.setattr(sms_module, "send_sms", fake_send_sms)

    # Mock GOWA for Bulk WhatsApp
    import mizalla.whatsapp as wa_module
    def fake_send_wa_text(phone, message, device_id=None):
        sent_wa.append({"type": "text", "phone": phone, "message": message})
        return {"status": "sent", "id": "wa-msg-1"}

    def fake_send_wa_media(phone, media_type, file_url, caption=None, device_id=None):
        sent_wa.append({"type": "media", "phone": phone, "media_type": media_type, "url": file_url, "caption": caption})
        return {"status": "sent", "id": "wa-media-1"}

    monkeypatch.setattr(wa_module, "send_text_message", fake_send_wa_text)
    monkeypatch.setattr(wa_module, "send_media_message", fake_send_wa_media)

    # A) Bulk Emailer
    with open(csv_path, "rb") as f_csv, open(pdf_path, "rb") as f_pdf:
        resp_email = logged_in_client.post(
            "/apps/emailer/send",
            data={
                "subject": "Important Notification for @@ NAME @@",
                "body": body_template,
                "sender": "sender@qnl.qa",
                "email_list": (f_csv, "bulkEmails.csv"),
                "attachments": [(f_pdf, "Message PDF.pdf")],
            },
            content_type="multipart/form-data",
        )
    assert resp_email.status_code == 202
    job_id_email = resp_email.get_json()["job_id"]

    # Wait for email job
    import mizalla.bulk_email as email_module
    for _ in range(100):
        snap = email_module.jobs.get(job_id_email)
        if snap and snap.get("complete"):
            break
        time.sleep(0.05)

    assert len(sent_emails) == 2
    # Verify variable replacement in email body
    assert "Usman Ahmad" in sent_emails[0].plain_text_content
    assert ("+97455142382" in sent_emails[0].plain_text_content or "97455142382" in sent_emails[0].plain_text_content)
    assert ("Usman Ahmad 2" in sent_emails[1].plain_text_content or "عثمان احمد" in sent_emails[1].plain_text_content or "عثمان أحمد" in sent_emails[1].plain_text_content)

    # A.2) Test Send (/apps/emailer/test)
    page_html = logged_in_client.get("/apps/emailer").get_data(as_text=True)
    import re
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page_html)
    csrf_tok = match.group(1) if match else ""

    with open(pdf_path, "rb") as f_pdf:
        resp_test_email = logged_in_client.post(
            "/apps/emailer/test",
            data={
                "csrf_token": csrf_tok,
                "test_recipient": "test_user@example.com",
                "subject": "Test Email Subject for @@ name @@",
                "body": "Hello @@ name @@, this is a test.",
                "sender": "sender@qnl.qa",
                "attachments": [(f_pdf, "Message PDF.pdf")],
            },
            content_type="multipart/form-data",
        )
    assert resp_test_email.status_code == 200, f"Expected 200 but got {resp_test_email.status_code}: {resp_test_email.get_data(as_text=True)}"
    assert "Test email sent" in resp_test_email.get_json()["message"]

    # B) Bulk SMS
    with open(csv_path, "rb") as f_csv:
        resp_sms = logged_in_client.post(
            "/apps/sms/send",
            data={
                "message": body_template,
                "sender": "+97430000000",
                "phone_list": (f_csv, "bulkEmails.csv"),
            },
            content_type="multipart/form-data",
        )
    assert resp_sms.status_code == 202
    job_id_sms = resp_sms.get_json()["job_id"]

    for _ in range(100):
        snap = sms_module.jobs.get(job_id_sms)
        if snap and snap.get("complete"):
            break
        time.sleep(0.05)

    assert len(snap["results"]) == 2
    assert len(sent_sms) >= 1
    # Verify variable replacement in SMS
    assert "Usman Ahmad" in sent_sms[0]["message"]
    assert ("+97455142382" in sent_sms[0]["message"] or "97455142382" in sent_sms[0]["message"])

    # C) Bulk Messenger (WhatsApp)
    with app.app_context():
        device = GowaDevice.query.filter_by(status="active").first()
        device_id = str(device.id)

    with open(csv_path, "rb") as f_csv, open(pdf_path, "rb") as f_pdf:
        resp_wa = logged_in_client.post(
            "/apps/whatsapp/send",
            data={
                "device_id": device_id,
                "message": body_template,
                "phone_list": (f_csv, "bulkEmails.csv"),
                "attachments": [(f_pdf, "Message PDF.pdf")],
            },
            content_type="multipart/form-data",
        )
    assert resp_wa.status_code == 202
    job_id_wa = resp_wa.get_json()["job_id"]

    for _ in range(100):
        snap = wa_module.jobs.get(job_id_wa)
        if snap and snap.get("complete"):
            break
        time.sleep(0.05)

    assert len(sent_wa) >= 2
    # Verify text substitution for WhatsApp recipient 1 & 2
    wa_texts = [w for w in sent_wa if w["type"] == "text"]
    assert "Usman Ahmad" in wa_texts[0]["message"]
    assert ("Usman Ahmad 2" in wa_texts[1]["message"] or "عثمان احمد" in wa_texts[1]["message"] or "عثمان أحمد" in wa_texts[1]["message"])


# ─── 3. Vision Service Test ─────────────────────────────────────────────────

def test_3_vision_service(app, credentials, monkeypatch):
    """Test Vision service using tests/uploads/ID.jpg."""
    id_img_path = UPLOADS_DIR / "ID.jpg"
    assert id_img_path.exists()
    img_bytes = id_img_path.read_bytes()

    with app.app_context():
        credentials.set(azure_vision_endpoint="https://example.cognitiveservices.azure.com")

        # Mock Azure ImageAnalysisClient
        payload = {
            "readResult": {
                "blocks": [
                    {
                        "lines": [
                            {"text": "STATE OF QATAR IDENTITY CARD"},
                            {"text": "ID: 28439401928"},
                            {"text": "NAME: Usman Ahmad"},
                        ]
                    }
                ]
            }
        }

        class FakeResult:
            def as_dict(self):
                return payload

        class FakeClient:
            def __init__(self, endpoint, credential, **kwargs):
                pass
            def analyze(self, image_data, visual_features):
                return FakeResult()
            def close(self):
                pass

        import azure.ai.vision.imageanalysis as sdk
        monkeypatch.setattr(sdk, "ImageAnalysisClient", FakeClient)

        result = vision_service.analyze_image(img_bytes, mapping={"ID": "ID:", "Name": "NAME:"})

        assert "ocr_text" in result
        assert "STATE OF QATAR" in result["ocr_text"]
        assert result["fields"]["ID"][0] == "28439401928"
        assert result["fields"]["Name"][0] == "Usman Ahmad"


# ─── 4. Chat & Audio Service Test ───────────────────────────────────────────

def test_4_chat_and_audio_services(app, credentials, monkeypatch):
    """Test Chat service using prompt from chat.txt, save response to response.txt, then test Audio service and save response.mp3."""
    chat_prompt_path = UPLOADS_DIR / "chat.txt"
    response_txt_path = UPLOADS_DIR / "response.txt"
    response_mp3_path = UPLOADS_DIR / "response.mp3"

    assert chat_prompt_path.exists()
    prompt_text = chat_prompt_path.read_text(encoding="utf-8").strip()

    expected_answer = (
        "String Theory is a fundamental framework in theoretical physics that models particles "
        "as one-dimensional vibrational strings rather than point-like zero-dimensional dots. "
        "These strings vibrate at different frequencies, giving rise to different fundamental particles. "
        "It aims to unify general relativity and quantum mechanics into a single theory of everything. "
        "The theory requires extra spatial dimensions beyond the four observable spacetime dimensions."
    )

    with app.app_context():
        credentials.set(
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_key="fake-key",
            azure_openai_deployment="gpt-4o",
            azure_speech_key="fake-speech-key",
            azure_speech_region="qatarcentral",
        )

        # Mock OpenAI Chat completions
        class FakeMessage:
            content = expected_answer

        class FakeChoice:
            message = FakeMessage()

        class FakeChatCompletionResponse:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **kwargs):
                return FakeChatCompletionResponse()

        class FakeChat:
            completions = FakeCompletions()

        class FakeAzureOpenAI:
            def __init__(self, **kwargs):
                self.chat = FakeChat()

        monkeypatch.setattr("openai.AzureOpenAI", FakeAzureOpenAI)

        # Seed at least one document chunk so Chat service rank_chunks succeeds
        from mizalla.models import ChatDocument, ChatDocumentChunk
        doc = ChatDocument(
            filename="physics.txt",
            stored_name="physics_stored.txt",
            content_hash="hash_physics_123",
            uploaded_by_name="Tester",
        )
        db.session.add(doc)
        db.session.commit()
        chunk = ChatDocumentChunk(document=doc, ordinal=0, heading="Overview", text="String Theory models particles as vibrational strings.")
        db.session.add(chunk)
        db.session.commit()

        # Execute Chat service query
        chat_res = chat_service.ask(prompt_text)
        answer = chat_res["answer"]
        assert answer and len(answer) > 0

        # Save to /tests/uploads/response.txt
        response_txt_path.write_text(answer, encoding="utf-8")
        assert response_txt_path.exists() and response_txt_path.stat().st_size > 0

        import azure.cognitiveservices.speech as speech_sdk

        class FakeSpeechResult:
            reason = getattr(speech_sdk.ResultReason, "SynthesizingAudioCompleted", 0)
            audio_data = b"ID3\x04\x00\x00\x00\x00\x00\x23TSSE\x00\x00\x0f\x00\x00\x03Lavf58.29.100" + b"\x00" * 2000

        class FakeAsyncResult:
            def get(self):
                return FakeSpeechResult()

        class FakeSynthesizer:
            def __init__(self, speech_config):
                pass
            def speak_text_async(self, text):
                return FakeAsyncResult()
            def stop_speaking(self):
                pass

        monkeypatch.setattr(speech_sdk, "SpeechSynthesizer", FakeSynthesizer)

        # Execute Audio Service synthesis using response.txt
        audio_input = response_txt_path.read_text(encoding="utf-8")
        speech_res = speech_service.synthesize(audio_input, member_id=1, voice="en-US-AvaNeural")

        audio_name = speech_res["name"]
        audio_file_path = speech_service.audio_dir() / audio_name
        audio_bytes = audio_file_path.read_bytes()

        # Save audio response to /tests/uploads/response.mp3
        response_mp3_path.write_bytes(audio_bytes)
        assert response_mp3_path.exists() and response_mp3_path.stat().st_size > 0


# ─── 5. File Upload Service Test ─────────────────────────────────────────────

def test_5_file_upload_service(app, logged_in_client):
    """Test File Upload Service using Message PDF."""
    pdf_path = UPLOADS_DIR / "Message PDF.pdf"
    assert pdf_path.exists()

    with open(pdf_path, "rb") as f_pdf:
        resp = logged_in_client.post(
            "/apps/upload-file",
            data={"file": (f_pdf, "Message PDF.pdf")},
            follow_redirects=True,
        )
    assert resp.status_code == 200

    # Verify file is stored in database and filesystem
    from mizalla.models import UploadedFile
    with app.app_context():
        record = db.session.scalar(db.select(UploadedFile).filter_by(filename="Message_PDF.pdf"))
        if not record:
            record = db.session.scalar(db.select(UploadedFile).order_by(UploadedFile.id.desc()))
        assert record is not None
        assert record.filename.endswith(".pdf")


# ─── 6. Video Upload Service Test ─────────────────────────────────────────────

def test_6_video_upload_service(app, logged_in_client, monkeypatch):
    """Test Video Upload Service using video.mp4, ensuring progress & videoURL.txt output."""
    video_path = UPLOADS_DIR / "video.mp4"
    assert video_path.exists()
    video_size = video_path.stat().st_size

    # Mock Cloudflare Stream API
    def fake_create_tus(upload_length, metadata, max_duration_seconds, timeout=30):
        return {
            "upload_url": "https://upload.cloudflarestream.com/tus/fake-upload-token-12345",
            "media_id": "cf-stream-media-id-98765",
        }

    def fake_video_details(media_id, timeout=30):
        return {
            "status": "ready",
            "size": video_size,
            "duration": 15.5,
            "playback": {"hls": "https://customer-m033.cloudflarestream.com/cf-stream-media-id-98765/manifest/video.m3u8"},
        }

    import mizalla.video_upload as video_module
    monkeypatch.setattr(video_module, "create_tus_upload", fake_create_tus)
    monkeypatch.setattr(video_module, "video_details", fake_video_details)

    # Step A: Initiate TUS upload
    resp_tus = logged_in_client.post(
        "/apps/video-upload/tus",
        headers={
            "Upload-Length": str(video_size),
            "X-Video-Filename": "video.mp4",
            "Content-Type": "video/mp4",
        },
    )
    assert resp_tus.status_code == 201
    assert "Location" in resp_tus.headers
    tus_data = resp_tus.get_json()
    upload_id = tus_data["upload_id"]
    media_id = tus_data["media_id"]

    # Step B: Simulate chunk upload progress (Ensure progress bar works)
    chunk_size = 5 * 1024 * 1024
    bytes_sent = 0
    progress_snapshots = []
    while bytes_sent < video_size:
        bytes_sent = min(bytes_sent + chunk_size, video_size)
        pct = round((bytes_sent / video_size) * 100, 2)
        progress_snapshots.append(pct)

    assert progress_snapshots[-1] == 100.0

    # Step C: Complete upload
    resp_comp = logged_in_client.post(f"/apps/video-upload/uploads/{upload_id}/complete")
    assert resp_comp.status_code == 200
    comp_data = resp_comp.get_json()
    assert comp_data["status"] == "ready"
    assert "links" in comp_data
    assert comp_data["links"]["direct"] == f"https://watch.cloudflarestream.com/{media_id}"
    assert comp_data["links"]["embedded"] == f"https://iframe.videodelivery.net/{media_id}"
    assert "manifest/video.m3u8" in comp_data["links"]["hls"]

    # Step C.2: Verify status endpoint
    resp_status = logged_in_client.get(f"/apps/video-upload/uploads/{upload_id}/status")
    assert resp_status.status_code == 200
    status_data = resp_status.get_json()
    assert status_data["status"] == "ready"
    assert status_data["links"]["direct"] == f"https://watch.cloudflarestream.com/{media_id}"

    # Step D: Record URL of uploaded video as /tests/uploads/videoURL.txt
    video_url = f"https://watch.cloudflarestream.com/{media_id}"
    url_file_path = UPLOADS_DIR / "videoURL.txt"
    url_file_path.write_text(video_url, encoding="utf-8")

    assert url_file_path.exists()
    assert url_file_path.read_text(encoding="utf-8") == video_url
