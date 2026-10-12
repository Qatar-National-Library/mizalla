"""Cloudflare Stream client for the Video Upload service.

Cloudflare Stream is reached over its v4 REST API with a bearer token, in the
same shape as :mod:`mizalla.pingram`: a plain ``urllib`` request rather
than an SDK, because Stream's tus upload is a protocol handshake rather than a
JSON call and an SDK would not help with it.

The one-time upload URL this provisions is *not* a secret, and deliberately so -
it is handed to the member's own browser, which then sends the video bytes
straight to Cloudflare. That is the whole point of a direct creator upload: the
API token stays on the server, the video never passes through it, and a 2 GB
upload costs this application nothing but a few hundred bytes. What the URL
*can* do is consume the account's storage, so it is issued only to a member who
has been granted the service, never cached, and never reused.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

from .azure_ai import credential, is_transport_message

logger = logging.getLogger(__name__)

#: Cloudflare's production API base. Overridable through ``CLOUDFLARE_ENDPOINT``
#: because it is the one part of the integration a deployment may redirect - to a
#: regional base, a proxy, or a test account - and that should not need a code
#: change. The trailing slash is stripped on use so a value with or without one
#: both produce a valid URL.
DEFAULT_ENDPOINT = "https://api.cloudflare.com/client/v4"

#: Provisioning an upload URL is a single small request against an edge API, so
#: this is generous. It is not the timeout for the upload itself: those bytes
#: never touch this process.
REQUEST_TIMEOUT = 30

#: What Cloudflare calls the resumable-upload version. Sent on the provisioning
#: request so Stream accepts the tus headers that come with it.
TUS_VERSION = "1.0.0"

#: Marker added to Stream on every upload so a video stored by this service can
#: be told apart from one added through the Cloudflare dashboard or the CLI.
SOURCE_METADATA_KEY = "mizalla"


class CloudflareError(Exception):
    """Cloudflare was reached but refused the request, or answered unusably."""


def endpoint() -> str:
    """The API base to call, without a trailing slash."""
    raw = (credential("cloudflare_endpoint") or DEFAULT_ENDPOINT).strip()
    if "?" in raw:
        raw = raw.split("?")[0]
    raw = raw.rstrip("/")
    if "/accounts" in raw:
        raw = raw.split("/accounts")[0].rstrip("/")
    return raw or DEFAULT_ENDPOINT


def _stream_url(*parts: str, query: dict | None = None) -> str:
    """Build a Stream URL for this account.

    The account id is a path segment, so it is quoted: it is hexadecimal today,
    but a value that arrived from a hand-edited .env must not be able to reshape
    the path.
    """
    account = urllib.parse.quote(credential("cloudflare_account_id"), safe="")
    path = "/".join(urllib.parse.quote(part, safe="") for part in parts)
    url = f"{endpoint()}/accounts/{account}/stream"
    if path:
        url = f"{url}/{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    return url


def _request(method: str, url: str, headers: dict | None = None, timeout: int = REQUEST_TIMEOUT) -> tuple:
    """Perform one Cloudflare call, returning ``(status, headers, body)``.

    ``urllib.error.HTTPError`` is a response rather than a failure, so it is read
    like one - Cloudflare puts the reason in the body of a 4xx far more often
    than in the status line.

    A transport failure (DNS, refused connection, TLS, timeout) is raised as
    :class:`CloudflareError` instead. Left raw it would escape to a 500 page
    naming the internal host and socket internals, which a member can neither act
    on nor read safely - and it would be reported as a bug in this application
    rather than as an unreachable Cloudflare.
    """
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {credential('cloudflare_api_token')}", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Cloudflare transport failure calling %s %s: %s", method, url, exc)
        if is_transport_message(str(exc)):
            raise CloudflareError(
                "Could not reach Cloudflare Stream. Check the network connection and try again."
            ) from exc
        raise CloudflareError(f"Cloudflare Stream could not be reached: {exc}") from exc


def _friendly_error(status: int, body: str) -> str:
    """Turn a Cloudflare error body into something a member can act on.

    Cloudflare's own message is preferred when present, because it distinguishes
    the failures that matter here - a bad token from a missing permission - and
    those cannot be guessed from the status code. Only the shape of the response
    is replaced when it holds no such message, since a raw body can echo the
    request headers back.
    """
    messages = []
    try:
        for error in (json.loads(body) or {}).get("errors") or []:
            message = (error or {}).get("message")
            if message:
                messages.append(str(message))
    except (json.JSONDecodeError, ValueError, AttributeError):
        messages = []

    if messages:
        return "Cloudflare Stream refused the request: " + "; ".join(messages)

    if status in (401, 403):
        # The overwhelmingly common cause is a token without the Stream:Edit
        # permission, which reads as 403 with an empty error list.
        return ("Cloudflare Stream rejected the API token (HTTP %d). Check that "
                "CLOUDFLARE_API_TOKEN is correct and has the Stream:Edit permission." % status)
    if status == 404:
        return "Cloudflare Stream could not find that video. It may already have been deleted."
    return f"Cloudflare Stream returned an unexpected response (HTTP {status})."


def encode_metadata(pairs: dict) -> str:
    """Build a tus ``Upload-Metadata`` value from key/value pairs.

    Per the tus spec each value is base64 and the key and value are separated by a
    space, not an equals sign; pairs are joined by a comma with no space. Keys
    with an empty value are dropped, because the spec gives a bare key (such as
    ``requiresignedurls``) a meaning of "present" and an empty value a different
    one.
    """
    encoded = []
    for key, value in pairs.items():
        if value in (None, ""):
            continue
        encoded.append(f"{key} {b64encode(str(value).encode('utf-8')).decode('ascii')}")
    return ",".join(encoded)


def create_tus_upload(
    upload_length: int,
    metadata: dict,
    max_duration_seconds: int,
    timeout: int = REQUEST_TIMEOUT,
) -> dict:
    """Reserve one-time storage and return the tus URL the browser uploads to.

    Returns ``{"upload_url": ..., "media_id": ...}``. ``media_id`` may be empty
    if Cloudflare omitted the header, which the caller must tolerate - the docs
    are explicit that the video id should come from ``stream-media-id`` rather
    than from parsing the URL, but they do not promise the header on every
    response, and an upload that works is worth more than an id for it.

    Raises :class:`CloudflareError` with a member-readable message on any refusal,
    missing credentials, or unusable answer. A dict of errors like
    :func:`mizalla.pingram.send_sms` returns would be wrong here: this is a
    single request the member is waiting on, so the caller wants to raise.
    """
    if not upload_length or upload_length < 1:
        raise CloudflareError("The video has no readable size, so Cloudflare cannot reserve storage for it.")
    if not max_duration_seconds or max_duration_seconds < 1:
        raise CloudflareError(
            "The maximum video duration is not configured. An administrator must set "
            "video_upload_max_duration_seconds."
        )

    missing = [
        name
        for name, key in (
            ("CLOUDFLARE_ACCOUNT_ID", "cloudflare_account_id"),
            ("CLOUDFLARE_API_TOKEN", "cloudflare_api_token"),
        )
        if not credential(key)
    ]
    if missing:
        raise CloudflareError(
            "This service has no credentials. An administrator must add "
            + ", ".join(missing)
            + " to the .env file and restart the application."
        )

    # The declared ceiling is passed as metadata rather than as a query
    # parameter, because that is the tus way to carry a per-upload constraint;
    # maxDurationSeconds is what Cloudflare reads to size the reservation.
    payload = dict(metadata)
    payload["maxDurationSeconds"] = str(max_duration_seconds)

    headers = {
        "Tus-Resumable": TUS_VERSION,
        "Upload-Length": str(upload_length),
        "Content-Type": "application/json",
    }
    encoded = encode_metadata(payload)
    if encoded:
        headers["Upload-Metadata"] = encoded

    # direct_user=true is what makes this a *creator* upload: the resulting video
    # is owned by the end user rather than by the API token's account, and the
    # one-time URL is issued for a single upload.
    status, response_headers, body = _request(
        "POST", _stream_url(query={"direct_user": "true"}), headers, timeout
    )

    if status not in (200, 201):
        logger.warning("Cloudflare Stream refused a tus upload (HTTP %d): %s", status, body[:300])
        raise CloudflareError(_friendly_error(status, body))

    # urllib's HTTPMessage is case-insensitive, so this is not spelling-sensitive.
    upload_url = (response_headers.get("Location") or "").strip()
    if not upload_url:
        logger.warning("Cloudflare Stream accepted a tus upload but returned no Location header")
        raise CloudflareError("Cloudflare Stream did not return an upload URL. Try again in a moment.")

    return {
        "upload_url": upload_url,
        "media_id": (response_headers.get("stream-media-id") or "").strip(),
    }


def video_details(media_id: str, timeout: int = REQUEST_TIMEOUT) -> dict:
    """Fetch one video's current state from Cloudflare.

    Used after a member's browser reports a finished upload, so the status shown
    is Cloudflare's own rather than the browser's claim. Raises
    :class:`CloudflareError` when the video cannot be read, which the caller
    treats as "unknown" rather than as a failure of the upload.
    """
    if not media_id or not credential("cloudflare_api_token"):
        raise CloudflareError("This service has no credentials.")

    status, _headers, body = _request("GET", _stream_url(media_id), timeout=timeout)
    if status != 200:
        logger.warning("Cloudflare Stream video lookup failed (HTTP %d) for %s", status, media_id)
        raise CloudflareError(_friendly_error(status, body))

    try:
        result = (json.loads(body) or {}).get("result") or {}
    except (json.JSONDecodeError, ValueError, AttributeError):
        raise CloudflareError("Cloudflare Stream returned a response that could not be read.") from None
    if not isinstance(result, dict):
        raise CloudflareError("Cloudflare Stream returned a response that could not be read.")
    return result


def delete_video(media_id: str, timeout: int = REQUEST_TIMEOUT) -> None:
    """Delete a video and its copies from Cloudflare.

    Raises :class:`CloudflareError` on refusal. The caller keeps its own record
    when this raises, because the video is still on Cloudflare and pretending
    otherwise would leave an entry that cannot be removed from here.
    """
    if not media_id:
        raise CloudflareError("That video has no Cloudflare identifier, so it cannot be deleted.")
    if not credential("cloudflare_api_token"):
        raise CloudflareError(
            "This service has no credentials. An administrator must add "
            "CLOUDFLARE_API_TOKEN to the .env file and restart the application."
        )

    status, _headers, body = _request("DELETE", _stream_url(media_id), timeout=timeout)
    # A repeat delete answers 404 rather than an error status, which is the
    # outcome the caller wanted, so it is not raised.
    if status not in (200, 202, 204, 404):
        logger.warning("Cloudflare Stream refused to delete %s (HTTP %d): %s", media_id, status, body[:300])
        raise CloudflareError(_friendly_error(status, body))
