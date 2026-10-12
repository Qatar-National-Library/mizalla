"""Tests for the Upload File service.

The service is small, but three of its behaviours are load-bearing and easy to
regress silently, so they are pinned here:

* the grant gates every route, not just the header link;
* an uploaded ``.html`` comes back as a *download*, never as a rendered page -
  serving it inline would be stored XSS in the app's own origin;
* the on-disk name is generated, so two members may both upload ``report.docx``.

Access is granted through the same ``AppFeatureAccess`` table the admin member
form writes to, so these tests exercise the real grant rather than a stub.

Model attributes are read inside ``app.app_context()`` and reduced to plain ids
there: SQLAlchemy expires everything on commit, so touching an attribute after a
test client request has popped the context raises rather than answering.
"""

import io
from pathlib import Path

import pytest

from mizalla import create_app
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    FEATURE_UPLOAD,
    AppFeatureAccess,
    Member,
    UploadedFile,
)
from mizalla.upload_file import (
    ACCEPT_ATTRIBUTE,
    ALLOWED_SUFFIXES,
    FOLDER_NAME,
)

PASSWORD = "member-password-long"

#: Exactly what the service was asked to accept.
EXPECTED_SUFFIXES = {
    ".pdf", ".txt", ".docx", ".xlsx", ".ppt", ".pptx", ".doc", ".html", ".htm",
}

#: Types near the allowed set that must still be refused.
REFUSED_SUFFIXES = [".exe", ".png", ".zip", ".md", ".csv", ".xls", ".rtf", ".js"]


def make_member(email="user@example.com", name="User", **kwargs):
    member = Member(name=name, email=email, **kwargs)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def grant(member):
    sync_member_features(member, [FEATURE_UPLOAD])
    db.session.commit()


def login(client, email="user@example.com"):
    client.post("/apps/login", data={"email": email, "password": PASSWORD})


def upload(client, name="report.pdf", data=b"hello"):
    return client.post(
        "/apps/upload-file",
        data={"file": (io.BytesIO(data), name)},
        content_type="multipart/form-data",
    )


@pytest.fixture
def uploader(app, client):
    """A signed-in member who has been granted the service."""
    with app.app_context():
        grant(make_member())
    login(client)


def folder(app):
    return Path(app.config["UPLOAD_FOLDER"]) / FOLDER_NAME


def files_on_disk(app):
    """Stored filenames, or nothing at all if the folder was never created.

    A rejected upload never reaches the point of writing, so it also never
    creates the folder - and "no folder" is the strongest statement available
    that nothing was written.
    """
    return sorted(p.name for p in folder(app).iterdir()) if folder(app).is_dir() else []


def only_record(app):
    """The single stored file, reduced to plain values safe to use later."""
    with app.app_context():
        record = db.session.scalar(db.select(UploadedFile))
        return record.id, record.filename, record.stored_name


def member_id(email):
    """The id of a member by email. Requires an open app context."""
    return db.session.scalar(db.select(Member.id).filter_by(email=email))


# ─── The accepted set ────────────────────────────────────────────────────────

def test_the_accepted_types_are_exactly_the_ones_asked_for():
    """A guard on the list itself: adding or dropping a type is a deliberate act."""
    assert ALLOWED_SUFFIXES == EXPECTED_SUFFIXES


def test_the_accept_attribute_offers_exactly_the_accepted_types():
    """The file input's filter must not offer something the server will reject."""
    assert set(ACCEPT_ATTRIBUTE.split(",")) == EXPECTED_SUFFIXES


@pytest.mark.parametrize("suffix", sorted(EXPECTED_SUFFIXES))
def test_every_accepted_type_uploads(app, client, uploader, suffix):
    assert upload(client, f"document{suffix}").status_code == 302
    with app.app_context():
        names = [f.filename for f in db.session.scalars(db.select(UploadedFile))]
    assert names == [f"document{suffix}"]


@pytest.mark.parametrize("suffix", REFUSED_SUFFIXES)
def test_a_type_outside_the_list_is_refused(app, client, uploader, suffix):
    assert upload(client, f"document{suffix}").status_code == 302
    body = client.get("/apps/upload-file").get_data(as_text=True)
    assert "is not an accepted file type" in body
    assert "PDF, TXT, DOCX, XLSX, PPTX, PPT, DOC, HTML, HTM" in body
    with app.app_context():
        assert db.session.scalars(db.select(UploadedFile)).all() == []
    assert files_on_disk(app) == []


