"""Azure Speech synthesis for the Speech Test Service.

Text is turned into an MP3 and written under the instance folder so the page
can hand the browser a plain URL to play, rather than inlining megabytes of
base64 into the HTML. Only a small window of recent clips is kept per member.
"""

import uuid
from pathlib import Path

from flask import current_app

from .azure_ai import (
    credential,
    SPEECH_REQUIRED,
    ServiceError,
    is_transport_message,
    require_configured,
)

#: MP3 keeps the payload small enough to stream while still sounding natural,
#: which matters more here than fidelity - the point is a replayable preview.
OUTPUT_FORMAT = "Audio24Khz48KBitRateMonoMp3"
CONTENT_TYPE = "audio/mpeg"
FILE_SUFFIX = ".mp3"

MAX_CHARACTERS = 5000
#: Clips kept per member. Older ones are deleted as new ones are written.
KEEP_PER_MEMBER = 5

#: The voice picker offers a fixed catalogue rather than a free-text box.
#: Every name below is a real Azure Neural voice, taken from Microsoft's
#: published TTS language-support table, so selecting one cannot produce an
#: opaque "voice not found" from Azure.
#:
#: English accents carry two male and two female voices each. Azure publishes
#: only one male and one female Arabic voice per locale, so the Arabic accents
#: offer both of the voices that exist rather than four - a fourth per-gender
#: pair is not available in the catalogue to select.
VOICE_GROUPS = (
    {
        "accent": "English (United States)",
        "locale": "en-US",
        "voices": (
            ("Ava", "Female", "en-US-AvaNeural"),
            ("Emma", "Female", "en-US-EmmaNeural"),
            ("Andrew", "Male", "en-US-AndrewNeural"),
            ("Brian", "Male", "en-US-BrianNeural"),
        ),
    },
    {
        "accent": "English (United Kingdom)",
        "locale": "en-GB",
        "voices": (
            ("Sonia", "Female", "en-GB-SoniaNeural"),
            ("Libby", "Female", "en-GB-LibbyNeural"),
            ("Ryan", "Male", "en-GB-RyanNeural"),
            ("Thomas", "Male", "en-GB-ThomasNeural"),
        ),
    },
    {
        "accent": "English (Australia)",
        "locale": "en-AU",
        "voices": (
            ("Natasha", "Female", "en-AU-NatashaNeural"),
            ("Carly", "Female", "en-AU-CarlyNeural"),
            ("William", "Male", "en-AU-WilliamNeural"),
            ("Darren", "Male", "en-AU-DarrenNeural"),
        ),
    },
    {
        "accent": "English (India)",
        "locale": "en-IN",
        "voices": (
            ("Neerja", "Female", "en-IN-NeerjaNeural"),
            ("Aashi", "Female", "en-IN-AashiNeural"),
            ("Prabhat", "Male", "en-IN-PrabhatNeural"),
            ("Rehaan", "Male", "en-IN-RehaanNeural"),
        ),
    },
    {
        "accent": "Arabic (Egypt)",
        "locale": "ar-EG",
        "voices": (
            ("Salma", "Female", "ar-EG-SalmaNeural"),
            ("Shakir", "Male", "ar-EG-ShakirNeural"),
        ),
    },
    {
        "accent": "Arabic (Saudi Arabia)",
        "locale": "ar-SA",
        "voices": (
            ("Zariyah", "Female", "ar-SA-ZariyahNeural"),
            ("Hamed", "Male", "ar-SA-HamedNeural"),
        ),
    },
    {
        "accent": "Arabic (Morocco)",
        "locale": "ar-MA",
        "voices": (
            ("Mouna", "Female", "ar-MA-MounaNeural"),
            ("Jamal", "Male", "ar-MA-JamalNeural"),
        ),
    },
    {
        "accent": "Arabic (Qatar)",
        "locale": "ar-QA",
        "voices": (
            ("Amal", "Female", "ar-QA-AmalNeural"),
            ("Moaz", "Male", "ar-QA-MoazNeural"),
        ),
    },
)

#: Flat name -> (display name, gender) lookup, for labelling a chosen voice.
VOICE_INDEX = {
    name: (display, gender)
    for group in VOICE_GROUPS
    for display, gender, name in group["voices"]
}

#: Offered when nothing valid was submitted.
DEFAULT_VOICE = "en-US-AvaNeural"


def audio_dir() -> Path:
    """Where synthesised clips are written, created on first use."""
    root = Path(current_app.instance_path) / "speech_audio"
    root.mkdir(parents=True, exist_ok=True)
    return root


def default_voice() -> str:
    return DEFAULT_VOICE


def voice_label(name: str) -> str:
    """``Salma (Female)`` for a catalogue voice, or the raw name if unknown."""
    display, gender = VOICE_INDEX.get(name, (name, ""))
    return f"{display} ({gender})" if gender else display


def normalise_voice(voice: str | None) -> str:
    """Resolve a submitted voice to a name in the catalogue.

    The name is sent to Azure as part of a request body, so the check is not
    about tidiness: accepting a name outside the catalogue would turn a typo
    into an opaque upstream error. Anything unrecognised - including a crafted
    value that happens to match the general name shape - falls back to the
    default rather than being forwarded.
    """
    candidate = (voice or "").strip()
    if not candidate:
        return DEFAULT_VOICE
    if candidate in VOICE_INDEX:
        return candidate
    raise ServiceError(
        f"'{candidate}' is not one of the available voices. Pick one from the Voice list."
    )


