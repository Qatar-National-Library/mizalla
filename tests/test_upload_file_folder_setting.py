"""Tests for the ``upload_file_folder`` setting.

The setting decides where the Upload File service writes. Two things can go
wrong and neither is visible until a member tries to upload, so both are pinned
here:

* an administrator points it at a folder that cannot hold files (a file, or a
  path running through one), and every upload then fails;
* the service keeps writing to the old folder after the setting changes, so the
  change appears not to have taken effect until a restart.
"""

import io
from pathlib import Path

import pytest

from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import FEATURE_UPLOAD, Member, UploadedFile
from mizalla.settings import (
    DEFAULT_APP_SETTINGS,
    DIRECTORY_SETTING_KEYS,
    get_setting,
    set_setting,
    validate_directory_setting,
)
from mizalla.upload_file import FOLDER_NAME, SETTING_KEY, upload_root

PASSWORD = "member-password-long"


def make_member(email="user@example.com", **kwargs):
    member = Member(name="User", email=email, **kwargs)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    sync_member_features(member, [FEATURE_UPLOAD])
    db.session.commit()
    return member


def login(client, email="user@example.com"):
    client.post("/apps/login", data={"email": email, "password": PASSWORD})


def upload(client, name="report.pdf", data=b"payload"):
    return client.post(
        "/apps/upload-file",
        data={"file": (io.BytesIO(data), name)},
        content_type="multipart/form-data",
    )


@pytest.fixture
def granted(app, client):
    with app.app_context():
        make_member()
    login(client)


def records(app):
    """Every stored record, oldest first, reduced to plain tuples.

    Plain values rather than model instances: a test client request pops the app
    context, and SQLAlchemy expires loaded rows on the next commit.
    """
    with app.app_context():
        rows = db.session.scalars(db.select(UploadedFile).order_by(UploadedFile.id)).all()
        return [(row.id, row.stored_name) for row in rows]


def admin_settings(client, **overrides):
    """POST the settings form as an admin, returning the response."""
    data = {"app_base_url": "http://localhost:5000/apps/mizalla"}
    data.update(overrides)
    return client.post("/apps/admin/settings", data=data)


# ─── The default ─────────────────────────────────────────────────────────────

def test_the_setting_defaults_to_blank(app):
    with app.app_context():
        assert get_setting(SETTING_KEY) == ""


def test_blank_uses_the_applications_uploads_folder(app):
    with app.app_context():
        assert upload_root() == Path(app.config["UPLOAD_FOLDER"]) / FOLDER_NAME


def test_the_service_still_owns_a_subfolder_of_the_configured_base(app, tmp_path):
    """Even when pointed at the shared uploads folder, files stay separated."""
    base = tmp_path / "shared"
    with app.app_context():
        set_setting(SETTING_KEY, str(base))
        assert upload_root() == base / FOLDER_NAME


# ─── Reading it ──────────────────────────────────────────────────────────────

def test_the_configured_folder_is_used(app, tmp_path):
    base = tmp_path / "elsewhere"
    with app.app_context():
        set_setting(SETTING_KEY, str(base))
        assert upload_root() == base / FOLDER_NAME
    assert (base / FOLDER_NAME).is_dir()


def test_the_folder_is_created_when_it_does_not_exist(app, tmp_path):
    base = tmp_path / "not" / "created" / "yet"
    with app.app_context():
        set_setting(SETTING_KEY, str(base))
        assert upload_root().is_dir()


def test_an_upload_lands_in_the_configured_folder(app, client, granted, tmp_path):
    base = tmp_path / "shared-drive"
    with app.app_context():
        set_setting(SETTING_KEY, str(base))
    upload(client, "report.pdf", b"payload")

    ((_, stored_name),) = records(app)
    assert (base / FOLDER_NAME / stored_name).read_bytes() == b"payload"
    # And not in the default location, which is what would happen if the service
    # cached the folder at boot.
    assert not (Path(app.config["UPLOAD_FOLDER"]) / FOLDER_NAME / stored_name).exists()


def test_a_download_still_works_from_the_configured_folder(app, client, granted, tmp_path):
    with app.app_context():
        set_setting(SETTING_KEY, str(tmp_path / "shared"))
    upload(client, "report.pdf", b"payload")
    ((record_id, _),) = records(app)

    response = client.get(f"/apps/upload-file/files/{record_id}/download")
    assert response.status_code == 200
    assert response.data == b"payload"


def test_a_delete_removes_the_file_from_the_configured_folder(app, client, granted, tmp_path):
    base = tmp_path / "shared"
    with app.app_context():
        set_setting(SETTING_KEY, str(base))
    upload(client, "report.pdf", b"payload")
    ((record_id, stored_name),) = records(app)

    assert client.post(f"/apps/upload-file/files/{record_id}/delete").status_code == 302
    assert not (base / FOLDER_NAME / stored_name).exists()


def test_a_change_takes_effect_without_a_restart(app, client, granted, tmp_path):
    """The point of a hot setting: no restart between one upload and the next."""
    with app.app_context():
        set_setting(SETTING_KEY, str(tmp_path / "first"))
    upload(client, "one.pdf", b"one")

    with app.app_context():
        set_setting(SETTING_KEY, str(tmp_path / "second"))
    upload(client, "two.pdf", b"two")

    ((_, first_stored), (_, second_stored)) = records(app)
    assert (tmp_path / "first" / FOLDER_NAME / first_stored).read_bytes() == b"one"
    assert (tmp_path / "second" / FOLDER_NAME / second_stored).read_bytes() == b"two"


def test_the_page_shows_where_files_are_stored(app, client, granted, tmp_path):
    base = tmp_path / "shared"
    with app.app_context():
        set_setting(SETTING_KEY, str(base))
    body = client.get("/apps/upload-file").get_data(as_text=True)
    assert str(base / FOLDER_NAME) in body


