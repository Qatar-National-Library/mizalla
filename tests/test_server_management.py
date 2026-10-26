"""Tests for Environments & Server Management UI and Connection Lifecycle."""

from unittest.mock import patch
import pytest

from mizalla.extensions import db
from mizalla.models import Member, Server, ServerCredential


def login_admin(client, app):
    with app.app_context():
        admin = Member.query.filter_by(email="admin@example.com").first()
        if not admin:
            admin = Member(name="Admin User", email="admin@example.com", is_admin=True)
            admin.set_password("adminpass123")
            db.session.add(admin)
            db.session.commit()
    client.post("/apps/login", data={"email": "admin@example.com", "password": "adminpass123"})


def test_servers_page_renamed_and_form_fields(client, app):
    """Test that the page is renamed to 'Environments & Server Management'

    and the Add Server form has username/password and lacks OS/Technology.
    """
    login_admin(client, app)
    resp = client.get("/apps/admin/servers")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Check renamed headers, tabs, and eyebrows
    assert "Environments &amp; Server Management" in html or "Environments & Server Management" in html
    assert "Server Inventory" not in html

    # Check fields in Add Server form
    assert 'name="username"' in html
    assert 'name="password"' in html
    assert "Operating system and technology packages will be detected automatically" in html

    # Reconnect modal dialog
    assert 'id="reconnect-dialog"' in html
    assert 'id="reconnect-modal-form"' in html
    assert 'id="modal-username"' in html
    assert 'id="modal-password"' in html


