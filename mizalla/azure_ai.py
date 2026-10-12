"""Configuration checks and shared error types for the Azure-backed services.

Each service needs a different set of credentials before it can be used. The web
layer asks :func:`service_status` to decide whether to render a working link or
a disabled one with the reason spelled out, and the service modules raise
:class:`ServiceNotConfigured` rather than failing deep inside an SDK call.

Credentials are read from the process environment - which :func:`create_app`
populates from the ``.env`` file - and never from the settings table. That is a
deliberate security boundary: a key typed into a web form is a key sitting in a
database table, a backup, and every screenshot of the settings screen. Nothing
here reads or writes the database, so the admin UI has no way to display one.
"""

import os
from urllib.parse import urlparse

#: Settings that must be present before each service can run.
VISION_REQUIRED = ("azure_vision_endpoint", "azure_vision_key")
SPEECH_REQUIRED = ("azure_speech_key", "azure_speech_region")
CHAT_REQUIRED = ("azure_openai_endpoint", "azure_openai_key", "azure_openai_deployment")

#: The region is a required credential rather than an optional one because the
#: SMS host is chosen by it: sending a UAE campaign through the EU host routes
#: it to the wrong sender pool, and the messages appear to succeed while never
#: arriving. Requiring it means a deployment that has not chosen deliberately is
#: reported as unconfigured instead of silently defaulting to the US region.
SMS_REQUIRED = ("pingram_api_key", "pingram_region")

#: Cloudflare Stream, for the Video Upload service. The endpoint is a credential
#: rather than a constant because the API base is configurable: it is the one
#: part of a Cloudflare integration that a deployment legitimately redirects (a
#: proxy, a regional base URL, a test account), and hardcoding it would make such
#: a deployment edit code. Read by mizalla.cloudflare_stream.
VIDEO_REQUIRED = ("cloudflare_account_id", "cloudflare_api_token", "cloudflare_endpoint")

#: Environment variable names for each credential, tried in order. The first
#: name is what this deployment's .env uses; the rest are the conventional Azure
#: spellings, so a .env written either way works without a code change.
_ENV_NAMES = {
    "azure_vision_endpoint": ("MS_VISION_ENDPOINT", "AZURE_VISION_ENDPOINT"),
    "azure_vision_key": ("MS_VISION_KEY", "AZURE_VISION_KEY"),
    "azure_speech_key": ("MS_SPEECH_KEY", "AZURE_SPEECH_KEY"),
    "azure_speech_region": ("MS_REGION", "AZURE_SPEECH_REGION", "MS_SPEECH_ENDPOINT"),
    "azure_openai_endpoint": ("AZURE_OPENAI_ENDPOINT",),
    "azure_openai_key": ("AZURE_OPENAI_KEY",),
    "azure_openai_deployment": ("AZURE_OPENAI_DEPLOYMENT_NAME", "AZURE_OPENAI_DEPLOYMENT"),
    "azure_openai_api_version": ("AZURE_OPENAI_API_VERSION",),
    # Pingram, for the Bulk SMS service. Read by mizalla.pingram.
    "pingram_api_key": ("PINGRAM_API_KEY",),
    "pingram_region": ("PINGRAM_REGION_CODE", "PINGRAM_REGION"),
    # Cloudflare Stream, for the Video Upload service. Read by
    # mizalla.cloudflare_stream.
    "cloudflare_account_id": ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_STREAM_ACCOUNT_ID", "TUS_ACCOUNT_ID"),
    "cloudflare_api_token": (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_API_KEY",
        "CLOUDFLARE_STREAM_API_TOKEN",
        "CLOUDFLARE_STREAM_API_KEY",
        "TUS_API_KEY",
        "TUS_API_TOKEN",
    ),
    "cloudflare_endpoint": ("CLOUDFLARE_ENDPOINT", "CLOUDFLARE_STREAM_ENDPOINT", "TUS_ENDPOINT"),
}

