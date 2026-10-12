"""The service links shown in the header.

Every service the app offers sits behind a feature grant, so the header is the
single place that decides what a member can see. Two rules apply:

* A service the member has not been granted is omitted entirely. A link that
  leads to a 403 is worse than no link - it advertises something and then
  refuses it.
* A service the member *has* been granted but whose credentials are absent is
  still listed, disabled, with the reason in its tooltip. Hiding it would leave
  a granted member wondering whether they had been set up at all.

Administrators always pass :func:`has_feature_access`, so they see every service
regardless of their own grants and can verify one before rolling it out.
"""

from flask import url_for

from .azure_ai import service_status
from .feature_access import has_feature_access
from .models import (
    FEATURE_BULK_EMAIL,
    FEATURE_BULK_SMS,
    FEATURE_BULK_WHATSAPP,
    FEATURE_CHAT,
    FEATURE_SPEECH,
    FEATURE_VIDEO_UPLOAD,
    FEATURE_VISION,
    FEATURE_UPLOAD,
)

#: Header order. Projects is rendered separately in the template because it is
#: not grant-gated - every signed-in member can see it.
SERVICES = (
    {"feature": FEATURE_BULK_EMAIL, "label": "Emailer", "endpoint": "bulk_email.index"},
    {"feature": FEATURE_BULK_WHATSAPP, "label": "WhatsApp", "endpoint": "whatsapp.index"},
    {"feature": FEATURE_BULK_SMS, "label": "SMS", "endpoint": "bulk_sms.index"},
    {"feature": FEATURE_VISION, "label": "Vision", "endpoint": "ai.vision"},
    {"feature": FEATURE_SPEECH, "label": "Speech", "endpoint": "ai.speech"},
    {"feature": FEATURE_CHAT, "label": "Chat", "endpoint": "ai.chat"},
    {"feature": FEATURE_UPLOAD, "label": "Upload File", "endpoint": "upload_file.index"},
    # The full name is in FEATURE_LABELS and is what the admin member form shows.
    {"feature": FEATURE_VIDEO_UPLOAD, "label": "Upload Video", "endpoint": "video_upload.index"},
)


def nav_services(member) -> list[dict]:
    """Header entries for the services ``member`` may use.

    Each entry gains ``href``, and ``enabled`` plus ``reason`` from the
    credential check. Reading the environment is cheap enough to do for every
    service on every page, which is what lets the check live here rather than in
    a view.
    """
    links = []
    for service in SERVICES:
        if not has_feature_access(member, service["feature"]):
            continue
        status = service_status(service["feature"])
        links.append({
            **service,
            "href": url_for(service["endpoint"]),
            "enabled": status["configured"],
            "reason": (
                "" if status["configured"]
                else "Not configured. An administrator must add these to the .env file: "
                     + ", ".join(status["missing"])
            ),
        })
    return links
