"""Pingram client — the REST calls behind the Bulk SMS service.

Pingram publishes an SDK for most languages, but each one adds a dependency
chain to perform a single authenticated HTTP POST. This calls the documented
endpoint directly instead, which keeps the failure surface small and explicit:
either the transport failed or the API answered, and both are handled below.

Credentials are read from the environment through :mod:`mizalla.azure_ai`
rather than from the settings table, which is the boundary every other service
in this app uses. A key typed into a web form is a key sitting in a database
table, a backup, and every screenshot of the settings screen.
"""

import json
import logging
import urllib.error
import urllib.request

from .azure_ai import credential, is_transport_message

logger = logging.getLogger("mizalla.pingram")

#: Pingram serves each region from its own host. ``us`` is the default and has
#: no regional label; the other two carry theirs as the first DNS label.
REGION_BASE_URLS = {
    "us": "https://api.pingram.io",
    "eu": "https://api.eu.pingram.io",
    "ca": "https://api.ca.pingram.io",
}
DEFAULT_REGION = "us"

#: How long one send may take before it is treated as a failure. Pingram
#: acknowledges in well under a second, so the slack covers a cold TLS
#: handshake on a slow link rather than a slow API.
SEND_TIMEOUT = 30

#: Human-readable text for the HTTP statuses a send can plausibly return. The
#: default arm names the status rather than guessing, so an unmapped code is
#: still actionable.
ERROR_MESSAGES = {
    400: "Pingram rejected the request — check the destination number and message.",
    401: "Authentication failed — the Pingram API key is invalid or missing.",
    403: "Permission denied — this Pingram account may not send to that destination.",
    404: "Not found — the requested Pingram resource does not exist.",
    413: "Payload too large — the message exceeds what Pingram accepts in one send.",
    422: "Pingram could not process the request — the number or message is not sendable.",
    429: "Rate limited — too many requests. Wait a moment and try again.",
    500: "Pingram server error — try again later.",
    502: "Bad gateway — Pingram is temporarily unavailable.",
    503: "Service unavailable — Pingram is down for maintenance.",
    504: "Gateway timeout — the request took too long. Try again.",
}


def region() -> str:
    """The configured region code, lower-cased, or ``us`` when unset or unknown.

    An unrecognised code falls back rather than raising: a typo in ``.env``
    should not take the whole navigation down, and the US host is the documented
    default. :func:`api_base_url` reports what was actually used so a
    misconfigured deployment can be spotted in the service page footer.
    """
    code = (credential("pingram_region") or DEFAULT_REGION).strip().lower()
    return code if code in REGION_BASE_URLS else DEFAULT_REGION


def api_base_url() -> str:
    """The API host for the configured region."""
    return REGION_BASE_URLS[region()]


def is_configured() -> bool:
    """True when both credentials needed to send are present."""
    return bool(credential("pingram_api_key")) and bool(credential("pingram_region"))


def _friendly_error(status_code: int, response_body: str = "") -> str:
    """A message a member can act on, for one failed HTTP status."""
    base = ERROR_MESSAGES.get(status_code, f"Pingram returned HTTP {status_code}.")
    # Pingram's own error text is short and specific ("destination not
    # reachable"), so it is worth keeping. A long body is a stack trace or an
    # HTML error page and would only bury the useful part.
    detail = (response_body or "").strip()
    if detail and len(detail) < 200:
        return f"{base} Details: {detail}"
    return base


def send_sms(
    phone: str,
    message: str,
    notification_type: str = "sms",
    sender: str | None = None,
    timeout: int = SEND_TIMEOUT,
) -> dict:
    """Send one SMS through Pingram.

    Returns ``{"tracking_id": "..."}`` on success, or ``{"error": "..."}`` on any
    failure. A dict rather than an exception because the caller is a background
    loop over hundreds of recipients, where the normal case for a failure is one
    bad number rather than a broken service.

    ``notification_type`` is a stable slug Pingram groups its analytics by. It
    is not a template id — sending directly, without a stored template, is what
    this service does.
    """
    api_key = credential("pingram_api_key")
    if not api_key:
        return {"error": "This service has no credentials. An administrator must add "
                         "PINGRAM_API_KEY to the .env file and restart the application."}

    payload = {
        "type": notification_type,
        "to": phone,
        "message": message,
    }
    if sender:
        # Only sent when set: Pingram treats `from` as an override that must
        # name a dedicated number on the account, so omitting it leaves the
        # account default in place.
        payload["from"] = sender

    request = urllib.request.Request(
        f"{api_base_url()}/sms",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        logger.warning("Pingram HTTP %d sending to %s: %s", exc.code, phone, error_body[:300])
        return {"error": _friendly_error(exc.code, error_body)}
    except Exception as exc:
        # A transport failure names an internal host and socket internals.
        # None of that is actionable for a member, and the raw text can echo
        # the request, so it is replaced with a plain connectivity sentence.
        logger.warning("Pingram transport failure sending to %s: %s", phone, exc)
        if is_transport_message(str(exc)):
            return {"error": f"Could not reach Pingram at {api_base_url()}. Check the "
                             "network connection and try again."}
        return {"error": f"The message to {phone} could not be sent: {exc}"}

    tracking_id = ""
    try:
        tracking_id = (json.loads(body) or {}).get("trackingId", "") or ""
    except (json.JSONDecodeError, ValueError):
        # A 2xx with an unparseable body is still a send Pingram accepted;
        # the recipient result is what matters, so this is logged, not raised.
        logger.info("Pingram 2xx with unparseable body for %s: %s", phone, body[:200])
    return {"tracking_id": tracking_id}