def test_the_extension_check_ignores_case(app, client, uploader):
    """``REPORT.PDF`` is the same file as ``report.pdf`` and must not be refused."""
    assert upload(client, "REPORT.PDF").status_code == 302
    assert only_record(app)[1] == "REPORT.PDF"


def test_a_filename_that_sanitises_to_nothing_is_refused(app, client, uploader):
    """``secure_filename`` can empty a name. Inventing one would misfile it silently."""
    assert upload(client, "..").status_code == 302
    assert "is not an accepted file type" in client.get("/apps/upload-file").get_data(as_text=True)
    with app.app_context():
        assert db.session.scalars(db.select(UploadedFile)).all() == []


def test_uploading_nothing_is_refused_with_a_reason(app, client, uploader):
    response = client.post("/apps/upload-file", data={}, content_type="multipart/form-data")
    assert response.status_code == 302
    assert "Choose a file to upload." in client.get("/apps/upload-file").get_data(as_text=True)


# ─── Where the bytes land ────────────────────────────────────────────────────

def test_the_file_is_written_to_disk_with_its_bytes(app, client, uploader):
    payload = b"%PDF-1.7 pretend bytes"
    upload(client, "report.pdf", payload)
    _, _, stored_name = only_record(app)
    with app.app_context():
        assert db.session.scalar(db.select(UploadedFile.size_bytes)) == len(payload)
    assert (folder(app) / stored_name).read_bytes() == payload


def test_the_on_disk_name_is_generated_not_the_uploaded_one(app, client, uploader):
    """A generated name is what stops two members' ``report.docx`` colliding."""
    upload(client, "report.pdf", b"one")
    _, filename, stored_name = only_record(app)
    assert filename == "report.pdf"
    assert stored_name != "report.pdf"
    assert stored_name.endswith(".pdf")


def test_two_members_may_each_hold_a_file_of_the_same_name(app, client, uploader):
    upload(client, "report.docx", b"first")

    with app.app_context():
        grant(make_member(email="second@example.com"))
    login(client, "second@example.com")
    upload(client, "report.docx", b"second")

    with app.app_context():
        stored = {r.stored_name for r in db.session.scalars(db.select(UploadedFile))}
    assert len(stored) == 2
    assert sorted((folder(app) / name).read_bytes() for name in stored) == [b"first", b"second"]


def test_a_traversing_filename_cannot_escape_the_upload_folder(app, client, uploader):
    """``../../`` in the browser-supplied name must not reach outside the folder."""
    upload(client, "../../escaped.pdf", b"payload")
    _, filename, stored_name = only_record(app)
    assert filename == "escaped.pdf"
    written = (folder(app) / stored_name).resolve()
    assert written.parent == folder(app).resolve()
    assert written.read_bytes() == b"payload"


def test_uploads_land_in_their_own_subfolder(app, client, uploader):
    """Not beside the project attachments and the Chat library."""
    upload(client, "report.pdf")
    assert files_on_disk(app) != []


def test_the_uploader_and_time_are_recorded(app, client):
    with app.app_context():
        grant(make_member())
        uploader_id = db.session.scalar(db.select(Member.id).filter_by(email="user@example.com"))
    login(client)

    upload(client, "report.pdf")
    with app.app_context():
        record = db.session.scalar(db.select(UploadedFile))
        assert record.uploaded_by_id == uploader_id
        assert record.uploaded_by_name == "User"
        assert record.created_at is not None


def test_the_page_lists_newest_first(app, client, uploader):
    upload(client, "first.pdf")
    upload(client, "second.pdf")
    # Scoped to the list, because each upload's flash message also names its file.
    body = client.get("/apps/upload-file").get_data(as_text=True)
    listing = body[body.index('<ul class="document-list">'):]
    assert listing.index("second.pdf") < listing.index("first.pdf")


def test_an_empty_folder_says_so(app, client, uploader):
    assert "The folder is empty." in client.get("/apps/upload-file").get_data(as_text=True)


# ─── Downloads ───────────────────────────────────────────────────────────────

def test_a_download_returns_the_exact_bytes(app, client, uploader):
    payload = b"binary\x00bytes\xff"
    upload(client, "report.pdf", payload)
    record_id, _, _ = only_record(app)
    response = client.get(f"/apps/upload-file/files/{record_id}/download")
    assert response.status_code == 200
    assert response.data == payload