def test_add_server_with_credentials_and_auto_detection_placeholder(client, app):
    """Adding a server stores credentials and defaults OS to Detecting..."""
    login_admin(client, app)

    with patch("mizalla.admin._start_connection_cycle") as mock_cycle:
        resp = client.post(
            "/apps/admin/servers",
            data={
                "name": "Production App Node",
                "ip": "10.0.1.50",
                "username": "deploy_user",
                "password": "SecretPassword123!",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        with app.app_context():
            server = Server.query.filter_by(name="Production App Node").first()
            assert server is not None
            assert server.ip == "10.0.1.50"
            assert server.operating_system == "Detecting..."
            assert server.agent_status == "connecting"

            # Check credentials stored and encrypted
            cred = server.default_credential
            assert cred is not None
            assert cred.username == "deploy_user"
            assert cred.password == "SecretPassword123!"

        mock_cycle.assert_called_once()


def test_reconnect_endpoint_and_status(client, app):
    """Test POST to /apps/admin/servers/<id>/reconnect and connection-status endpoint."""
    login_admin(client, app)

    with app.app_context():
        server = Server(
            name="Test Worker",
            ip="10.0.2.100",
            operating_system="Detecting...",
            agent_status="failed",
            connection_attempts=5,
        )
        db.session.add(server)
        db.session.commit()

        cred = ServerCredential(
            server_id=server.id,
            username="initial_user",
            is_default=True,
        )
        cred.password = "old_password"
        db.session.add(cred)
        db.session.commit()
        server_id = server.id

    with patch("mizalla.admin._start_connection_cycle") as mock_cycle:
        resp = client.post(
            f"/apps/admin/servers/{server_id}/reconnect",
            data={
                "username": "updated_user",
                "password": "new_secure_password",
            },
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == 200
        json_data = resp.get_json()
        assert json_data["agent_status"] == "connecting"
        assert json_data["connection_attempts"] == 1
        assert "1 of 5 retries" in json_data["connection_message"]

        with app.app_context():
            s = db.session.get(Server, server_id)
            assert s.agent_status == "connecting"
            assert s.connection_attempts == 1
            cred = s.default_credential
            assert cred.username == "updated_user"
            assert cred.password == "new_secure_password"

        mock_cycle.assert_called_once()

    # Test status endpoint
    status_resp = client.get(f"/apps/admin/servers/{server_id}/connection-status")
    assert status_resp.status_code == 200
    st = status_resp.get_json()
    assert st["agent_status"] == "connecting"
    assert st["connection_attempts"] == 1
    assert st["max_connection_attempts"] == 5
    assert "1 of 5 retries" in st["connection_message"]
    assert st["is_online"] is False


def test_connection_cycle_success_populates_os_and_packages(app):
    """_run_connection_cycle automatically fills OS and technology packages on successful connection."""
    from mizalla.admin import _run_connection_cycle

    with app.app_context():
        server = Server(
            name="Live Node",
            ip="192.168.1.20",
            operating_system="Detecting...",
            agent_status="connecting",
            connection_attempts=1,
        )
        db.session.add(server)
        db.session.commit()

        cred = ServerCredential(
            server_id=server.id,
            username="root",
            is_default=True,
        )
        cred.password = "rootpass"
        db.session.add(cred)
        db.session.commit()
        server_id = server.id

    # Mock asyncssh_engine.test_ssh_connection to succeed on attempt 2
    mock_telemetry = {
        "os_version": "Ubuntu 24.04 LTS (Noble Numbat)",
        "packages_summary": "Nginx, Python 3.12, Docker 26.0",
    }

    call_count = 0

    def mock_test_ssh(ip, port, username, password):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return (False, "Connection refused", {})
        return (True, "Connected successfully", mock_telemetry)

    with patch("mizalla.asyncssh_engine.test_ssh_connection", side_effect=mock_test_ssh), \
         patch("time.sleep", return_value=None):
        _run_connection_cycle(app, server_id, "root", "rootpass", max_retries=5)

    with app.app_context():
        s = db.session.get(Server, server_id)
        assert s.agent_status == "online"
        assert s.operating_system == "Ubuntu 24.04 LTS (Noble Numbat)"
        assert s.technology_packages == "Nginx, Python 3.12, Docker 26.0"
        assert s.connection_attempts == 0
        assert "Connected successfully" in s.connection_message


def test_connection_cycle_fails_after_5_attempts(app):
    """_run_connection_cycle strictly stops after 5 retries."""
    from mizalla.admin import _run_connection_cycle

    with app.app_context():
        server = Server(
            name="Unreachable Node",
            ip="192.168.99.99",
            operating_system="Detecting...",
            agent_status="connecting",
            connection_attempts=1,
        )
        db.session.add(server)
        db.session.commit()

        cred = ServerCredential(
            server_id=server.id,
            username="user",
            is_default=True,
        )
        cred.password = "pass"
        db.session.add(cred)
        db.session.commit()
        server_id = server.id

    call_count = 0

    def mock_test_ssh(ip, port, username, password):
        nonlocal call_count
        call_count += 1
        return (False, "Host unreachable", {})

    with patch("mizalla.asyncssh_engine.test_ssh_connection", side_effect=mock_test_ssh), \
         patch("time.sleep", return_value=None):
        _run_connection_cycle(app, server_id, "user", "pass", max_retries=5)

    assert call_count == 5

    with app.app_context():
        s = db.session.get(Server, server_id)
        assert s.agent_status == "failed"
        assert s.connection_attempts == 5
        assert "Connection failed after 5 retries" in s.connection_message


def test_member_edit_layout_and_side_panels(client, app):
    """Test that member edit page has 70% main form, Registered Devices 30% panel, and Registered Servers panel."""
    from mizalla.models import GowaDevice
    login_admin(client, app)

    with app.app_context():
        member = Member(name="Target User", email="target@example.com", is_admin=False)
        member.set_password("TargetPass123!")
        db.session.add(member)

        device = GowaDevice(name="TestPhone", device_id="device-123", status="connected")
        db.session.add(device)

        server = Server(
            name="Alpha Node",
            ip="192.168.1.10",
            operating_system="Debian 12",
            agent_status="online",
        )
        db.session.add(server)
        db.session.commit()
        member_id = member.id
        server_id = server.id

    resp = client.get(f"/apps/admin/members/{member_id}/edit")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # 70/30 layout classes
    assert "member-edit-layout" in html
    assert "main-form-col" in html
    assert "side-panels-col" in html

    # Registered Devices panel exists outside fieldset
    assert 'id="registered-devices-panel"' in html
    assert "Registered Devices" in html
    assert "TestPhone" in html
    assert 'name="device_ids"' in html
    assert 'id="whatsapp-device-fieldset"' not in html

    # Registered Servers panel exists with server checkbox, status, and delete button only
    assert 'id="registered-servers-panel"' in html
    assert "Registered Servers" in html
    assert "Alpha Node" in html
    assert "192.168.1.10" in html
    assert "ONLINE" in html
    assert 'name="server_ids"' in html
    assert f'delete-btn-{server_id}' in html
    assert "Delete" in html
    # RECONNECT and Edit & Token removed from member edit panel
    assert "reconnect-btn" not in html
    assert "Edit &amp; Token" not in html and "Edit & Token" not in html

    # Service access checkboxes controlling the panels
    assert 'id="cb-bulk-whatsapp"' in html
    assert 'id="cb-remote-exec"' in html


def test_member_server_assignment_lifecycle(client, app):
    """Test assigning server access via checkbox, verifying get_accessible_servers_for_user, and revoking."""
    from mizalla.admin import get_accessible_servers_for_user
    from mizalla.models import ServerAccess

    login_admin(client, app)

    with app.app_context():
        member = Member(name="Remote Worker", email="worker@example.com", is_admin=False)
        member.set_password("WorkerPass123!")
        db.session.add(member)

        s1 = Server(name="Web Server", ip="10.0.0.1", operating_system="Ubuntu 22.04")
        s2 = Server(name="DB Server", ip="10.0.0.2", operating_system="Debian 12")
        db.session.add_all([s1, s2])
        db.session.commit()
        member_id = member.id
        s1_id = s1.id
        s2_id = s2.id

    # Initially member has no accessible servers
    with app.app_context():
        m = db.session.get(Member, member_id)
        assert len(get_accessible_servers_for_user(m)) == 0

    # Admin edits member, assigns s1
    resp = client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={
            "name": "Remote Worker",
            "email": "worker@example.com",
            "feature_ids": ["remote_exec"],
            "server_ids": [s1_id],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        m = db.session.get(Member, member_id)
        grants = ServerAccess.query.filter_by(member_id=member_id).all()
        assert len(grants) == 1
        assert grants[0].server_id == s1_id
        acc = get_accessible_servers_for_user(m)
        assert len(acc) == 1
        assert acc[0].id == s1_id

    # View edit page: s1 is checked, s2 is not checked
    resp = client.get(f"/apps/admin/members/{member_id}/edit")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert f'value="{s1_id}" checked' in html
    assert f'value="{s2_id}" checked' not in html

    # Revoke access to s1 and assign s2
    resp = client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={
            "name": "Remote Worker",
            "email": "worker@example.com",
            "feature_ids": ["remote_exec"],
            "server_ids": [s2_id],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        m = db.session.get(Member, member_id)
        grants = ServerAccess.query.filter_by(member_id=member_id).all()
        assert len(grants) == 1
        assert grants[0].server_id == s2_id
        acc = get_accessible_servers_for_user(m)
        assert len(acc) == 1
        assert acc[0].id == s2_id

    # Revoke remote_exec feature completely -> revokes all server access
    resp = client.post(
        f"/apps/admin/members/{member_id}/edit",
        data={
            "name": "Remote Worker",
            "email": "worker@example.com",
            "features_submitted": "1",
            "feature_ids": [],
            "server_ids": [s2_id],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        m = db.session.get(Member, member_id)
        grants = ServerAccess.query.filter_by(member_id=member_id).all()
        assert len(grants) == 0
        acc = get_accessible_servers_for_user(m)
        assert len(acc) == 0


def test_user_menu_feedback_moderation_under_give_feedback(client, app):
    """Test that in the user menu dropdown, Feedback Moderation appears below Give Feedback & Suggestions."""
    login_admin(client, app)
    resp = client.get("/apps/admin/members")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    pos_give_feedback = html.find("Give Feedback & Suggestions")
    pos_moderation = html.find("Feedback Moderation")

    assert pos_give_feedback != -1
    assert pos_moderation != -1
    assert pos_moderation > pos_give_feedback, "Feedback Moderation must appear after Give Feedback & Suggestions"


def test_connection_cycle_populates_full_telemetry(app):
    """_run_connection_cycle persists all hardware and runtime telemetry onto Server record."""
    import json
    from mizalla.admin import _run_connection_cycle

    with app.app_context():
        server = Server(
            name="Telemetry Test Server",
            ip="192.168.1.55",
            operating_system="Detecting...",
            agent_status="connecting",
            connection_attempts=1,
        )
        db.session.add(server)
        db.session.commit()

        cred = ServerCredential(
            server_id=server.id,
            username="ubuntu",
            is_default=True,
        )
        cred.password = "secretpass"
        db.session.add(cred)
        db.session.commit()
        server_id = server.id

    full_telemetry = {
        "os_type": "linux",
        "operating_system": "Ubuntu 22.04 LTS (Jammy Jellyfish)",
        "os_version": "Ubuntu 22.04 LTS (Jammy Jellyfish)",
        "packages_summary": "Python 3.10, Docker 24.0",
        "ram_total": "16.0 GB",
        "ram_used": "4.2 GB",
        "ram_percent": 26.3,
        "disk_total": "250.0 GB",
        "disk_used": "45.0 GB",
        "disk_percent": 18.0,
        "core_tags": (
            "external_storage: Mounted at /mnt/external_backup\r\n"
            "containerizers: Docker 24.0.5 (Running) · Compose: /opt/web-stack\r\n"
            "runtimes: Python 3.11.4 · PHP 8.2.10\r\n"
            "web_services: Apache (2.4.52) [Running on port 80, 443 in /var/www/html]\r\n"
            "drupal: Drupal 10.1.2 [Running on port 80 in /var/www/drupal]"
        ),
    }

    with patch("mizalla.asyncssh_engine.test_ssh_connection", return_value=(True, "Connected successfully", full_telemetry)), \
         patch("time.sleep", return_value=None):
        _run_connection_cycle(app, server_id, "ubuntu", "secretpass", max_retries=1)

    with app.app_context():
        s = db.session.get(Server, server_id)
        assert s.agent_status == "online"
        assert s.operating_system == "Ubuntu 22.04 LTS (Jammy Jellyfish)"
        assert s.ram_total == "16.0 GB"
        assert s.ram_used == "4.2 GB"
        assert s.ram_percent == 26.3
        assert s.disk_total == "250.0 GB"
        assert s.disk_used == "45.0 GB"
        assert s.disk_percent == 18.0
        assert s.has_tag("drupal") is True
        assert s.has_tag("docker") is True
        assert s.has_tag("php") is True
        assert s.has_tag("nonexistent_tag") is False
        assert len(s.get_tag_lines()) == 5
        assert s.has_telemetry is True


def test_server_connection_status_returns_telemetry_json(client, app):
    """GET /apps/admin/servers/<id>/connection-status returns all telemetry metrics."""
    login_admin(client, app)

    with app.app_context():
        server = Server(
            name="JSON Telemetry Node",
            ip="192.168.1.77",
            operating_system="Ubuntu 22.04 LTS",
            agent_status="online",
            ram_total="32.0 GB",
            ram_used="8.0 GB",
            ram_percent=25.0,
            disk_total="500.0 GB",
            disk_used="100.0 GB",
            disk_percent=20.0,
            core_tags=(
                "containerizers: Docker 25.0.0 (Running) · Compose: /srv/compose\r\n"
                "drupal: Drupal 10 [Running on port 80 in /var/www/html]"
            ),
        )
        db.session.add(server)
        db.session.commit()
        server_id = server.id

    resp = client.get(f"/apps/admin/servers/{server_id}/connection-status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["is_online"] is True
    assert data["ram_total"] == "32.0 GB"
    assert data["ram_used"] == "8.0 GB"
    assert data["ram_percent"] == 25.0
    assert data["disk_total"] == "500.0 GB"
    assert data["disk_used"] == "100.0 GB"
    assert data["disk_percent"] == 20.0
    assert "drupal" in data["core_tags"].lower()
    assert len(data["tag_lines"]) == 2
    assert "containerizers: Docker 25.0.0" in data["tag_lines"][0]


def test_server_cards_render_green_horizontal_bar_graphs_and_telemetry(client, app):
    """Server Card Display renders horizontal bar graphs with green in-use fill and tags."""
    login_admin(client, app)

    with app.app_context():
        server = Server(
            name="Display Telemetry Server",
            ip="192.168.1.88",
            operating_system="Ubuntu 22.04 LTS",
            agent_status="online",
            ram_total="16.0 GB",
            ram_used="4.2 GB",
            ram_percent=26.3,
            disk_total="250.0 GB",
            disk_used="45.0 GB",
            disk_percent=18.0,
            core_tags="drupal: Drupal 10.1 [Running on port 80 in /var/www/drupal]\r\ncontainerizers: Docker 24.0.5",
        )
        db.session.add(server)
        db.session.commit()
        server_id = server.id

    resp = client.get("/apps/admin/servers")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Horizontal bar graph checks
    # RAM
    assert f'id="ram-bar-fill-{server_id}"' in html
    assert "width:26.3%" in html
    assert f'id="ram-text-{server_id}"' in html
    assert "4.2 GB / 16.0 GB" in html

    # Storage
    assert f'id="disk-bar-fill-{server_id}"' in html
    assert "width:18.0%" in html
    assert f'id="disk-text-{server_id}"' in html
    assert "45.0 GB / 250.0 GB" in html

    # Tag capsules
    assert "Drupal 10.1" in html
    assert "Docker 24.0.5" in html
    html = resp.get_data(as_text=True)

    # Horizontal bar graph checks
    # RAM
    assert f'id="ram-bar-fill-{server_id}"' in html
    assert "background:#2e7d32" in html
    assert "width:26.3%" in html
    assert "4.2 GB / 16.0 GB" in html

    # Permanent memory / Storage
    assert f'id="disk-bar-fill-{server_id}"' in html
    assert "width:18.0%" in html
    assert "45.0 GB / 250.0 GB" in html

    # Core tag capsules
    assert "Drupal 10.1" in html
    assert "Docker 24.0.5" in html


def test_server_edit_triggers_reconnection_cycle(client, app):
    """Updating a server via server_edit POST initiates connection cycle."""
    login_admin(client, app)

    with app.app_context():
        server = Server(
            name="Old Server Name",
            ip="192.168.1.120",
            operating_system="Detecting...",
            agent_status="offline",
        )
        db.session.add(server)
        db.session.commit()

        cred = ServerCredential(
            server_id=server.id,
            username="ubuntu",
            is_default=True,
        )
        cred.password = "mypassword"
        db.session.add(cred)
        db.session.commit()
        server_id = server.id

    with patch("mizalla.admin._start_connection_cycle") as mock_cycle:
        resp = client.post(
            f"/apps/admin/servers/{server_id}/edit",
            data={
                "name": "Updated Server Name",
                "ip": "192.168.1.121",
                "username": "ubuntu_updated",
                "password": "newpassword123",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        mock_cycle.assert_called_once()


def test_os_scripts_exist_and_contain_parallel_sections():
    """Verify that ubuntu.txt, rhel.txt, and windows.txt exist in /automate/scripts and contain parallel sections."""
    from pathlib import Path
    from mizalla.asyncssh_engine import load_os_script, SCRIPTS_DIR

    assert SCRIPTS_DIR.exists()
    assert (SCRIPTS_DIR / "ubuntu.txt").is_file()
    assert (SCRIPTS_DIR / "rhel.txt").is_file()
    assert (SCRIPTS_DIR / "windows.txt").is_file()

    # Ubuntu script parsing
    ubuntu_sections = load_os_script("ubuntu")
    assert ubuntu_sections is not None
    assert "os_version" in ubuntu_sections
    assert "ram" in ubuntu_sections
    assert "disk" in ubuntu_sections
    assert "external_storage" in ubuntu_sections
    assert "docker" in ubuntu_sections
    assert "kubernetes" in ubuntu_sections
    assert "python" in ubuntu_sections
    assert "php" in ubuntu_sections
    assert "nodejs" in ubuntu_sections
    assert "uvicorn" in ubuntu_sections
    assert "apache" in ubuntu_sections
    assert "nginx" in ubuntu_sections
    assert "drupal" in ubuntu_sections
    assert any("cat /proc/meminfo" in c for c in ubuntu_sections["ram"])

    # RHEL script parsing
    rhel_sections = load_os_script("rhel")
    assert rhel_sections is not None
    assert "os_version" in rhel_sections
    assert "ram" in rhel_sections
    assert "disk" in rhel_sections
    assert "docker" in rhel_sections
    assert "kubernetes" in rhel_sections
    assert "drupal" in rhel_sections
    assert any("cat /etc/os-release" in c for c in rhel_sections["os_version"])

    # Windows script parsing
    windows_sections = load_os_script("windows")
    assert windows_sections is not None
    assert "os_version" in windows_sections
    assert "ram" in windows_sections
    assert "disk" in windows_sections
    assert any("Get-CimInstance" in c for c in windows_sections["ram"])


def test_custom_os_script_dynamic_loading():
    """Verify that a new script added to /automate/scripts/<os>.txt is dynamically loaded."""
    from mizalla.asyncssh_engine import load_os_script, SCRIPTS_DIR

    custom_file = SCRIPTS_DIR / "freebsd.txt"
    try:
        custom_content = """# os_version
uname -v

# ram
sysctl -n hw.physmem

# disk
df -k /

# custom_feature
echo enabled
"""
        custom_file.write_text(custom_content, encoding="utf-8")
        sections = load_os_script("freebsd")
        assert sections is not None
        assert "os_version" in sections
        assert "ram" in sections
        assert "disk" in sections
        assert "custom_feature" in sections
        assert sections["os_version"] == ["uname -v"]
        assert sections["ram"] == ["sysctl -n hw.physmem"]
        assert sections["disk"] == ["df -k /"]
        assert sections["custom_feature"] == ["echo enabled"]
    finally:
        if custom_file.exists():
            custom_file.unlink()


def test_core_tags_omission_when_features_do_not_exist(app):
    """Verify that features not present on the server are omitted from core_tags."""
    with app.app_context():
        # Server where only python and docker are present; drupal and external_storage are absent
        server = Server(
            name="Sparse Server",
            ip="10.0.0.50",
            operating_system="Ubuntu 22.04",
            core_tags="containerizers: Docker 24.0\r\nruntimes: Python 3.11",
        )
        db.session.add(server)
        db.session.commit()

        # Present checks
        assert server.has_tag("docker") is True
        assert server.has_tag("python") is True

        # Absent / omitted checks
        assert server.has_tag("drupal") is False
        assert server.has_tag("external_storage") is False
        assert server.has_tag("kubernetes") is False

        # Tag lines list
        lines = server.get_tag_lines()
        assert len(lines) == 2
        assert "containerizers: Docker 24.0" in lines
        assert "runtimes: Python 3.11" in lines

        # Tag values list (no prefix labels)
        values = server.get_tag_values()
        assert values == ["Docker 24.0", "Python 3.11"]


def test_probe_drupal_includes_installed_folder_path():
    """Verify _probe_drupal returns the installed folder path when detected."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from mizalla.asyncssh_engine import _probe_drupal

    mock_conn = MagicMock()

    async def mock_run(cmd, check=False, timeout=5.0):
        res = MagicMock()
        res.exit_status = 0
        if "VERSION" in cmd:
            res.stdout = "const VERSION = '10.2.1';\n"
        elif "Drupal.php" in cmd:
            res.stdout = "/var/www/drupal_site/core/lib/Drupal.php\n"
        elif "apache" in cmd or "nginx" in cmd or "php-fpm" in cmd:
            res.stdout = "1234 apache2\n"
        else:
            res.stdout = ""
        return res

    mock_conn.run = AsyncMock(side_effect=mock_run)
    result = asyncio.run(_probe_drupal(mock_conn, "linux"))
    assert result is not None
    assert "Drupal 10.2.1" in result
    assert "/var/www/drupal_site" in result
    assert "Running" in result
    assert result == "Drupal 10.2.1 [/var/www/drupal_site] [Running]"


def test_probe_containerizers_includes_docker_compose_yaml_paths():
    """Verify _probe_containerizers returns the docker compose YAML paths when detected."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from mizalla.asyncssh_engine import _probe_containerizers

    mock_conn = MagicMock()

    async def mock_run(cmd, check=False, timeout=5.0):
        res = MagicMock()
        res.exit_status = 0
        if "docker version" in cmd or "docker --version" in cmd:
            res.stdout = "Docker version 25.0.3, build 4debf41\n"
        elif "docker info" in cmd or "dockerd" in cmd:
            res.stdout = "Server Version: 25.0.3\n"
        elif "docker-compose" in cmd or "compose" in cmd:
            res.stdout = "/opt/my_stack/docker-compose.yml\n/srv/app/compose.yaml\n"
        elif "kubectl" in cmd:
            res.exit_status = 1
            res.stdout = ""
        else:
            res.stdout = ""
        return res

    mock_conn.run = AsyncMock(side_effect=mock_run)
    result = asyncio.run(_probe_containerizers(mock_conn, "linux"))
    assert result is not None
    assert "Docker 25.0.3 (Running)" in result
    assert "/opt/my_stack/docker-compose.yml" in result
    assert "/srv/app/compose.yaml" in result


def test_external_storage_shows_mounted_path_and_filters_home_mounts():
    """Verify _probe_external_storage returns 'Mounted: /WebVol02' and filters out user home mounts."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from mizalla.asyncssh_engine import _probe_external_storage

    mock_conn = MagicMock()
    # Output simulating user's system where /home/devopsadmin/azureStorage and /WebVol02 both match findmnt
    findmnt_output = (
        "/home/devopsadmin/azureStorage fuse //storage.file.core.windows.net\n"
        "/WebVol02 cifs //nfs-server/volume\n"
    )

    async def mock_run(cmd, check=False, timeout=5.0):
        res = MagicMock()
        res.exit_status = 0
        res.stdout = findmnt_output
        return res

    mock_conn.run = AsyncMock(side_effect=mock_run)
    result = asyncio.run(_probe_external_storage(mock_conn, "linux"))
    assert result == "Mounted: /WebVol02"


def test_drupal_returns_none_when_drupal_php_absent_even_if_apache_runs():
    """Verify _probe_drupal returns None if Drupal.php is not found, even if Apache is running."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from mizalla.asyncssh_engine import _probe_drupal

    mock_conn = MagicMock()

    async def mock_run(cmd, check=False, timeout=5.0):
        res = MagicMock()
        res.exit_status = 0
        if "Drupal.php" in cmd:
            res.stdout = ""  # Not found
            res.exit_status = 1
        elif "apache" in cmd or "httpd" in cmd:
            res.stdout = "1234 apache2\n"  # Apache is running
        else:
            res.stdout = ""
        return res

    mock_conn.run = AsyncMock(side_effect=mock_run)
    result = asyncio.run(_probe_drupal(mock_conn, "linux"))
    assert result is None


def test_nodejs_and_uvicorn_return_none_when_not_installed():
    """Verify nodejs and uvicorn return None when not installed, regardless of ps output."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from mizalla.asyncssh_engine import _probe_nodejs, _probe_uvicorn

    mock_conn = MagicMock()

    async def mock_run(cmd, check=False, timeout=5.0):
        res = MagicMock()
        # node -v and uvicorn --version fail
        if "node -v" in cmd or "uvicorn --version" in cmd:
            res.exit_status = 127
            res.stdout = "command not found\n"
        else:
            # Random background processes might have 'node' in ps aux
            res.exit_status = 0
            res.stdout = "root 123 0.0 0.1 /usr/bin/k8s-node-manager\n"
        return res

    mock_conn.run = AsyncMock(side_effect=mock_run)
    assert asyncio.run(_probe_nodejs(mock_conn, "linux")) is None
    assert asyncio.run(_probe_uvicorn(mock_conn, "linux")) is None


def test_parse_kubectl_pods():
    """Verify _parse_kubectl_pods correctly parses all-namespace and single-namespace outputs."""
    from mizalla.asyncssh_engine import _parse_kubectl_pods

    # 1. Output from kubectl get pods -A
    k8s_all_ns_output = """
NAMESPACE     NAME                               READY   STATUS             RESTARTS       AGE
kube-system   coredns-76f75df574-abcde           1/1     Running            0              10d
default       my-app-6c9f7b59-xyz12              1/1     Running            1 (2m ago)     2h
prod          api-gateway-5d8f76-m9q2            2/2     Running            0              45d
test          job-worker-completed-123           0/1     Completed          0              1d
ingress       traefik-9df8c                      0/1     CrashLoopBackOff   5 (1m ago)     30m
"""
    pods = _parse_kubectl_pods(k8s_all_ns_output)
    assert len(pods) == 5
    assert pods[0] == {
        "namespace": "kube-system",
        "name": "coredns-76f75df574-abcde",
        "ready": "1/1",
        "status": "Running",
        "age": "10d",
    }
    assert pods[1]["name"] == "my-app-6c9f7b59-xyz12"
    assert pods[1]["status"] == "Running"
    assert pods[1]["age"] == "2h"
    assert pods[4]["status"] == "CrashLoopBackOff"
    assert pods[4]["age"] == "30m"

    # 2. Output from kubectl get pods (single namespace)
    k8s_single_ns_output = """
NAME                               READY   STATUS      RESTARTS   AGE
web-server-79d5f                   1/1     Running     0          4h
db-migration-job                   0/1     Completed   0          5d
"""
    single_pods = _parse_kubectl_pods(k8s_single_ns_output)
    assert len(single_pods) == 2
    assert single_pods[0]["namespace"] == "default"
    assert single_pods[0]["name"] == "web-server-79d5f"
    assert single_pods[0]["status"] == "Running"
    assert single_pods[0]["age"] == "4h"

    # 3. No resources or empty
    assert _parse_kubectl_pods("No resources found in default namespace.") == []
    assert _parse_kubectl_pods("") == []


def test_server_k8s_pods_telemetry_and_ui(client, app):
    """Verify Server.k8s_pods is stored, parsed, and rendered on server cards and API."""
    import json
    login_admin(client, app)

    sample_pods = [
        {"namespace": "prod", "name": "web-api-abc", "ready": "1/1", "status": "Running", "age": "3d"},
        {"namespace": "prod", "name": "worker-xyz", "ready": "1/1", "status": "Running", "age": "1d"},
    ]

    with app.app_context():
        server = Server(
            name="K8s Worker Server",
            ip="192.168.1.130",
            operating_system="Ubuntu 22.04",
            agent_status="online",
            core_tags="kubernetes: Kubernetes v1.28.2 (Running)",
            k8s_pods=json.dumps(sample_pods),
        )
        db.session.add(server)
        db.session.commit()
        server_id = server.id

        assert server.has_telemetry is True
        parsed_pods = server.get_k8s_pods()
        assert len(parsed_pods) == 2
        assert parsed_pods[0]["name"] == "web-api-abc"
        assert parsed_pods[0]["status"] == "Running"
        assert parsed_pods[0]["age"] == "3d"

    # Verify JSON API connection-status
    api_resp = client.get(f"/apps/admin/servers/{server_id}/connection-status")
    assert api_resp.status_code == 200
    data = api_resp.get_json()
    assert "k8s_pods" in data
    assert len(data["k8s_pods"]) == 2
    assert data["k8s_pods"][0]["name"] == "web-api-abc"

    # Verify Admin UI card rendering
    admin_resp = client.get("/apps/admin/servers")
    assert admin_resp.status_code == 200
    admin_html = admin_resp.get_data(as_text=True)
    assert f'id="k8s-pods-container-{server_id}"' in admin_html
    assert "Kubernetes" in admin_html
    assert "background:lightgreen" in admin_html or "background: lightgreen" in admin_html or "background-color:lightgreen" in admin_html
    assert "rgba(144, 238, 144, 0.5)" in admin_html or "rgba(144,238,144,0.5)" in admin_html
    assert "web-api-abc" in admin_html
    assert "worker-xyz" in admin_html
    assert "Running" in admin_html
    assert "3d" in admin_html

    # Verify Remote Exec card rendering
    re_resp = client.get("/apps/remote-exec/", follow_redirects=True)
    assert re_resp.status_code == 200
    re_html = re_resp.get_data(as_text=True)
    assert f'id="k8s-pods-container-{server_id}"' in re_html
    assert "Kubernetes" in re_html
    assert "background:lightgreen" in re_html or "background: lightgreen" in re_html or "background-color:lightgreen" in re_html
    assert "rgba(144, 238, 144, 0.5)" in re_html or "rgba(144,238,144,0.5)" in re_html
    assert "web-api-abc" in re_html
    assert "worker-xyz" in re_html
    assert "Running" in re_html
    assert "3d" in re_html
    # Verify card displays Ubuntu rather than generic Linux
    assert "Ubuntu 22.04" in re_html


def test_probe_kubernetes_and_pods_separated():
    """Verify _probe_kubernetes and _probe_kubernetes_pods run and return cleanly when separated."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from mizalla.asyncssh_engine import _probe_kubernetes, _probe_kubernetes_pods, _probe_os

    mock_conn = MagicMock()

    # 1. Test _probe_kubernetes version detection
    async def mock_run_version(cmd, check=False, timeout=5.0):
        res = MagicMock()
        res.exit_status = 0
        res.stdout = "Client Version: v1.29.1\nKustomize Version: v5.0.4-0.20230601165947-6ce0bd390ce3\n"
        return res

    mock_conn.run = AsyncMock(side_effect=mock_run_version)
    k8s_tag = asyncio.run(_probe_kubernetes(mock_conn, "linux", cmds=["kubectl version --client 2>/dev/null"]))
    assert k8s_tag == "Kubernetes v1.29.1 (Running)"

    # 2. Test _probe_kubernetes_pods pod extraction
    async def mock_run_pods(cmd, check=False, timeout=8.0):
        res = MagicMock()
        res.exit_status = 0
        res.stdout = """
NAMESPACE     NAME                      READY   STATUS    RESTARTS   AGE
kube-system   coredns-76f75df574-abcde  1/1     Running   0          5d
default       app-pod-123               1/1     Running   0          1h
"""
        return res

    mock_conn.run = AsyncMock(side_effect=mock_run_pods)
    tag_str, pods = asyncio.run(_probe_kubernetes_pods(mock_conn, "linux", cmds=["kubectl get pods -A 2>/dev/null"]))
    assert tag_str is None
    assert len(pods) == 2
    assert pods[0]["name"] == "coredns-76f75df574-abcde"
    assert pods[1]["name"] == "app-pod-123"

    # 3. Test _probe_os extracts Ubuntu distro and version
    async def mock_run_os(cmd, check=False, timeout=5.0):
        res = MagicMock()
        res.exit_status = 0
        res.stdout = """NAME="Ubuntu"
VERSION="22.04.4 LTS (Jammy Jellyfish)"
ID=ubuntu
ID_LIKE=debian
PRETTY_NAME="Ubuntu 22.04.4 LTS"
VERSION_ID="22.04"
"""
        return res

    mock_conn.run = AsyncMock(side_effect=mock_run_os)
    os_info = asyncio.run(_probe_os(mock_conn, "linux", cmds=["cat /etc/os-release 2>/dev/null"]))
    assert os_info["os_type"] == "ubuntu"
    assert os_info["operating_system"] == "Ubuntu 22.04.4 LTS"
    assert os_info["os_version"] == "Ubuntu 22.04.4 LTS"


def test_kubectl_detected_runs_pods_and_renders_panel(client, app):
    """Test that when Kubectl is detected for a server, pod IDs are listed in a Kubernetes panel

    with heading in light green and body in the same light green with alpha 0.5.
    """
    import json
    from mizalla.asyncssh_engine import _parse_kubectl_pods
    login_admin(client, app)

    # 1. Test _parse_kubectl_pods with pod ID lists
    pod_output = """
pod/nginx-deployment-7b864d4b8f-12345
pod/redis-leader-0
pod/worker-queue-abcde
"""
    parsed = _parse_kubectl_pods(pod_output)
    assert len(parsed) == 3
    assert parsed[0]["name"] == "nginx-deployment-7b864d4b8f-12345"
    assert parsed[1]["name"] == "redis-leader-0"
    assert parsed[2]["name"] == "worker-queue-abcde"

    # 2. Test rendering on Server cards
    with app.app_context():
        server = Server(
            name="Prod Kubernetes Node",
            ip="10.0.0.50",
            operating_system="Ubuntu 24.04",
            agent_status="online",
            core_tags="kubernetes: Kubernetes v1.30.0 (Running)",
            k8s_pods=json.dumps(parsed),
        )
        db.session.add(server)
        db.session.commit()
        sid = server.id

        assert server.has_k8s is True
        pods = server.get_k8s_pods()
        assert len(pods) == 3
        assert pods[0]["id"] == "nginx-deployment-7b864d4b8f-12345"

    resp = client.get("/apps/admin/servers")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Verify panel exists
    assert f'id="k8s-pods-container-{sid}"' in html
    # Verify heading is "Kubernetes"
    assert "Kubernetes" in html
    # Verify heading background is light green
    assert "background:lightgreen" in html or "background: lightgreen" in html or "background-color:lightgreen" in html
    # Verify body background is the same light green with alpha 0.5
    assert "rgba(144, 238, 144, 0.5)" in html or "rgba(144,238,144,0.5)" in html
    # Verify each pod ID is rendered in the body
    assert "nginx-deployment-7b864d4b8f-12345" in html
    assert "redis-leader-0" in html
    assert "worker-queue-abcde" in html


def test_k8s_pods_show_running_age_in_brackets(client, app):
    """Test that when displaying k8s pods, running age is displayed in brackets () with each pod ID."""
    import json
    from mizalla.asyncssh_engine import _parse_kubectl_pods
    login_admin(client, app)

    # Output from kubectl get pods -A or kubectl get pods
    kubectl_output = """
NAMESPACE     NAME                                READY   STATUS    RESTARTS   AGE
default       web-api-7b864d4b8f-11111            1/1     Running   0          3d2h
kube-system   coredns-787d4945fb-22222            1/1     Running   0          14d
monitoring    prometheus-server-0                 2/2     Running   1          28d
"""
    parsed = _parse_kubectl_pods(kubectl_output)
    assert len(parsed) == 3
    assert parsed[0]["name"] == "web-api-7b864d4b8f-11111"
    assert parsed[0]["age"] == "3d2h"
    assert parsed[1]["age"] == "14d"
    assert parsed[2]["age"] == "28d"

    with app.app_context():
        server = Server(
            name="Cluster Node With Pod Ages",
            ip="10.0.5.10",
            operating_system="Ubuntu 22.04",
            agent_status="online",
            core_tags="kubernetes: Kubernetes v1.28.0 (Running)",
            k8s_pods=json.dumps(parsed),
        )
        db.session.add(server)
        db.session.commit()
        sid = server.id

    resp = client.get("/apps/admin/servers")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Verify user running pod IDs are displayed with running age in brackets ()
    assert "web-api-7b864d4b8f-11111 (3d2h)" in html
    assert "prometheus-server-0 (28d)" in html
    # kube-system pods should NOT be shown
    assert "coredns-787d4945fb-22222" not in html


def test_k8s_pods_filters_kube_system_and_non_running(client, app):
    """Verify that when showing k8s pods, kube-system pods and non-running pods are never shown."""
    import json
    from mizalla.asyncssh_engine import _parse_kubectl_pods
    login_admin(client, app)

    k8s_mixed_output = """
NAMESPACE     NAME                               READY   STATUS             RESTARTS   AGE
kube-system   coredns-76f75df574-abcde           1/1     Running            0          10d
kube-system   kube-proxy-j9x2k                   1/1     Running            0          10d
default       frontend-web-123                   1/1     Running            0          2h
production    api-service-456                    2/2     Running            1          5d
analytics     spark-job-done-789                 0/1     Completed          0          1d
ingress       nginx-ingress-failing              0/1     CrashLoopBackOff   4          30m
staging       worker-starting                    0/1     Pending            0          5m
"""
    # 1. Verify _parse_kubectl_pods with only_user_running=True
    user_running = _parse_kubectl_pods(k8s_mixed_output, only_user_running=True)
    assert len(user_running) == 2
    assert user_running[0]["name"] == "frontend-web-123"
    assert user_running[0]["namespace"] == "default"
    assert user_running[1]["name"] == "api-service-456"
    assert user_running[1]["namespace"] == "production"

    # 2. Verify server.get_k8s_pods() filters out kube-system and non-running pods
    all_parsed = _parse_kubectl_pods(k8s_mixed_output)
    assert len(all_parsed) == 7

    with app.app_context():
        server = Server(
            name="K8s Filter Test Node",
            ip="10.0.99.1",
            operating_system="Ubuntu 22.04",
            agent_status="online",
            core_tags="kubernetes: Kubernetes v1.29.0 (Running)",
            k8s_pods=json.dumps(all_parsed),
        )
        db.session.add(server)
        db.session.commit()
        sid = server.id

        displayed_pods = server.get_k8s_pods()
        assert len(displayed_pods) == 2
        pod_names = [p["name"] for p in displayed_pods]
        assert "frontend-web-123" in pod_names
        assert "api-service-456" in pod_names
        assert "coredns-76f75df574-abcde" not in pod_names
        assert "kube-proxy-j9x2k" not in pod_names
        assert "spark-job-done-789" not in pod_names
        assert "nginx-ingress-failing" not in pod_names
        assert "worker-starting" not in pod_names

        # Raw pods can still be retrieved with only_user_running=False
        assert len(server.get_k8s_pods(only_user_running=False)) == 7

    # 3. Verify HTML rendering on admin servers page
    resp = client.get("/apps/admin/servers")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "frontend-web-123" in html
    assert "api-service-456" in html
    assert "coredns-76f75df574-abcde" not in html
    assert "kube-proxy-j9x2k" not in html
    assert "spark-job-done-789" not in html
    assert "nginx-ingress-failing" not in html
    assert "worker-starting" not in html

    # 4. Verify admin connection-status endpoint returns only user running pods
    resp_telemetry = client.get(f"/apps/admin/servers/{sid}/connection-status")
    assert resp_telemetry.status_code == 200
    t_data = resp_telemetry.get_json()
    assert len(t_data["k8s_pods"]) == 2
    t_names = [p["name"] for p in t_data["k8s_pods"]]
    assert "frontend-web-123" in t_names
    assert "api-service-456" in t_names
    assert "coredns-76f75df574-abcde" not in t_names

    # 5. Verify remote exec dashboard renders only user running pods
    re_resp = client.get("/apps/remote-exec/", follow_redirects=True)
    assert re_resp.status_code == 200
    re_html = re_resp.get_data(as_text=True)
    assert "frontend-web-123" in re_html
    assert "api-service-456" in re_html
    assert "coredns-76f75df574-abcde" not in re_html
    assert "kube-proxy-j9x2k" not in re_html
    assert "spark-job-done-789" not in re_html
    assert "nginx-ingress-failing" not in re_html
    assert "worker-starting" not in re_html


def test_asyncssh_locate_and_read_docker_compose():
    """Test _locate_docker_compose_file and _read_docker_compose_file helpers."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from mizalla.asyncssh_engine import _locate_docker_compose_file, _read_docker_compose_file

    mock_conn = MagicMock()
    # Mock locate
    mock_locate_res = MagicMock()
    mock_locate_res.exit_status = 0
    mock_locate_res.stdout = "/opt/myapp/docker-compose.yml\n"
    mock_conn.run = AsyncMock(return_value=mock_locate_res)

    loc = asyncio.run(_locate_docker_compose_file(mock_conn, "linux"))
    assert loc == "/opt/myapp/docker-compose.yml"

    # Mock read
    sample_yaml = "version: '3.8'\nservices:\n  app:\n    image: python:3.11\n    restart: always"
    mock_read_res = MagicMock()
    mock_read_res.exit_status = 0
    mock_read_res.stdout = sample_yaml
    mock_conn.run = AsyncMock(return_value=mock_read_res)

    content = asyncio.run(_read_docker_compose_file(mock_conn, loc, "linux"))
    assert "version: '3.8'" in content
    assert "image: python:3.11" in content


def test_docker_compose_detected_renders_collapsed_accordion_with_yaml(client, app):
    """Test that when docker compose is detected, an accordion with heading 'docker-compose.yml'

    is rendered with full YAML contents and hidden (collapsed) by default.
    """
    login_admin(client, app)

    sample_yaml = """version: '3.8'
services:
  web:
    image: nginx:alpine
    ports:
      - "80:80"
  db:
    image: postgres:15
    environment:
      POSTGRES_PASSWORD: secretpassword
"""

    with app.app_context():
        server = Server(
            name="Docker Compose App Server",
            ip="192.168.1.120",
            operating_system="Ubuntu 22.04",
            agent_status="online",
            core_tags="docker: Docker 26.0.0 (Running) [Compose: /opt/app/docker-compose.yml]",
            docker_compose_content=sample_yaml,
        )
        db.session.add(server)
        db.session.commit()
        sid = server.id

        assert server.has_docker_compose is True
        assert server.has_telemetry is True
        assert "services:" in server.docker_compose_content

    # 1. Verify on Admin Environments & Servers page
    resp = client.get("/apps/admin/servers")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Accordion element exists
    assert f'id="docker-compose-accordion-{sid}"' in html
    assert f'id="docker-compose-content-{sid}"' in html

    # Accordion heading is "docker-compose.yml"
    assert "docker-compose.yml" in html

    # Accordion is collapsed by default (does NOT contain open attribute on this details)
    # Extract the details tag
    accordion_start = html.find(f'id="docker-compose-accordion-{sid}"')
    details_tag = html[accordion_start - 30 : accordion_start + 60]
    assert "open" not in details_tag.split(">")[0]

    # Body contains formatted YAML content
    assert "image: nginx:alpine" in html
    assert "image: postgres:15" in html
    assert 'POSTGRES_PASSWORD: secretpassword' in html

    # 2. Verify connection status endpoint returns docker compose content and path
    status_resp = client.get(f"/apps/admin/servers/{sid}/connection-status")
    assert status_resp.status_code == 200
    data = status_resp.get_json()
    assert data["has_docker_compose"] is True
    assert "version: '3.8'" in data["docker_compose_content"]
    assert data["docker_compose_path"] == "/opt/app/docker-compose.yml"

    # Heading displays path in brackets next to docker-compose.yml
    assert "docker-compose.yml (/opt/app/docker-compose.yml)" in html


def test_rhel_ram_and_storage_parsers():
    """Verify RHEL RAM and Storage parsers handle all RHEL output formats (free, /proc/meminfo without MemAvailable, wrapped df)."""
    from mizalla.asyncssh_engine import _parse_linux_ram, _parse_linux_disk

    # 1. RHEL /proc/meminfo without MemAvailable (standard on RHEL 6 and early RHEL 7 kernels)
    rhel_meminfo_no_avail = """MemTotal:       16384000 kB
MemFree:         4096000 kB
Buffers:          512000 kB
Cached:          3584000 kB
SReclaimable:     204800 kB
SwapTotal:       2097148 kB
"""
    res_ram = _parse_linux_ram(rhel_meminfo_no_avail)
    assert res_ram is not None
    assert res_ram["ram_total"] == "15.6 GB"
    assert float(res_ram["ram_percent"]) > 0

    # 2. RHEL free -b (bytes) output
    rhel_free_b = """              total        used        free      shared  buff/cache   available
Mem:    17179869184  4294967296  8589934592   12345678  4294967296 12884901888
Swap:    2147483648           0  2147483648
"""
    res_free = _parse_linux_ram(rhel_free_b)
    assert res_free is not None
    assert res_free["ram_total"] == "16.0 GB"
    assert res_free["ram_used"] == "4.0 GB"
    assert res_free["ram_percent"] == 25.0

    # 3. RHEL free -m (megabytes) output
    rhel_free_m = """              total        used        free      shared  buff/cache   available
Mem:          16384        4096        8192          12        4096       12288
Swap:          2048           0        2048
"""
    res_free_m = _parse_linux_ram(rhel_free_m)
    assert res_free_m is not None
    assert res_free_m["ram_total"] == "16.0 GB"
    assert res_free_m["ram_used"] == "4.0 GB"
    assert res_free_m["ram_percent"] == 25.0

    # 4. RHEL POSIX df -Pk / output (unwrapped)
    rhel_df_posix = """Filesystem         1024-blocks      Used Available Capacity Mounted on
/dev/mapper/rhel-root 52428800  10485760  41943040      20% /
"""
    res_disk_p = _parse_linux_disk(rhel_df_posix)
    assert res_disk_p is not None
    assert res_disk_p["disk_total"] == "50.0 GB"
    assert res_disk_p["disk_used"] == "10.0 GB"
    assert res_disk_p["disk_percent"] == 20.0

    # 5. RHEL df -k / with wrapped line due to long LVM device name
    rhel_df_wrapped = """Filesystem           1K-blocks      Used Available Use% Mounted on
/dev/mapper/rhel_production--server--01-root
                      52428800  10485760  41943040  20% /
"""
    res_disk_w = _parse_linux_disk(rhel_df_wrapped)
    assert res_disk_w is not None
    assert res_disk_w["disk_total"] == "50.0 GB"
    assert res_disk_w["disk_used"] == "10.0 GB"
    assert res_disk_w["disk_percent"] == 20.0


