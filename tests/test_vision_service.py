"""Tests for the Vision service response mapping.

The mapping is a waterfall per field: try the value as a path into the Vision
response, then as a label to find in the recognised text, then fall back to the
whole recognised text. These tests pin each rung, and the case-normalisation
that lets a path be written in camelCase or snake_case.
"""

import json

import pytest

from mizalla.azure_ai import ServiceError, ServiceNotConfigured
from mizalla.vision_service import (
    analyze_image,
    default_mapping,
    load_mapping,
    ocr_lines,
    ocr_text,
    resolve_field,
    resolve_path,
)

#: The shape ``result.as_dict()`` really returns. The key is ``readResult`` -
#: recorded from a live response, not from the API's documentation examples, which
#: show ``read``. This fixture previously used ``read``, and the parser looked for
#: ``read`` too, so the pair agreed with each other and with neither Azure. Against
#: the real endpoint the service reported success with no text and no mapped
#: fields. Anything added here should match what Azure sends.
PAYLOAD = {
    "metadata": {},
    "modelVersion": "2022-04-30",
    "readResult": {
        "blocks": [
            {
                "lines": [
                    {"text": "QNL IDENTIFICATION", "words": [{"text": "QNL"}]},
                    {"text": "ID: A-1042", "words": [{"text": "ID:"}, {"text": "A-1042"}]},
                ]
            }
        ]
    },
}


# ─── Mapping parsing ────────────────────────────────────────────────────────

def test_empty_mapping_falls_back_to_the_default():
    mapping, error = load_mapping("")
    assert mapping == default_mapping()
    assert error is None


def test_malformed_json_degrades_instead_of_raising():
    mapping, error = load_mapping("{not json")
    assert mapping == default_mapping()
    assert "not valid JSON" in error


def test_a_json_array_is_rejected_with_a_reason():
    mapping, error = load_mapping('["ID"]')
    assert mapping == default_mapping()
    assert "JSON object" in error


def test_numeric_and_null_values_are_coerced():
    mapping, error = load_mapping('{"ID": 42, "Name": null}')
    assert error is None
    assert mapping == {"ID": "42", "Name": ""}


# ─── Path resolution ────────────────────────────────────────────────────────

def test_dotted_and_indexed_paths_resolve():
    from mizalla.vision_service import _normalise

    payload = _normalise(PAYLOAD)
    assert resolve_path(payload, "readResult.blocks.0.lines.0.text") == "QNL IDENTIFICATION"
    assert resolve_path(payload, "readResult.blocks[0].lines[1].text") == "ID: A-1042"


def test_a_bare_field_name_fans_out_across_a_list():
    from mizalla.vision_service import _normalise

    found = resolve_path(_normalise(PAYLOAD), "readResult.blocks.0.lines.text")
    assert found == ["QNL IDENTIFICATION", "ID: A-1042"]


def test_a_missing_path_returns_none_rather_than_raising():
    from mizalla.vision_service import _normalise

    assert resolve_path(_normalise(PAYLOAD), "readResult.blocks.9.lines.0.text") is None
    assert resolve_path(PAYLOAD, "nope.nothing") is None
    assert resolve_path(PAYLOAD, "") is None


def test_camel_case_and_snake_case_are_equivalent():
    """The SDK serialises keys both ways depending on the path taken, so a
    path written either way has to resolve to the same value."""
    from mizalla.vision_service import _normalise

    normalised = _normalise({"readResult": {"blocks": [{"boundingRegions": []}]}})
    assert resolve_path(normalised, "read_result.blocks.0.bounding_regions") == []
    # camelCase and bracket indexes resolve against the normalised copy too.
    assert resolve_path(normalised, "readResult.blocks[0].boundingRegions") == []


# ─── OCR text ───────────────────────────────────────────────────────────────

def test_ocr_lines_and_text():
    assert ocr_lines(PAYLOAD) == ["QNL IDENTIFICATION", "ID: A-1042"]
    assert ocr_text(PAYLOAD) == "QNL IDENTIFICATION\nID: A-1042"


# ─── The waterfall ───────────────────────────────────────────────────────────

def test_a_path_is_preferred_over_a_label():
    from mizalla.vision_service import _normalise

    # Mirrors analyze_image, which normalises before resolving.
    payload = _normalise(PAYLOAD)
    text, strategy = resolve_field(payload, ocr_lines(payload), "readResult.blocks.0.lines.1.text")
    assert text == "ID: A-1042"
    assert strategy == "response-path"


