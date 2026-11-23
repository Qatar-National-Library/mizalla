from datetime import datetime, timezone
import pytest
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    FEATURE_REMOTE_EXEC,
    CommandItem,
    CommandGroup,
    CommandGroupItem,
    Member,
    Server,
    COMMAND_TYPE_REMOTE,
)


@pytest.fixture
def auth_client(client, app):
    """Authenticate an admin user with Flask-Login session."""
    with app.app_context():
        user = Member.query.filter_by(email="admin_search_ui@test.com").first()
        if not user:
            user = Member(
                name="Admin Search UI",
                email="admin_search_ui@test.com",
                is_admin=True,
                can_add_command_items=True,
                can_add_command_groups=True,
            )
            user.set_password("Secret123!")
            db.session.add(user)
            db.session.flush()
            sync_member_features(user, [FEATURE_REMOTE_EXEC])
            db.session.commit()
            user_id = user.id
        else:
            user_id = user.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


def test_command_item_last_modified_timestamp(app):
    """Test last_modified_timestamp reflects created_on and updated_on in UTC."""
    with app.app_context():
        user = Member.query.filter_by(email="admin_search_ui@test.com").first()
        if not user:
            user = Member(
                name="Recency User",
                email="admin_search_ui@test.com",
                is_admin=True,
            )
            user.set_password("Pass123!")
            db.session.add(user)
            db.session.commit()

        now = datetime.now(timezone.utc)
        item = CommandItem(
            name="Test Recency Item",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="echo recency",
            created_by_id=user.id,
            created_on=now,
        )
        db.session.add(item)
        db.session.commit()

        assert item.last_modified_timestamp == pytest.approx(now.timestamp(), abs=2.0)

        # Update updated_on
        later = datetime.now(timezone.utc)
        item.updated_on = later
        db.session.commit()

        assert item.last_modified_timestamp == pytest.approx(later.timestamp(), abs=2.0)


def test_command_group_builder_renders_search_and_recent_controls(auth_client, app):
    """Verify group builder form renders search input, OS dropdown, and recent items container."""
    with app.app_context():
        server = Server.query.filter_by(ip="192.168.1.150").first()
        if not server:
            server = Server(
                name="Ubuntu Test Host",
                ip="192.168.1.150",
                operating_system="Ubuntu 22.04",
                os_type="ubuntu",
                status="available",
            )
            db.session.add(server)
            db.session.commit()

    resp = auth_client.get("/apps/remote-exec/groups/new")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # 1. Search controls
    assert 'id="avail-items-search-input"' in html
    assert 'id="avail-items-search-clear"' in html
    assert 'id="avail-items-search-prompt"' in html
    assert 'Type more than 3 characters to search' in html

    # 2. OS Filter
    assert 'id="avail-items-os-filter-select"' in html
    assert 'Any Operating System' in html
    assert 'Ubuntu / Debian' in html
    assert 'Windows' in html
    assert 'RHEL / CentOS / Rocky' in html

    # 3. Recent items section
    assert 'id="avail-items-recent-section"' in html
    assert 'Recently Added or Edited' in html
    assert 'Last 10m' in html
    assert 'id="avail-items-recent-list"' in html


def test_save_item_updates_updated_on_and_returns_last_modified(auth_client, app):
    """Test AJAX item save returns last_modified timestamp and sets updated_on."""
    with app.app_context():
        user = Member.query.filter_by(email="admin_search_ui@test.com").first()
        item = CommandItem(
            name="Item Before Edit",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="uptime",
            created_by_id=user.id,
        )
        db.session.add(item)
        db.session.commit()
        item_id = item.id

    resp = auth_client.post(
        "/apps/remote-exec/items/save",
        data={
            "item_id": str(item_id),
            "name": "Item After Edit",
            "command_type": "remote",
            "command_content": "uptime -p",
            "target_os": "ubuntu",
            "ajax": "1",
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "last_modified" in data["item"]
    assert data["item"]["name"] == "Item After Edit"

    with app.app_context():
        reloaded = CommandItem.query.get(item_id)
        assert reloaded.updated_on is not None
        assert reloaded.last_modified_timestamp == data["item"]["last_modified"]