#: The name an administrator has to put in .env, per credential. This is what
#: gets reported when something is unset, so it reads as an instruction rather
#: than as an internal settings key.
SETTING_LABELS = {
    "azure_vision_endpoint": "MS_VISION_ENDPOINT",
    "azure_vision_key": "MS_VISION_KEY",
    "azure_speech_key": "MS_SPEECH_KEY",
    "azure_speech_region": "MS_REGION (or MS_SPEECH_ENDPOINT)",
    "azure_openai_endpoint": "AZURE_OPENAI_ENDPOINT",
    "azure_openai_key": "AZURE_OPENAI_KEY",
    "azure_openai_deployment": "AZURE_OPENAI_DEPLOYMENT_NAME",
    "azure_openai_api_version": "AZURE_OPENAI_API_VERSION",
    "pingram_api_key": "PINGRAM_API_KEY",
    "pingram_region": "PINGRAM_REGION_CODE",
    "cloudflare_account_id": "CLOUDFLARE_ACCOUNT_ID",
    "cloudflare_api_token": "CLOUDFLARE_API_TOKEN (needs the Stream:Edit permission)",
    "cloudflare_endpoint": "CLOUDFLARE_ENDPOINT",
}

#: feature key -> required settings, used for both the disabled links and the
#: "why is this unavailable" text on the service pages.
FEATURE_REQUIREMENTS = {
    "vision": VISION_REQUIRED,
    "speech": SPEECH_REQUIRED,
    "chat": CHAT_REQUIRED,
    # Bulk email and Bulk WhatsApp are absent on purpose: their credentials are
    # not in .env (SendGrid and GOWA each bring their own configuration), so
    # there is nothing for this table to check and listing them would report
    # them permanently unconfigured.
    "bulk_sms": SMS_REQUIRED,
    "video_upload": VIDEO_REQUIRED,
}


class ServiceNotConfigured(Exception):
    """The service has no credentials, so it cannot be used at all."""


class ServiceError(Exception):
    """The upstream Azure service was reached but returned an error."""


#: Transport-level failures never reach the service, so the SDK re-raises them as
#: a raw socket/DNS/TLS message naming the internal endpoint and the transport
#: stack. A member cannot act on any of that, so the text is recognised and
#: replaced rather than shown. Detection is by class name and message hint
#: because the concrete exception types come from whichever HTTP stack the
#: installed SDK version happens to use.
_TRANSPORT_ERRORS = frozenset({
    "APIConnectionError", "APITimeoutError",
    "ConnectError", "ConnectTimeout", "ConnectionError", "ConnectionResetError",
    "DNSError", "MaxRetryError", "NameResolutionError", "NewConnectionError",
    "ProxyError", "ReadTimeout", "RemoteDisconnected", "SSLError", "Timeout",
    "TimeoutError", "TransportError", "UnsupportedProtocol",
})
_TRANSPORT_HINTS = (
    "failed to resolve", "name or service not known", "temporary failure in name resolution",
    "connection refused", "connection aborted", "connection reset", "timed out",
    "timeout", "network is unreachable", "no route to host", "ssl", "certificate",
    "max retries exceeded", "getaddrinfo",
)


def is_transport_message(message: str) -> bool:
    """True when ``message`` reads like a network failure rather than an answer.

    Split out from :func:`is_transport_failure` because the Speech SDK reports
    transport problems as free text in ``error_details`` with no exception
    object to inspect.
    """
    lowered = message.casefold()
    return any(hint in lowered for hint in _TRANSPORT_HINTS)