def test_a_bogus_path_ends_at_the_ocr_text_rung():
    """An unmatched path is not also a usable label, so it falls to the end."""
    from mizalla.vision_service import _normalise

    payload = _normalise(PAYLOAD)
    _text, strategy = resolve_field(payload, ocr_lines(payload), "readResult.blocks.7.lines.7.text")
    assert strategy == "ocr-text"


def test_a_label_that_is_a_prefix_of_another_word_does_not_match():
    """'QNL' must not be found inside a longer token."""
    payload = {"readResult": {"blocks": [{"lines": [{"text": "SOMETHINGQQNLER"}]}]}}
    _text, strategy = resolve_field(payload, ocr_lines(payload), "QNL")
    assert strategy == "ocr-text"


def test_a_label_matching_on_a_word_boundary_is_found():
    text, strategy = resolve_field(PAYLOAD, ocr_lines(PAYLOAD), "QNL")
    assert text == "IDENTIFICATION"
    assert strategy == "label"


def test_a_label_alone_on_its_line_takes_the_next_line():
    payload = {"readResult": {"blocks": [{"lines": [{"text": "ID:"}, {"text": "B-77"}]}]}}
    text, strategy = resolve_field(payload, ocr_lines(payload), "ID:")
    assert text == "B-77"
    assert strategy == "label"


def test_an_empty_value_returns_the_whole_ocr_text():
    text, strategy = resolve_field(PAYLOAD, ocr_lines(PAYLOAD), "")
    assert text == "QNL IDENTIFICATION\nID: A-1042"
    assert strategy == "ocr-text"


def test_a_label_that_is_nowhere_still_falls_back_to_ocr_text():
    text, strategy = resolve_field(PAYLOAD, ocr_lines(PAYLOAD), "MISSING:")
    assert strategy == "ocr-text"
    assert "QNL" in text


# ─── analyze_image ───────────────────────────────────────────────────────────

def test_missing_credentials_raise_not_configured(app):
    with app.app_context():
        with pytest.raises(ServiceNotConfigured) as caught:
            analyze_image(b"\xff\xd8\xffdata")
    # The message names the .env variables to add, not a settings screen.
    message = str(caught.value)
    assert "MS_VISION_ENDPOINT" in message
    assert "MS_VISION_KEY" in message
    assert ".env" in message


def test_an_empty_upload_is_rejected_before_any_call(app, credentials):
    with app.app_context():
        credentials.set()
        with pytest.raises(ServiceError):
            analyze_image(b"")


class _FakeCaption:
    text = "A printed identity card on a desk."
    confidence = 0.71


class _FakeRead:
    def __init__(self, payload):
        self.blocks = payload["readResult"]["blocks"]


def _wants_caption(features) -> bool:
    return any(getattr(f, "name", "") == "CAPTION" for f in features)


class _FakeResult:
    """Mirrors the real SDK: ``caption`` exists only if CAPTION was requested.

    Modelling that matters because it is what makes the region failure visible in
    a test - a fake that always carries a caption hides the fact that the service
    now depends on a feature some regions do not offer.
    """

    def __init__(self, payload, features):
        self.read = _FakeRead(payload)
        self._payload = payload
        if _wants_caption(features):
            self.caption = _FakeCaption()

    def as_dict(self):
        return self._payload


class _FakeClient:
    """Stands in for ImageAnalysisClient so the test needs no network."""

    last_args = None

    def __init__(self, endpoint, credential, **kwargs):
        self.endpoint = endpoint
        # kwargs is accepted, not read: the real client takes a ``transport``
        # keyword for the TLS setting, and a fake that rejects it would fail
        # before the behaviour under test could run.

    def analyze(self, image_data, visual_features):
        _FakeClient.last_args = {"data": image_data, "features": visual_features}
        return _FakeResult(PAYLOAD, visual_features)

    def close(self):
        pass