#: The SDK collapses every failure into ``CancellationReason.Error``. These are
#: the ones a user can actually act on, keyed by the enum's short name. A reason
#: outside this table is free text from Azure and is shown verbatim, which beats
#: replacing it with a generic guess.
#: None of these name a Settings screen: the key and region live in the .env
#: file, and the voice is chosen from a fixed list, so telling someone to go and
#: edit either would send them somewhere the value does not exist.
_CANCEL_REASONS = {
    "Error": (
        "Azure Speech could not synthesise this text. Try a different voice, or ask "
        "an administrator to check the Speech key and region in the .env file."
    ),
    "ConnectionFailure": (
        "Azure Speech could not be reached. Ask an administrator to check the Speech "
        "region in the .env file and that this machine can reach it."
    ),
    "InvalidArgument": (
        "Azure Speech rejected the request. Pick a different voice from the Voice list."
    ),
}

_FALLBACK_CANCEL = "Azure Speech could not synthesise this text."


def _describe(detail: str) -> str:
    """Turn an Azure-reported failure into a sentence naming the thing to fix.

    Unlike :func:`_friendly_error` this never short-circuits on text we wrote
    ourselves, because it is only ever handed detail that came back from Azure.
    """
    lowered = detail.casefold()
    if "401" in lowered or "unauthorized" in lowered or "access denied" in lowered:
        return "Azure rejected the key. Ask an administrator to check the Speech key in the .env file."
    if "404" in lowered or "not found" in lowered:
        return "Azure Speech region not found. Ask an administrator to check the Speech region in the .env file."
    if "429" in lowered or "quota" in lowered:
        return "Azure Speech quota reached. Check the quota on your Speech resource."
    if is_transport_message(detail):
        return "Azure Speech could not be reached. Ask an administrator to check the Speech region in the .env file and that this machine has a route to it."
    return detail


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, ServiceError):
        return str(exc)
    message = str(exc).strip()
    return _describe(message) or exc.__class__.__name__


def _prune(member_id: int, keep_name: str) -> None:
    """Delete this member's clips beyond the retention window."""
    prefix = f"{member_id}-"
    try:
        clips = sorted(
            (path for path in audio_dir().glob(f"{prefix}*") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in clips[KEEP_PER_MEMBER:]:
            if path.name != keep_name:
                path.unlink(missing_ok=True)
    except OSError:
        # Retention is best-effort; never fail a synthesis because of it.
        pass


def synthesize(text: str, member_id: int, voice: str | None = None) -> dict:
    """Synthesise ``text`` and return the stored clip's details.

    Raises :class:`ServiceNotConfigured` when credentials are missing and
    :class:`ServiceError` for empty input, an invalid voice, or a failed
    synthesis.
    """
    require_configured(SPEECH_REQUIRED)

    text = (text or "").strip()
    if not text:
        raise ServiceError("Enter some text to convert to speech.")
    if len(text) > MAX_CHARACTERS:
        raise ServiceError(
            f"Text is too long ({len(text):,} characters). Keep it under {MAX_CHARACTERS:,}."
        )
    voice_name = normalise_voice(voice)

    from azure.cognitiveservices.speech import (
        ResultReason,
        SpeechConfig,
        SpeechSynthesisOutputFormat,
        SpeechSynthesizer,
    )

    speech_config = SpeechConfig(subscription=credential("azure_speech_key"), region=credential("azure_speech_region"))
    speech_config.speech_synthesis_voice_name = voice_name
    try:
        speech_config.speech_synthesis_output_format = getattr(
            SpeechSynthesisOutputFormat, OUTPUT_FORMAT
        )
    except AttributeError:  # pragma: no cover - guards an SDK rename
        speech_config.speech_synthesis_output_format = (
            SpeechSynthesisOutputFormat.Audio24Khz48KBitRateMonoMp3
        )

    # No AudioConfig: with no output target the SDK returns the encoded audio
    # on the result instead of writing a file, which is what we store. Pulling
    # the result explicitly (rather than speak_text's return value) also lets a
    # cancelled synthesis surface its reason instead of yielding silence.
    synthesizer = SpeechSynthesizer(speech_config=speech_config)
    try:
        result = synthesizer.speak_text_async(text).get()
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a flash
        raise ServiceError(_friendly_error(exc)) from exc
    finally:
        try:
            synthesizer.stop_speaking()
        except Exception:  # noqa: BLE001 - cleanup must not mask a result
            pass

    if result.reason == ResultReason.Canceled:
        # The SDK reports every failure - wrong key, wrong region, bad voice
        # name, quota, network - as the same bare "Error" enum, which tells the
        # user nothing actionable. CancellationReason separates the cases that
        # have different fixes, and error_details carries the HTTP-level reason
        # (an Azure 401 surfaces here, not as an exception, so the exception
        # path never sees it).
        details = getattr(result, "cancellation_details", None)
        reason = str(getattr(details, "reason", "") or "").strip()
        error_details = str(getattr(details, "error_details", "") or "").strip()
        if error_details:
            message = _describe(error_details)
        else:
            message = _CANCEL_REASONS.get(reason.rsplit(".", 1)[-1], reason or _FALLBACK_CANCEL)
        raise ServiceError(message)

    audio = result.audio_data
    if not audio:
        raise ServiceError("Azure Speech returned no audio. Try a different voice or shorter text.")

    name = f"{member_id}-{uuid.uuid4().hex}{FILE_SUFFIX}"
    target = audio_dir() / name
    target.write_bytes(bytes(audio))
    _prune(member_id, name)

    return {
        "name": name,
        "voice": voice_name,
        "characters": len(text),
        "bytes": len(audio),
        "content_type": CONTENT_TYPE,
    }