def is_transport_failure(exc: Exception) -> bool:
    """True when ``exc`` is a network failure rather than a response from Azure.

    The whole ``__cause__`` chain is inspected. The OpenAI SDK re-raises the
    real ``httpx``/``urllib3`` failure as the cause of a generic
    ``APIConnectionError`` whose own message is only ``"Connection error."``, so
    looking at the exception alone would miss every network fault.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        for klass in type(exc).__mro__:
            if klass.__name__ in _TRANSPORT_ERRORS:
                return True
        if is_transport_message(str(exc)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _region_from_endpoint(endpoint: str) -> str:
    """Derive a Speech region from a cognitive-services endpoint URL.

    The Speech SDK wants a bare region such as ``eastus``, but a .env that
    already carries the endpoint for the Vision-style variables has a full URL.
    The region is the first label of the host, so
    ``https://eastus.api.cognitive.microsoft.com`` yields ``eastus``.
    """
    host = (urlparse(endpoint).hostname or "").strip()
    if not host:
        return ""
    return host.split(".")[0]


def credential(key: str) -> str:
    """The configured value for ``key``, or "" when it is not set.

    An unrecognised key falls back to looking up ``key`` itself as an
    environment variable, so a new credential needs only a ``_ENV_NAMES`` entry
    to be spelled out - or nothing at all to reuse an existing variable name.
    """
    for name in _ENV_NAMES.get(key, (key,)):
        value = os.environ.get(name, "").strip()
        if value:
            # Only the last spelling of a credential may need normalising, and
            # the normalising ones (region from endpoint) are always last.
            if name.endswith("_ENDPOINT") and key == "azure_speech_region":
                return _region_from_endpoint(value)
            return value
    if key == "cloudflare_endpoint":
        return "https://api.cloudflare.com/client/v4"
    return ""


def missing_settings(keys) -> list[str]:
    """Credential keys from ``keys`` that have no usable value."""
    return [key for key in keys if not credential(key).strip()]


def missing_labels(keys) -> list[str]:
    """The .env variable names for the unset credentials in ``keys``."""
    return [SETTING_LABELS.get(key, key) for key in missing_settings(keys)]


def is_configured(keys) -> bool:
    return not missing_settings(keys)


def service_status(feature_key: str) -> dict:
    """Readiness for one service, for rendering its link in the header.

    Returns ``{"configured": bool, "missing": [env_name, ...]}``. Reading the
    environment cannot raise, so this is safe to call for every service on every
    page - which is exactly what the header does.
    """
    required = FEATURE_REQUIREMENTS.get(feature_key, ())
    labels = missing_labels(required)
    return {"configured": not labels, "missing": labels}


def require_configured(keys) -> None:
    """Raise :class:`ServiceNotConfigured` unless every key in ``keys`` is set."""
    labels = missing_labels(keys)
    if labels:
        raise ServiceNotConfigured(
            "This service has no credentials. An administrator must add these to "
            "the .env file and restart the application: " + ", ".join(labels)
        )


def tls_verification_disabled() -> bool:
    """Whether the administrator has asked to skip TLS certificate verification.

    Some corporate networks terminate TLS with an inspecting proxy whose CA is not
    in the machine trust store, and the Azure SDKs then fail every call with
    ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``. The
    ``azure_skip_tls_verify`` setting exists for those networks.

    It is read here rather than in the settings module because this is the only
    place that talks to the SDKs, and because the value has to be re-read per
    client - an administrator flips it in the UI and the next call honours it,
    without a restart.
    """
    from .settings import get_setting

    return get_setting("azure_skip_tls_verify", "true").strip().lower() == "true"


def openai_client_kwargs() -> dict:
    """Keyword arguments for ``openai.AzureOpenAI``, honouring the TLS setting.

    The OpenAI SDK speaks HTTP through ``httpx``, so verification is dropped by
    handing it a client that does not verify. Returns an empty dict when
    verification is on, so a caller can splat this unconditionally.
    """
    if not tls_verification_disabled():
        return {}
    import httpx

    return {"http_client": httpx.Client(verify=False)}


def azure_core_transport():
    """A ``requests`` transport with certificate verification disabled, or None.

    The azure-core SDKs (Vision, Speech) speak HTTP through ``requests``, so
    verification is dropped on the session their transport uses. Returns None
    when verification is on, so a caller can pass ``transport=`` only when it
    has something to pass.
    """
    if not tls_verification_disabled():
        return None
    import requests
    from azure.core.pipeline.transport import RequestsTransport

    session = requests.Session()
    session.verify = False
    return RequestsTransport(session=session, session_owner=False)