# ─── Blank means default again ───────────────────────────────────────────────

def test_clearing_the_setting_returns_to_the_default(app, client, granted, tmp_path):
    with app.app_context():
        set_setting(SETTING_KEY, str(tmp_path / "shared"))
        assert upload_root() != Path(app.config["UPLOAD_FOLDER"]) / FOLDER_NAME

        set_setting(SETTING_KEY, "")
        assert upload_root() == Path(app.config["UPLOAD_FOLDER"]) / FOLDER_NAME


def test_whitespace_only_is_treated_as_blank(app):
    with app.app_context():
        set_setting(SETTING_KEY, "   ")
        assert upload_root() == Path(app.config["UPLOAD_FOLDER"]) / FOLDER_NAME


# ─── Validation ──────────────────────────────────────────────────────────────

def test_a_blank_directory_setting_is_allowed():
    assert validate_directory_setting("", SETTING_KEY) == ""
    assert validate_directory_setting("   ", SETTING_KEY) == ""


def test_an_existing_folder_is_accepted(tmp_path):
    assert validate_directory_setting(str(tmp_path), SETTING_KEY) == str(tmp_path.resolve())


def test_a_relative_path_is_made_absolute(tmp_path, monkeypatch):
    """Anchored at save time, so it still means the same folder after a restart."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "store").mkdir()
    assert validate_directory_setting("store", SETTING_KEY) == str((tmp_path / "store").resolve())


def test_a_home_relative_path_is_expanded():
    result = validate_directory_setting("~/uploads", SETTING_KEY)
    assert result.startswith(str(Path.home()))
    assert "~" not in result


def test_a_path_that_is_a_file_is_refused(tmp_path):
    existing = tmp_path / "notes.txt"
    existing.write_text("not a folder")
    with pytest.raises(ValueError, match="is a file, not a folder"):
        validate_directory_setting(str(existing), SETTING_KEY)


def test_a_path_through_a_file_is_refused(tmp_path):
    """``/tmp/notes.txt/uploads`` cannot be created, and the ancestor check says why."""
    existing = tmp_path / "notes.txt"
    existing.write_text("not a folder")
    with pytest.raises(ValueError, match="cannot be created"):
        validate_directory_setting(str(existing / "uploads"), SETTING_KEY)


def test_validation_does_not_create_the_folder(tmp_path):
    """A settings save must not create directories as a side effect."""
    target = tmp_path / "not-created-yet"
    validate_directory_setting(str(target), SETTING_KEY)
    assert not target.exists()


def test_the_setting_is_registered_as_a_directory_setting():
    assert SETTING_KEY in DIRECTORY_SETTING_KEYS


# ─── The admin UI ────────────────────────────────────────────────────────────

def test_the_setting_appears_on_the_settings_page(app, client):
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
    login(client, "admin@example.com")
    body = client.get("/apps/admin/settings").get_data(as_text=True)
    assert SETTING_KEY in body
    assert 'name="upload_file_folder"' in body


def test_an_admin_can_set_the_folder_from_the_ui(app, client, tmp_path):
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
    login(client, "admin@example.com")

    assert admin_settings(client, upload_file_folder=str(tmp_path / "shared")).status_code == 302
    with app.app_context():
        assert get_setting(SETTING_KEY) == str((tmp_path / "shared").resolve())
        assert upload_root() == (tmp_path / "shared").resolve() / FOLDER_NAME


def test_a_member_cannot_set_the_folder(app, client, granted, tmp_path):
    with app.app_context():
        before = get_setting(SETTING_KEY)
    response = admin_settings(client, upload_file_folder=str(tmp_path / "shared"))
    assert response.status_code == 403
    with app.app_context():
        assert get_setting(SETTING_KEY) == before


def test_an_invalid_folder_is_refused_and_nothing_is_written(app, client, tmp_path):
    existing = tmp_path / "notes.txt"
    existing.write_text("not a folder")
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
    login(client, "admin@example.com")

    response = admin_settings(client, upload_file_folder=str(existing))
    assert response.status_code == 302
    body = client.get("/apps/admin/settings").get_data(as_text=True)
    assert "is a file, not a folder" in body
    with app.app_context():
        assert get_setting(SETTING_KEY) == ""


def test_saving_other_settings_still_works_alongside_it(app, client, tmp_path):
    """Regression guard: the new validation loop must not swallow the old fields."""
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
    login(client, "admin@example.com")

    assert admin_settings(client, upload_file_folder=str(tmp_path)).status_code == 302
    with app.app_context():
        assert get_setting("app_base_url") == "http://localhost:5000/apps/mizalla"


def test_a_rejected_folder_discards_the_whole_form(app, client, tmp_path):
    """One bad field refuses the save, as an invalid label list already does.

    Pinned because the alternative is worse: half-saving a settings form leaves an
    administrator believing they moved the upload folder when they did not.
    """
    existing = tmp_path / "notes.txt"
    existing.write_text("not a folder")
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
    login(client, "admin@example.com")

    admin_settings(client, upload_file_folder=str(existing), app_base_url="http://saved.example/app")
    with app.app_context():
        assert get_setting(SETTING_KEY) == ""
        assert get_setting("app_base_url") == DEFAULT_APP_SETTINGS["app_base_url"][0]


def test_resetting_the_setting_returns_to_blank(app, client):
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
        set_setting(SETTING_KEY, str(Path(app.config["UPLOAD_FOLDER"])))
    login(client, "admin@example.com")

    assert client.post("/apps/admin/settings", data={"reset": SETTING_KEY}).status_code == 302
    with app.app_context():
        assert get_setting(SETTING_KEY) == ""