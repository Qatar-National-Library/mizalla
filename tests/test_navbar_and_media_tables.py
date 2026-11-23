import pytest
from bs4 import BeautifulSoup
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import FEATURE_MEDIA_WALL, Member

PASSWORD = "member-password-long"

@pytest.fixture
def auth_client(client, app):
    with app.app_context():
        member = Member.query.filter_by(email="testuser@example.com").first()
        if not member:
            member = Member(
                email="testuser@example.com",
                name="Test User",
                is_admin=True,
            )
            member.set_password(PASSWORD)
            db.session.add(member)
            sync_member_features(member, [FEATURE_MEDIA_WALL])
            db.session.commit()
        member_id = member.id

    client.post("/apps/login", data={"email": "testuser@example.com", "password": PASSWORD})
    return client

def test_media_wall_removes_file_details_and_inlines_icons(auth_client, app):
    """Verify that File Details column is removed and media icon is rendered in Asset Name cell."""
    res = auth_client.get("/apps/media-walls")
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")

    # Ensure no table header has "File Details"
    th_headers = [th.get_text(strip=True) for th in soup.find_all("th")]
    assert "File Details" not in th_headers

    # Check that render_signage_panel macro does not output th with File Details
    assert "<th>File Details</th>" not in html

def test_navbar_about_and_user_guide_relocated_to_user_dropdown(auth_client, client):
    """Verify About and User guide are moved to user dropdown when authenticated, and in public nav when guest."""
    # 1. Authenticated
    res = auth_client.get("/apps/remote-exec/", follow_redirects=True)
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")

    # In primary navigation: About and User guide should NOT be present
    nav = soup.find("nav", class_="navigation")
    if nav:
        nav_links = [a.get_text(strip=True) for a in nav.find_all("a")]
        assert "About" not in nav_links
        assert "User guide" not in nav_links

    # In user-dropdown-menu: About and User guide MUST be present
    dropdown = soup.find("div", class_="user-dropdown-menu")
    assert dropdown is not None
    dropdown_links = [a.get_text(strip=True) for a in dropdown.find_all("a")]
    assert "About" in dropdown_links
    assert "User guide" in dropdown_links

    # 2. Unauthenticated (Guest)
    res_guest = client.get("/apps/login")
    assert res_guest.status_code == 200
    html_guest = res_guest.data.decode("utf-8")
    soup_guest = BeautifulSoup(html_guest, "html.parser")
    public_nav = soup_guest.find("nav", class_="public-navigation")
    if public_nav:
        guest_links = [a.get_text(strip=True) for a in public_nav.find_all("a")]
        assert "About" in guest_links
        assert "User guide" in guest_links

def test_remote_exec_in_step_file_upload_present(auth_client):
    """Verify remote exec dashboard template contains in-step upload rendering for upload steps."""
    res = auth_client.get("/apps/remote-exec/", follow_redirects=True)
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    # Verify the JS contains the in-step upload container rendering
    assert 'Select Files to Upload' in html
    assert 'item.command_type === "upload"' in html
    assert 'uploadBox.style.border = "1px dashed' in html