def test_a_download_is_sent_under_the_stored_name(app, client, uploader):
    """The header carries the name the member sees in the list.

    That name went through ``secure_filename``, so spaces became underscores -
    the same treatment project attachments get, which keeps the header safe to
    parse. Pinned so a change to how the name is stored cannot silently alter
    what the browser calls the downloaded file.
    """
    upload(client, "quarterly report.pdf")
    record_id, filename, _ = only_record(app)
    assert filename == "quarterly_report.pdf"

    disposition = client.get(
        f"/apps/upload-file/files/{record_id}/download"
    ).headers["Content-Disposition"]
    assert "attachment" in disposition
    assert "quarterly_report.pdf" in disposition


def test_an_uploaded_html_page_is_never_served_as_a_rendered_page(app, client, uploader):
    """The security property that makes accepting .html safe at all.

    ``.html`` is on the accepted list, so a member can upload markup containing
    script. Served back with ``Content-Type: text/html`` it would execute in this
    app's origin against whoever opened it. It must be forced to a download.
    """
    script = b"<script>alert(document.cookie)</script>"
    upload(client, "attack.html", script)
    record_id, _, _ = only_record(app)

    response = client.get(f"/apps/upload-file/files/{record_id}/download")
    assert response.status_code == 200
    # The bytes are returned intact - it is a download, not a rejection.
    assert response.data == script
    # Three things together mean the browser cannot execute it: attachment
    # disposition, nosniff, and a content type no renderer will act on.
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Type"] == "application/octet-stream"


def test_an_uploaded_page_is_declared_as_markup_either_way(app, client, uploader):
    """Both accepted markup types get the same treatment."""
    for name in ("one.html", "two.htm"):
        upload(client, name, b"<b>hi</b>")
    with app.app_context():
        ids = list(db.session.scalars(db.select(UploadedFile.id).order_by(UploadedFile.id)))
    for record_id in ids:
        response = client.get(f"/apps/upload-file/files/{record_id}/download")
        assert response.headers["Content-Type"] == "application/octet-stream"


def test_downloading_something_that_is_not_there_returns_to_the_page(app, client, uploader):
    response = client.get("/apps/upload-file/files/999/download")
    assert response.status_code == 302
    assert "That file is no longer in the folder." in client.get("/apps/upload-file").get_data(as_text=True)


# ─── Deleting ────────────────────────────────────────────────────────────────

