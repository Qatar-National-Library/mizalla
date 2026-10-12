"""Tests for the Speech service.

The Azure SDK is stubbed out throughout: these cover the input validation and
the file handling around synthesis, none of which should need a live Azure
resource or the network.
"""

import re

import pytest

from mizalla.azure_ai import ServiceError, ServiceNotConfigured
from mizalla.extensions import db
from mizalla.models import Member
from mizalla.speech_service import (
    DEFAULT_VOICE,
    KEEP_PER_MEMBER,
    VOICE_GROUPS,
    VOICE_INDEX,
    audio_dir,
    normalise_voice,
    synthesize,
    voice_label,
)

PASSWORD = "member-password-long"


@pytest.fixture
def member(app):
    with app.app_context():
        user = Member(name="User", email="user@example.com")
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        yield user.id


def configure(credentials):
    """Give the Speech service the keys it would find in a real .env."""
    credentials.set()


class _FakeAudio:
    def __init__(self, data):
        self._data = data

    def __bytes__(self):
        return self._data

    def __len__(self):
        return len(self._data)


class _FakeResult:
    def __init__(self, audio=b"ID3fake-mp3-bytes", reason=None):
        self.audio_data = _FakeAudio(audio)
        self.reason = reason or _COMPLETED


class _Cancelled:
    """Mirrors a real cancelled result, including the SDK's reason enum.

    Using the real ``ResultReason.Canceled`` matters: the service compares
    against it, so a string stand-in would silently take the no-audio branch
    instead of reporting why the synthesis was cancelled.
    """

    def __init__(self, reason="Voice not available", error_details=""):
        from azure.cognitiveservices.speech import ResultReason

        self.reason = ResultReason.Canceled
        self.audio_data = _FakeAudio(b"")
        self.cancellation_details = type(
            "Details", (), {"reason": reason, "error_details": error_details}
        )()


class _CancelledWith(_Cancelled):
    """A cancellation the SDK reduced to its least useful form."""

    def __init__(self, reason, error_details=""):
        super().__init__(reason=f"CancellationReason.{reason}", error_details=error_details)


_COMPLETED = object()


class FakeSpeechConfig:
    """Records what the service asked for, so the tests can assert on it."""

    def __init__(self, subscription=None, region=None):
        self.subscription = subscription
        self.region = region
        self.speech_synthesis_voice_name = None
        self.speech_synthesis_output_format = None


def install_fake_speech(monkeypatch, result=None, raise_on_speak=None):
    """Replace the SDK's synthesiser so no test touches the network.

    SpeechConfig is replaced wholesale rather than patched: its real __init__
    opens a connection, and patching attribute setters on it is fragile.
    """
    import azure.cognitiveservices.speech as sdk

    class FakeResultFuture:
        def __init__(self, value):
            self._value = value

        def get(self):
            if isinstance(self._value, Exception):
                raise self._value
            return self._value

    class FakeSynthesizer:
        def __init__(self, speech_config=None, audio_config=None):
            self.speech_config = speech_config

        def speak_text_async(self, text):
            if raise_on_speak is not None:
                return FakeResultFuture(raise_on_speak)
            return FakeResultFuture(result or _FakeResult())

        def stop_speaking(self):
            pass

    monkeypatch.setattr(sdk, "SpeechSynthesizer", FakeSynthesizer)
    monkeypatch.setattr(sdk, "SpeechConfig", FakeSpeechConfig)


# ─── Configuration ───────────────────────────────────────────────────────────

def test_missing_credentials_raise_not_configured(app, member):
    with app.app_context():
        with pytest.raises(ServiceNotConfigured) as caught:
            synthesize("Hello", member)
    # The message names the .env variables to add, not a settings screen.
    message = str(caught.value)
    assert "MS_SPEECH_KEY" in message
    assert "MS_REGION" in message
    assert ".env" in message


# ─── Input validation ───────────────────────────────────────────────────────

def test_empty_text_is_rejected(app, credentials, member):
    with app.app_context():
        configure(credentials)
        with pytest.raises(ServiceError) as caught:
            synthesize("   ", member)
    assert "some text" in str(caught.value)


def test_overlong_text_is_rejected_before_the_call(app, credentials, member):
    with app.app_context():
        configure(credentials)
        with pytest.raises(ServiceError) as caught:
            synthesize("x" * 5001, member)
    assert "too long" in str(caught.value)


# A name outside the catalogue is refused outright rather than forwarded to
# Azure. This used to be a regex that accepted anything name-shaped; a typo then
# became an opaque upstream error naming a voice the user never picked.
@pytest.mark.parametrize(
    "voice",
    ["not a voice", "en_US_Ava!!", "en-US-Ava-Neural<script>", "en-US-AvaMultilingualNeural"],
)
def test_a_voice_outside_the_catalogue_is_rejected(app, credentials, member, voice):
    with app.app_context():
        configure(credentials)
        with pytest.raises(ServiceError) as caught:
            synthesize("Hello", member, voice)
    message = str(caught.value)
    assert "not one of the available voices" in message
    assert "Voice list" in message


