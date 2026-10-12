import json
import logging
import os
import urllib.error
import urllib.request

from .settings import get_setting

logger = logging.getLogger("mizalla.email")

# In-memory log of dispatched emails (useful for testing & development inspection)
SENT_EMAILS_LOG: list[dict] = []


def send_email(to_email: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    """
    Send an email via SendGrid API v3 using SENDGRID_API_KEY from environment.
    Falls back to safe logging when SENDGRID_API_KEY is not configured.
    """
    to_email_clean = to_email.strip()
    if not to_email_clean:
        return False

    from_email = get_setting("sendgrid_from_email", "noreply@mizalla.local")
    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()

    record = {
        "to": to_email_clean,
        "from": from_email,
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body,
        "sent_via_sendgrid": bool(api_key),
    }
    SENT_EMAILS_LOG.append(record)

    if not api_key:
        logger.info("SENDGRID_API_KEY not set. Email logged locally to '%s': %s", to_email_clean, subject)
        return True

    url = "https://api.sendgrid.com/v3/mail/send"
    payload = {
        "personalizations": [{"to": [{"email": to_email_clean}]}],
        "from": {"email": from_email},
        "subject": subject,
        "content": [
            {"type": "text/html", "value": html_body},
        ],
    }
    if text_body:
        payload["content"].insert(0, {"type": "text/plain", "value": text_body})

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mizalla/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status in (200, 202):
                logger.info("Email successfully sent via SendGrid to '%s': %s", to_email_clean, subject)
                return True
            logger.warning("SendGrid returned unexpected status code %d for '%s'", response.status, to_email_clean)
            return False
    except urllib.error.HTTPError as err:
        error_details = err.read().decode("utf-8", errors="ignore")
        logger.error("SendGrid HTTPError %d: %s for recipient '%s'", err.code, error_details, to_email_clean)
        return False
    except Exception as exc:
        logger.error("Failed to send SendGrid email to '%s': %s", to_email_clean, str(exc))
        return False
