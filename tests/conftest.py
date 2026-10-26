import os
import warnings

import pytest

from mizalla import create_app
from mizalla.extensions import db
from mizalla.azure_ai import _ENV_NAMES


def pytest_configure():
    warnings.filterwarnings(
        "ignore",
        message=r".*transform is deprecated.*",
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r".*text_matrix is deprecated.*",
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r".*instantiateVariableFont.*",
        category=UserWarning,
    )

#: Every secret the app reads from the environment. ``create_app`` loads the real
#: ``.env`` so that WSGI, ``flask run``, and a test-built app all agree on their
#: configuration - which means a test would otherwise inherit the developer's
#: live keys. That breaks tests two ways: a service would look configured when
#: the test meant to prove it is not, and a test that falls through to a real
#: HTTP call would POST to the live API. So they are all cleared per test.
#:
#: ``SECRET_KEY`` is deliberately absent: the fixtures supply their own, and
#: Flask needs one to sign the session cookie.
SECRET_ENV_VARS = {name for names in _ENV_NAMES.values() for name in names} | {
    "SENDGRID_API_KEY",
}


def _clear_secrets():
    for name in SECRET_ENV_VARS:
        os.environ.pop(name, None)


class Credentials:
    """Test-time stand-in for the .env file.

    The services read their keys from the environment, so a test that needs a
    configured Vision or Speech has to put a fake key in the environment too.
    Setting the internal key (``azure_vision_key``) clears it by name, so a test
    can say "everything except this one".
    """

    #: Internal credential key -> the .env variable name the code reads.
    ENV_NAMES = {
        "azure_vision_endpoint": "MS_VISION_ENDPOINT",
        "azure_vision_key": "MS_VISION_KEY",
        "azure_speech_key": "MS_SPEECH_KEY",
        "azure_speech_region": "MS_REGION",
        "azure_speech_endpoint": "MS_SPEECH_ENDPOINT",
        "azure_openai_endpoint": "AZURE_OPENAI_ENDPOINT",
        "azure_openai_key": "AZURE_OPENAI_KEY",
        "azure_openai_deployment": "AZURE_OPENAI_DEPLOYMENT_NAME",
        "azure_openai_api_version": "AZURE_OPENAI_API_VERSION",
        "pingram_api_key": "PINGRAM_API_KEY",
        "pingram_region": "PINGRAM_REGION_CODE",
        "cloudflare_account_id": "CLOUDFLARE_ACCOUNT_ID",
        "cloudflare_api_token": "CLOUDFLARE_API_TOKEN",
        "cloudflare_endpoint": "CLOUDFLARE_ENDPOINT",
        "google_analytics_apikey_json": "GOOGLE_ANALYTICS_APIKEY_JSON",
    }

    DEFAULTS = {
        "azure_vision_endpoint": "https://vision.cognitiveservices.azure.com",
        "azure_vision_key": "vision-key",
        "azure_speech_key": "speech-key",
        "azure_speech_region": "eastus",
        "azure_openai_endpoint": "https://openai.openai.azure.com",
        "azure_openai_key": "openai-key",
        "azure_openai_deployment": "gpt-deployment",
        "pingram_api_key": "pingram-key",
        "pingram_region": "us",
        # Not a real account: it only has to be long enough to be recognised as
        # an id, since no test is permitted to reach Cloudflare.
        "cloudflare_account_id": "acct0000test",
        "cloudflare_api_token": "cloudflare-token",
        "cloudflare_endpoint": "https://api.cloudflare.test/client/v4",
        "google_analytics_apikey_json": "apikey_GoogleAnalytics.json",
    }

    def set(self, **overrides):
        """Configure every service, with ``overrides`` replacing named keys.

        An override of ``""`` unsets that key, which is how a test asks "what
        happens when this one credential is missing?".
        """
        for key, value in {**self.DEFAULTS, **overrides}.items():
            if value == "":
                os.environ.pop(self.ENV_NAMES[key], None)
            else:
                os.environ[self.ENV_NAMES[key]] = value

    def clear(self, *keys):
        """Unset specific keys, or every known key when called with none."""
        targets = keys or tuple(self.ENV_NAMES)
        for key in targets:
            os.environ.pop(self.ENV_NAMES[key], None)


@pytest.fixture
def credentials():
    return Credentials()


@pytest.fixture(autouse=True)
def clean_secrets():
    """Isolate the live credentials per test, in both directions.

    Before: the developer's real ``.env`` must not decide whether a service
    renders as configured, nor hand a test a working key to call a live API
    with. After: values a test injected must not reach the next one.
    """
    saved = {name: os.environ.get(name) for name in SECRET_ENV_VARS}
    _clear_secrets()
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture
def app(tmp_path, clean_secrets):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-key",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "WTF_CSRF_ENABLED": False,
        }
    )
    # create_app reads the real .env back in (override=False), so the isolation
    # has to be re-applied after it - otherwise a developer's own keys decide
    # whether a service renders as configured.
    _clear_secrets()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()