def test_an_empty_voice_falls_back_to_the_catalogue_default(app):
    assert normalise_voice("") == DEFAULT_VOICE
    assert normalise_voice(None) == DEFAULT_VOICE
    assert normalise_voice("   ") == DEFAULT_VOICE


def test_a_valid_voice_is_kept():
    assert normalise_voice("en-GB-SoniaNeural") == "en-GB-SoniaNeural"
    assert normalise_voice("  ar-EG-SalmaNeural  ") == "ar-EG-SalmaNeural"


# ─── The voice catalogue ─────────────────────────────────────────────────────

def test_every_catalogue_entry_has_a_real_azure_name():
    """Every name must match the Azure locale-gender-name shape.

    The catalogue is hand-transcribed from Microsoft's published table, so this
    is the check that a typo does not reach the service as a valid-looking but
    nonexistent voice.
    """
    pattern = re.compile(r"^(en-US|en-GB|en-AU|en-IN|ar-EG|ar-SA|ar-MA|ar-QA)-[A-Za-z]+Neural$")
    for group in VOICE_GROUPS:
        for display, gender, name in group["voices"]:
            assert pattern.match(name), f"{name} is not an Azure voice name"
            assert name.startswith(group["locale"] + "-")
            assert gender in {"Male", "Female"}
            assert display in name


def test_each_accent_offers_two_male_and_two_female_voices_in_english():
    english = [g for g in VOICE_GROUPS if g["locale"].startswith("en-")]
    assert len(english) == 4
    for group in english:
        genders = [gender for _display, gender, _name in group["voices"]]
        assert genders.count("Male") == 2
        assert genders.count("Female") == 2


def test_each_arabic_accent_offers_the_one_male_and_one_female_azure_publishes():
    """Azure ships a single male and female Arabic voice per locale.

    The requirement was two of each per accent. That is not available to select,
    so the catalogue offers the pair that exists rather than padding the list
    with names Azure would reject. This test exists to make that explicit: if
    Azure ever adds more, the counts here are the ones to change.
    """
    arabic = [g for g in VOICE_GROUPS if g["locale"].startswith("ar-")]
    assert {g["locale"] for g in arabic} == {"ar-EG", "ar-SA", "ar-MA", "ar-QA"}
    for group in arabic:
        genders = [gender for _display, gender, _name in group["voices"]]
        assert genders.count("Male") == 1
        assert genders.count("Female") == 1


def test_voice_names_are_unique_across_the_catalogue():
    names = [name for group in VOICE_GROUPS for _d, _g, name in group["voices"]]
    assert len(names) == len(set(names))
    assert set(names) == set(VOICE_INDEX)


def test_voice_label_names_the_gender_and_falls_back_for_unknown_voices():
    assert voice_label("en-US-AvaNeural") == "Ava (Female)"
    assert voice_label("ar-QA-MoazNeural") == "Moaz (Male)"
    # A clip recorded before a catalogue change still has to render its name.
    assert voice_label("en-US-AvaMultilingualNeural") == "en-US-AvaMultilingualNeural"


# ─── Successful synthesis ───────────────────────────────────────────────────

def test_synthesis_writes_a_playable_file_and_reports_details(app, credentials, member, monkeypatch):
    with app.app_context():
        configure(credentials)
        install_fake_speech(monkeypatch)
        clip = synthesize("Hello there", member, "en-GB-SoniaNeural")

        assert clip["voice"] == "en-GB-SoniaNeural"
        assert clip["characters"] == len("Hello there")
        assert clip["content_type"] == "audio/mpeg"
        assert (audio_dir() / clip["name"]).is_file()
        assert (audio_dir() / clip["name"]).read_bytes() == b"ID3fake-mp3-bytes"


def test_a_cancelled_synthesis_raises_with_the_reason(app, credentials, member, monkeypatch):
    with app.app_context():
        configure(credentials)
        install_fake_speech(monkeypatch, result=_Cancelled())
        with pytest.raises(ServiceError) as caught:
            synthesize("Hello", member)
    assert "Voice not available" in str(caught.value)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("Error", "Speech key and region"),
        ("ConnectionFailure", "could not be reached"),
        ("InvalidArgument", "rejected the request"),
    ],
)
def test_a_bare_cancellation_reason_becomes_actionable_text(
    app, credentials, member, monkeypatch, reason, expected
):
    """The SDK reports every failure as the same enum; the user still needs a fix.

    A raw ``CancellationReason.Error`` shown on its own tells the user nothing
    they can act on, so each one is mapped to what to go and change. None of
    the mappings point at the Settings screen: the credentials live in .env and
    the voice comes from the picker.
    """
    with app.app_context():
        configure(credentials)
        install_fake_speech(monkeypatch, result=_CancelledWith(reason))
        with pytest.raises(ServiceError) as caught:
            synthesize("Hello", member)
    message = str(caught.value)
    assert expected in message
    assert "CancellationReason" not in message
    # Sending someone to a screen that no longer holds the key helps nobody.
    assert "Settings" not in message


