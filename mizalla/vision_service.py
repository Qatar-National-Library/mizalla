"""Azure AI Vision analysis for the Vision Test Service.

One ``analyze`` call returns the recognised text of the image and the complete
service payload. The payload is also mapped into named fields using the admin's
JSON mapping.

Only the READ feature is requested. CAPTION is not, because it is unavailable in
some Azure regions: the resource rejects the whole call with
"The feature 'Caption' is not supported in this region", which would take OCR
down with it even though OCR works fine. READ is the feature this service is
built on - the field mapping reads the OCR text - so requesting only what is
needed keeps the service working in every region. A deployment that does support
CAPTION can add ``VisualFeatures.CAPTION`` back and the caption will populate
without any other change.

Mapping resolution is a waterfall, tried per field, in this order:

1. **Response path** - the value is read as a path into the Vision response,
   e.g. ``readResult.blocks.0.lines.0.text``.
2. **Label** - the value names a label to look for in the OCR text, and the
   value is whatever follows it, e.g. ``ID:``.
3. **Whole OCR text** - the value is empty, or nothing above matched.

Response keys are matched case-insensitively and ``camelCase`` is treated the
same as ``snake_case``, so a path written either way resolves.
"""

import json
import re

from .azure_ai import (
    credential,
    VISION_REQUIRED,
    ServiceError,
    is_transport_failure,
    require_configured,
    azure_core_transport,
)
from .settings import get_setting

# A path is split on dots and [n] indexes, e.g. "readResult.blocks[0].lines[0].text".
_PATH_TOKEN = re.compile(r"[^.\[\]]+|\[\d+\]")
_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")

#: Fields the Vision service can be asked for.
DEFAULT_MAPPING = '{"ID": ""}'


def default_mapping() -> dict:
    """The mapping used when an admin has not saved a usable one."""
    return {"ID": ""}


def load_mapping(raw: str | None = None) -> tuple[dict, str | None]:
    """Parse the admin's mapping JSON.

    Returns ``(mapping, error)``. A malformed or non-object value falls back to
    :func:`default_mapping` with an explanation, so a typo in the settings page
    degrades to "ID = the image's text" instead of breaking the service.
    """
    if raw is None:
        raw = get_setting("vision_id_mapping")
    raw = (raw or "").strip()
    if not raw:
        return default_mapping(), None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return default_mapping(), f"Mapping is not valid JSON ({exc.msg} at position {exc.pos}). Using the default."
    if not isinstance(parsed, dict) or not parsed:
        return default_mapping(), "Mapping must be a JSON object with at least one field. Using the default."

    mapping: dict[str, str] = {}
    for name, value in parsed.items():
        field = str(name).strip()
        if not field:
            continue
        if isinstance(value, (int, float, bool)):
            value = str(value)
        elif value is None:
            value = ""
        elif not isinstance(value, str):
            # A nested object is not a usable pointer; keep it as JSON text so
            # path resolution fails and the waterfall falls through.
            value = json.dumps(value)
        mapping[field] = value.strip()
    if not mapping:
        return default_mapping(), "Mapping contained no usable field names. Using the default."
    return mapping, None