def test_analyze_returns_all_three_outputs_together(app, credentials, monkeypatch):
    with app.app_context():
        credentials.set(azure_vision_endpoint="https://example.cognitiveservices.azure.com")

        import azure.ai.vision.imageanalysis as sdk

        monkeypatch.setattr(sdk, "ImageAnalysisClient", _FakeClient)
        result = analyze_image(b"\xff\xd8\xffimagedata", {"ID": "ID:"})

        # CAPTION is not requested, so the real SDK returns no caption and the
        # page falls back. The OCR half is what this service is built on.
        assert result["caption"] == ""
        assert "readResult" in json.loads(result["json_text"])
        assert result["fields"]["ID"] == ("A-1042", "label")
        assert result["ocr_line_count"] == 2
        # The endpoint is passed through with no trailing slash, or Azure rejects it.
        assert _FakeClient.last_args["data"] == b"\xff\xd8\xffimagedata"


def test_only_the_read_feature_is_requested(app, credentials, monkeypatch):
    """Asking for CAPTION too takes OCR down in regions that do not offer it.

    Azure answers "The feature 'Caption' is not supported in this region" and
    rejects the entire call, so the request that reads text never runs. This
    pins the request to READ alone.
    """
    with app.app_context():
        credentials.set(azure_vision_endpoint="https://example.cognitiveservices.azure.com")

        import azure.ai.vision.imageanalysis as sdk

        monkeypatch.setattr(sdk, "ImageAnalysisClient", _FakeClient)
        analyze_image(b"\xff\xd8\xffimagedata")

        requested = [getattr(f, "name", "") for f in _FakeClient.last_args["features"]]
        assert requested == ["READ"]


def test_ocr_text_is_read_from_readresult_the_key_azure_actually_sends():
    """The silent failure: wrong key, empty result, no error.

    ``ocr_lines`` looked for ``read`` while Azure sends ``readResult``, so a
    correct response produced zero lines. Nothing raised - the page said success
    and showed no text, and every mapped field fell through to the empty-OCR
    branch. The test fixture had the same wrong key, so the pair agreed with each
    other and neither agreed with Azure.
    """
    lines = ocr_lines(PAYLOAD)
    assert lines == ["QNL IDENTIFICATION", "ID: A-1042"]
    assert ocr_text(PAYLOAD) == "QNL IDENTIFICATION\nID: A-1042"


def test_a_payload_using_the_old_read_key_still_resolves():
    """Hand-built payloads and saved mappings may still say ``read``."""
    payload = {"read": {"blocks": [{"lines": [{"text": "ID: A-1042"}]}]}}
    assert ocr_lines(payload) == ["ID: A-1042"]


def test_ocr_text_survives_the_normalisation_analyze_image_applies():
    """The key reaching ocr_lines in production is ``read_result``, not ``readResult``.

    analyze_image snake-cases the payload before reading it, so accepting only the
    wire spelling would trade one silent empty result for another.
    """
    from mizalla.vision_service import _normalise

    normalised = _normalise(PAYLOAD)
    assert "read_result" in normalised
    assert "readResult" not in normalised
    assert ocr_lines(normalised) == ["QNL IDENTIFICATION", "ID: A-1042"]


def test_a_network_failure_does_not_leak_the_endpoint_or_the_stack(app, credentials, monkeypatch):
    """A transport failure is replaced, not shown raw.

    ``str(exc)`` for a DNS failure names the internal host and the transport
    stack. That reaches the member who uploaded the image and tells them nothing
    they can fix, so the raw text must not survive into the flash.
    """
    with app.app_context():
        credentials.set(azure_vision_endpoint="https://internal.cognitiveservices.azure.com")

        import azure.ai.vision.imageanalysis as sdk

        raw = (
            "HTTPSConnection(host='internal.cognitiveservices.azure.com', port=443): "
            "Max retries exceeded with url: /computervision/imageanalysis:fail "
            "(Caused by NameResolutionError(...))"
        )

        class _Unreachable:
            def __init__(self, endpoint, credential, **kwargs):
                # kwargs accepted for the same reason as _FakeClient: the real
                # client takes a ``transport`` keyword.
                pass

            def analyze(self, image_data, visual_features):
                raise Exception(raw)

            def close(self):
                pass

        monkeypatch.setattr(sdk, "ImageAnalysisClient", _Unreachable)

        with pytest.raises(ServiceError) as caught:
            analyze_image(b"\xff\xd8\xffimagedata", {"ID": "ID:"})

    message = str(caught.value)
    assert "could not be reached" in message
    assert "internal.cognitiveservices.azure.com" not in message
    assert "NameResolutionError" not in message