def test_a_cancellation_with_http_details_is_reported_as_an_auth_failure(
    app, credentials, member, monkeypatch
):
    """Azure returns a 401 in the details, not as an exception, so it is mapped too."""
    with app.app_context():
        configure(credentials)
        install_fake_speech(
            monkeypatch, result=_CancelledWith("Error", "WebSocket connection failed: 401 Unauthorized")
        )
        with pytest.raises(ServiceError) as caught:
            synthesize("Hello", member)
    assert "key" in str(caught.value).lower()


def test_empty_audio_from_the_service_is_an_error(app, credentials, member, monkeypatch):
    with app.app_context():
        configure(credentials)
        install_fake_speech(monkeypatch, result=_FakeResult(audio=b""))
        with pytest.raises(ServiceError) as caught:
            synthesize("Hello", member)
    assert "no audio" in str(caught.value)


def test_an_auth_failure_says_the_key_is_wrong(app, credentials, member, monkeypatch):
    with app.app_context():
        configure(credentials)
        install_fake_speech(monkeypatch, raise_on_speak=Exception("401 Unauthorized"))
        with pytest.raises(ServiceError) as caught:
            synthesize("Hello", member)
    assert "key" in str(caught.value).lower()


# ─── Retention ──────────────────────────────────────────────────────────────

def test_only_a_window_of_clips_is_kept_per_member(app, credentials, member, monkeypatch):
    with app.app_context():
        configure(credentials)
        install_fake_speech(monkeypatch)
        names = [synthesize(f"Line {n}", member)["name"] for n in range(KEEP_PER_MEMBER + 3)]

        kept = {path.name for path in audio_dir().glob(f"{member}-*.mp3")}
        assert len(kept) == KEEP_PER_MEMBER
        # The newest survive, the oldest are pruned.
        assert names[-1] in kept
        assert names[0] not in kept


def test_one_member_does_not_prune_anothers_clips(app, credentials, monkeypatch):
    with app.app_context():
        configure(credentials)
        install_fake_speech(monkeypatch)
        first = Member(name="One", email="one@example.com")
        first.set_password(PASSWORD)
        second = Member(name="Two", email="two@example.com")
        second.set_password(PASSWORD)
        db.session.add_all([first, second])
        db.session.commit()

        mine = synthesize("Mine", first.id)["name"]
        for n in range(KEEP_PER_MEMBER + 2):
            synthesize(f"Theirs {n}", second.id)

        assert (audio_dir() / mine).is_file()


# ─── Route behaviour ────────────────────────────────────────────────────────

def test_audio_is_served_only_to_the_member_who_created_it(app, client, credentials):
    with app.app_context():
        from mizalla.feature_access import sync_member_features
        from mizalla.models import FEATURE_SPEECH

        credentials.set()

        owner = Member(name="Owner", email="owner@example.com")
        owner.set_password(PASSWORD)
        other = Member(name="Other", email="other@example.com")
        other.set_password(PASSWORD)
        db.session.add_all([owner, other])
        db.session.commit()
        sync_member_features(owner, [FEATURE_SPEECH])
        sync_member_features(other, [FEATURE_SPEECH])
        owner_id, other_id = owner.id, other.id
        db.session.commit()

        # The route only serves names shaped "<member id>-<32 hex>.mp3".
        token = "a" * 32
        (audio_dir() / f"{owner_id}-{token}.mp3").write_bytes(b"fake")

    client.post("/apps/login", data={"email": "owner@example.com", "password": PASSWORD})
    assert client.get(f"/apps/ai/speech/audio/{owner_id}-{token}.mp3").status_code == 200

    client.post("/apps/login", data={"email": "other@example.com", "password": PASSWORD})
    # Another member must not be able to read someone else's clip.
    assert client.get(f"/apps/ai/speech/audio/{owner_id}-{token}.mp3").status_code == 404


def test_a_traversal_attempt_in_the_audio_name_is_refused(app, client):
    with app.app_context():
        from mizalla.feature_access import sync_member_features
        from mizalla.models import FEATURE_SPEECH, Member as M

        user = M(name="User", email="user@example.com")
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        sync_member_features(user, [FEATURE_SPEECH])
        db.session.commit()

    client.post("/apps/login", data={"email": "user@example.com", "password": PASSWORD})
    for bad in ("../../secret", "1-nothex.mp3", "1-aaaa.mp3", "99999-" + "b" * 32 + ".wav"):
        assert client.get(f"/apps/ai/speech/audio/{bad}").status_code == 404