def _snake(name: str) -> str:
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def _normalise(value):
    """Recursively rewrite mapping keys to snake_case for path lookups."""
    if isinstance(value, dict):
        return {_snake(str(key)): _normalise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    return value


def _as_text(value) -> str:
    """Flatten a resolved value into displayable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_as_text(item) for item in value]
        return " ".join(part for part in parts if part)
    if isinstance(value, dict):
        # A ReadResult word object: prefer its text over the whole JSON blob.
        for key in ("text", "content", "value"):
            if key in value and isinstance(value[key], str):
                return value[key]
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def resolve_path(data, path: str):
    """Walk a dotted/indexed path into ``data``. Returns None if it does not exist."""
    path = path.strip()
    if not path:
        return None
    current = data
    for token in _PATH_TOKEN.findall(path):
        # Both "blocks[0]" and "blocks.0" mean the same index, so a bare run of
        # digits is an index too - not a mapping key.
        if token.isdigit() or token.startswith("["):
            index = int(token.strip("[]"))
            if not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]
        else:
            key = _snake(token)
            if isinstance(current, dict):
                if key not in current:
                    return None
                current = current[key]
            elif isinstance(current, list):
                # Allow a bare field name to fan out across a list of objects,
                # which is what "lines.text" usually means for a Read result.
                collected = []
                for item in current:
                    if isinstance(item, dict) and key in item:
                        collected.append(item[key])
                if not collected:
                    return None
                current = collected
            else:
                return None
    return current


def _read_result(payload: dict) -> dict:
    """The OCR half of a Vision response.

    Three spellings are accepted because three shapes arrive here. Azure sends
    ``readResult``; :func:`analyze_image` snake-cases the payload before calling
    this, so the key is ``read_result``; and ``read`` is what the API's own
    documentation examples and any hand-built payload use.

    The spelling matters more than it looks. Reading the wrong one returns an
    empty dict rather than raising, so the service reports success while the
    recognised text and every field mapped from it come back blank.
    """
    for key in ("readResult", "read_result", "read"):
        value = payload.get(key)
        if value:
            return value
    return {}


def ocr_lines(payload: dict) -> list[str]:
    """The recognised text of the image, one entry per detected line."""
    blocks = _read_result(payload).get("blocks") or []
    lines: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for line in block.get("lines") or []:
            if isinstance(line, dict):
                text = (line.get("text") or "").strip()
            else:
                text = str(line).strip()
            if text:
                lines.append(text)
    return lines


def ocr_text(payload: dict) -> str:
    return "\n".join(ocr_lines(payload))


def _after_label(lines: list[str], label: str) -> str | None:
    """Find ``label`` in the OCR text and return the value that follows it.

    Handles both ``ID: A-1042`` (value on the same line) and a label sitting
    alone above its value (common on cards and forms).
    """
    needle = label.strip().rstrip(":").strip()
    if not needle:
        return None
    # Match on word boundaries: a plain substring search for "ID" would hit
    # "IDENTIFICATION" and report the rest of that word as the ID.
    pattern = re.compile(rf"(?<![0-9A-Za-z]){re.escape(needle)}(?![0-9A-Za-z])", re.IGNORECASE)
    for index, line in enumerate(lines):
        match = pattern.search(line)
        if match is None:
            continue
        remainder = line[match.end():].strip().strip(":").strip()
        if remainder:
            return remainder
        for following in lines[index + 1:]:
            if following.strip():
                return following.strip()
    return None


def resolve_field(payload: dict, lines: list[str], value: str) -> tuple[str, str]:
    """Resolve one mapping value. Returns ``(text, strategy)``."""
    if value:
        found = _as_text(resolve_path(payload, value))
        if found:
            return found, "response-path"
        found = _after_label(lines, value)
        if found:
            return found, "label"
    whole = "\n".join(lines).strip()
    return whole, "ocr-text"


def _friendly_error(exc: Exception) -> str:
    """Turn an SDK/HTTP failure into something an admin can act on."""
    status = getattr(exc, "status_code", None)
    if status == 401 or status == 403:
        return "Azure rejected the key. Check the Azure Vision key in Settings."
    if status == 404:
        return "Azure Vision endpoint not found. Check the endpoint in Settings."
    if status == 415:
        return "Azure could not read that file as an image. Upload a JPG, PNG, BMP, GIF or WEBP."
    if status == 429:
        return "Azure Vision rate limit reached. Try again in a moment."
    if is_transport_failure(exc):
        return (
            "Azure Vision could not be reached. Check the endpoint in Settings and "
            "that this machine has a route to it."
        )
    message = str(exc).strip()
    return message or exc.__class__.__name__


def analyze_image(image_bytes: bytes, mapping: dict | None = None) -> dict:
    """Analyse an image and return the recognised text, the payload, and the mapping.

    ``caption`` is present in the result but empty unless the request asks for
    :class:`VisualFeatures.CAPTION`; see the module docstring for why it does not.

    Raises :class:`ServiceNotConfigured` when credentials are missing and
    :class:`ServiceError` when Azure rejects or fails the request.
    """
    require_configured(VISION_REQUIRED)
    if not image_bytes:
        raise ServiceError("The uploaded file was empty.")

    from azure.ai.vision.imageanalysis import ImageAnalysisClient
    from azure.ai.vision.imageanalysis.models import VisualFeatures
    from azure.core.credentials import AzureKeyCredential

    client = ImageAnalysisClient(
        endpoint=credential("azure_vision_endpoint").rstrip("/"),
        credential=AzureKeyCredential(credential("azure_vision_key")),
        transport=azure_core_transport(),
    )
    try:
        result = client.analyze(
            image_data=image_bytes,
            visual_features=[VisualFeatures.READ],
        )
        payload = result.as_dict()
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a flash
        raise ServiceError(_friendly_error(exc)) from exc
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - closing must never mask a result
            pass

    if not isinstance(payload, dict):
        raise ServiceError("Azure Vision returned an unexpected response.")

    # Empty unless CAPTION is requested above; the page falls back to a message.
    caption_result = getattr(result, "caption", None)
    caption = (getattr(caption_result, "text", "") or "").strip()

    normalised = _normalise(payload)
    lines = ocr_lines(normalised)
    fields = {
        name: resolve_field(normalised, lines, value or "")
        for name, value in (mapping or default_mapping()).items()
    }

    return {
        "caption": caption,
        "caption_confidence": getattr(caption_result, "confidence", None),
        "payload": payload,
        "json_text": json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        "ocr_text": "\n".join(lines),
        "ocr_line_count": len(lines),
        "fields": fields,
    }