def test_the_uploader_can_delete_their_own_file(app, client, uploader):
    upload(client, "report.pdf")
    record_id, _, stored_name = only_record(app)

    assert client.post(f"/apps/upload-file/files/{record_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.scalars(db.select(UploadedFile)).all() == []
    assert not (folder(app) / stored_name).exists()
    assert "Deleted report.pdf." in client.get("/apps/upload-file").get_data(as_text=True)


def test_another_member_cannot_delete_someone_elses_file(app, client, uploader):
    """A shared folder where anyone may delete anything loses work on a misclick."""
    upload(client, "report.pdf")
    record_id, _, stored_name = only_record(app)

    with app.app_context():
        grant(make_member(email="other@example.com"))
    login(client, "other@example.com")

    response = client.post(f"/apps/upload-file/files/{record_id}/delete")
    assert response.status_code == 302
    assert "You can only delete a file that you uploaded." in client.get("/apps/upload-file").get_data(as_text=True)
    with app.app_context():
        assert len(db.session.scalars(db.select(UploadedFile)).all()) == 1
    assert (folder(app) / stored_name).exists()


def test_the_delete_button_is_not_offered_to_a_non_owner(app, client, uploader):
    upload(client, "report.pdf")
    record_id, _, _ = only_record(app)

    with app.app_context():
        grant(make_member(email="other@example.com"))
    login(client, "other@example.com")

    body = client.get("/apps/upload-file").get_data(as_text=True)
    assert "Download" in body
    assert "Delete" not in body
    assert f"/apps/upload-file/files/{record_id}/delete" not in body


def test_an_administrator_can_delete_anyones_file(app, client, uploader):
    upload(client, "report.pdf")
    record_id, _, stored_name = only_record(app)

    with app.app_context():
        grant(make_member(email="admin@example.com", is_admin=True))
    login(client, "admin@example.com")

    body = client.get("/apps/upload-file").get_data(as_text=True)
    assert f"/apps/upload-file/files/{record_id}/delete" in body
    assert client.post(f"/apps/upload-file/files/{record_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.scalars(db.select(UploadedFile)).all() == []
    assert not (folder(app) / stored_name).exists()


def test_deleting_something_that_is_not_there_returns_to_the_page(app, client, uploader):
    response = client.post("/apps/upload-file/files/999/delete")
    assert response.status_code == 302
    assert "That file is no longer in the folder." in client.get("/apps/upload-file").get_data(as_text=True)


# ─── Access control ──────────────────────────────────────────────────────────

def test_the_page_is_refused_without_the_grant(app, client):
    with app.app_context():
        make_member()
    login(client)
    response = client.get("/apps/upload-file")
    assert response.status_code == 403
    assert "You do not have access to Upload File." in response.get_data(as_text=True)


def test_uploading_is_refused_without_the_grant(app, client):
    """The gate is on the route, not on the link - a crafted POST must fail too."""
    with app.app_context():
        make_member()
    login(client)

    assert upload(client, "report.pdf").status_code == 403
    with app.app_context():
        assert db.session.scalars(db.select(UploadedFile)).all() == []
    assert files_on_disk(app) == []


def test_downloading_is_refused_without_the_grant(app, client):
    with app.app_context():
        grant(make_member())
    login(client)
    upload(client, "report.pdf", b"the secret payload")
    record_id, _, _ = only_record(app)

    with app.app_context():
        make_member(email="other@example.com")
    login(client, "other@example.com")

    response = client.get(f"/apps/upload-file/files/{record_id}/download")
    assert response.status_code == 403
    assert "You do not have access to Upload File." in response.get_data(as_text=True)
    # The point of the assertion: the bytes are not disclosed, so this is not a
    # route that leaks file contents to an ungranted member.
    assert b"the secret payload" not in response.data


def test_deleting_is_refused_without_the_grant(app, client):
    with app.app_context():
        grant(make_member())
    login(client)
    upload(client, "report.pdf")
    record_id, _, stored_name = only_record(app)

    with app.app_context():
        make_member(email="other@example.com")
    login(client, "other@example.com")

    assert client.post(f"/apps/upload-file/files/{record_id}/delete").status_code == 403
    with app.app_context():
        assert len(db.session.scalars(db.select(UploadedFile)).all()) == 1
    assert (folder(app) / stored_name).exists()


def test_a_signed_out_visitor_is_sent_to_the_login_page(app, client):
    response = client.get("/apps/upload-file")
    assert response.status_code == 302
    assert "/apps/login" in response.headers["Location"]


# ─── Header link and admin UI ────────────────────────────────────────────────

def test_the_header_links_the_service_for_a_granted_member(app, client, uploader):
    body = client.get("/apps/mizalla/").get_data(as_text=True)
    assert 'href="/apps/upload-file"' in body
    assert ">Upload File<" in body


def test_the_header_omits_the_service_without_the_grant(app, client):
    with app.app_context():
        make_member()
    login(client)
    assert "/apps/upload-file" not in client.get("/apps/mizalla/").get_data(as_text=True)


def test_the_service_needs_no_credentials_so_it_is_never_greyed_out(app, client, uploader):
    """It calls nothing upstream, so it must not inherit the 'unconfigured' state.

    The ``clean_secrets`` fixture clears every Azure and SMS key, so this proves
    the link is a real anchor rather than the disabled span the other services
    fall back to.
    """
    body = client.get("/apps/mizalla/").get_data(as_text=True)
    link = next(chunk for chunk in body.split("<a ") if 'href="/apps/upload-file"' in chunk)
    assert "Upload File" in link
    assert "app-switcher-link-disabled" not in link


def test_the_admin_member_form_offers_the_grant(app, client):
    """The grant is administered from Members, like every other service."""
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
        admin_id = member_id("admin@example.com")
    login(client, "admin@example.com")

    body = client.get(f"/apps/admin/members/{admin_id}/edit").get_data(as_text=True)
    assert f'value="{FEATURE_UPLOAD}"' in body
    assert "Upload File" in body
    assert "Store PDF, Word, Excel, PowerPoint, text and HTML files" in body


def test_an_admin_grant_reaches_the_service(app, client):
    """End to end through the real admin form, not a direct call to the model."""
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
        make_member(email="member@example.com")
        target_id = member_id("member@example.com")
    login(client, "admin@example.com")

    # Every other service box is submitted unticked, so this also proves that
    # saving one grant does not disturb the rest.
    # The member form posts one repeated ``feature_ids`` field per ticked box.
    response = client.post(
        f"/apps/admin/members/{target_id}/edit",
        data={
            "name": "Member",
            "email": "member@example.com",
            "feature_ids": [FEATURE_UPLOAD],
        },
    )
    assert response.status_code == 302

    with app.app_context():
        granted = {
            row.feature_key
            for row in db.session.scalars(
                db.select(AppFeatureAccess).filter_by(member_id=target_id)
            )
        }
    assert granted == {FEATURE_UPLOAD}

    login(client, "member@example.com")
    assert client.get("/apps/upload-file").status_code == 200


def test_an_admin_can_revoke_the_grant_from_the_member_form(app, client):
    with app.app_context():
        make_member(email="admin@example.com", is_admin=True)
        grant(make_member(email="member@example.com"))
        target_id = member_id("member@example.com")
    login(client, "admin@example.com")

    # Submitted with the box unticked.
    response = client.post(
        f"/apps/admin/members/{target_id}/edit",
        data={"name": "Member", "email": "member@example.com"},
    )
    assert response.status_code == 302

    login(client, "member@example.com")
    assert client.get("/apps/upload-file").status_code == 403


# ─── Form contract ───────────────────────────────────────────────────────────

def test_the_form_carries_a_csrf_token(app, client, uploader):
    """Without it Flask-WTF rejects the POST before the view ever runs."""
    assert 'name="csrf_token"' in client.get("/apps/upload-file").get_data(as_text=True)


def test_the_form_posts_the_file_field_as_multipart(app, client, uploader):
    body = client.get("/apps/upload-file").get_data(as_text=True)
    assert 'enctype="multipart/form-data"' in body
    assert 'name="file"' in body


def test_a_post_without_a_csrf_token_is_refused(tmp_path):
    """Proves the token in the page is load-bearing, not decoration.

    The shared ``app`` fixture disables CSRF, so this builds its own CSRF-enabled
    app. Without that, a regression that dropped the token from the template
    would still pass every other test here.
    """
    csrf_app = create_app({
        "TESTING": True,
        "SECRET_KEY": "csrf-key",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'csrf.db'}",
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "WTF_CSRF_ENABLED": True,
    })
    with csrf_app.app_context():
        db.create_all()
        grant(make_member())
        csrf_client = csrf_app.test_client()

        # Sign in first. A CSRF-enabled app protects its own login form too, so
        # the session has to be established before the page can be reached.
        csrf_client.post(
            "/apps/login",
            data={
                "email": "user@example.com",
                "password": PASSWORD,
                "csrf_token": _csrf_token(csrf_client, "/apps/login"),
            },
        )

        response = csrf_client.post(
            "/apps/upload-file",
            data={"file": (io.BytesIO(b"x"), "report.pdf")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 400
        assert db.session.scalars(db.select(UploadedFile)).all() == []

        # With the token the page actually carries, the same request succeeds -
        # so the 400 above is about the token and nothing else.
        ok = csrf_client.post(
            "/apps/upload-file",
            data={
                "file": (io.BytesIO(b"x"), "report.pdf"),
                "csrf_token": _csrf_token(csrf_client),
            },
            content_type="multipart/form-data",
        )
        assert ok.status_code == 302
        assert len(db.session.scalars(db.select(UploadedFile)).all()) == 1
    with csrf_app.app_context():
        db.session.remove()
        db.drop_all()


def _csrf_token(client, path="/apps/upload-file") -> str:
    """The token the rendered form at ``path`` carries, read back out of it."""
    body = client.get(path).get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


# ─── Base URL setting and link generation ───────────────────────────────────

def test_upload_file_base_url_generates_success_link(app, client, uploader):
    """When upload_file_base_url is set, uploading a file displays a link matching base_url/<filename>."""
    from mizalla.settings import set_setting
    with app.app_context():
        set_setting("upload_file_base_url", "https://files.example.com/storage/")

    assert upload(client, "quarterly_report.pdf").status_code == 302
    body = client.get("/apps/upload-file").get_data(as_text=True)
    expected_link = "https://files.example.com/storage/quarterly_report.pdf"
    assert "Uploaded quarterly_report.pdf." in body
    assert f'href="{expected_link}"' in body
    assert expected_link in body


def test_upload_file_without_base_url_has_plain_success_message(app, client, uploader):
    """When upload_file_base_url is blank, uploading a file shows a plain success message."""
    from mizalla.settings import set_setting
    with app.app_context():
        set_setting("upload_file_base_url", "")

    assert upload(client, "simple.docx").status_code == 302
    body = client.get("/apps/upload-file").get_data(as_text=True)
    assert "Uploaded simple.docx." in body
    assert "https://" not in body and "http://" not in body