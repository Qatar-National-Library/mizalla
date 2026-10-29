"""Tests for GOWA service lifecycle management (PID history, auto clean-up, startup, and teardown)."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mizalla import create_app
from mizalla.extensions import db
from mizalla.gowa import get_mcp_base_url
from mizalla.gowa_manager import (
    MAX_PID_HISTORY,
    PID_HISTORY_FILENAME,
    build_command_line,
    cleanup_old_pids,
    clear_current_pid,
    get_gowa_process_status,
    is_pid_running,
    load_pid_history,
    save_pid_history,
    start_gowa_service,
    stop_gowa_service,
    terminate_pid,
)
from mizalla.models import AppSetting, Member


@pytest.fixture
def test_app(tmp_path):
    """Create a test application instance with temporary instance folder."""
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "INSTANCE_PATH": str(tmp_path),
            "MANAGE_GOWA": False,
            "GOWA_PORT": 5000,
            "GOWA_EXECUTABLE": str(tmp_path / "gowa.exe"),
        }
    )
    app.instance_path = str(tmp_path)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()


def test_pid_history_load_and_save_capped_at_five(tmp_path):
    """PID history should record up to 5 PIDs in FIFO/newest-first order and track current_pid."""
    # Initially empty
    state = load_pid_history(tmp_path)
    assert state == {"current_pid": None, "history": []}

    # Add 6 PIDs
    pids = [101, 102, 103, 104, 105, 106]
    for p in pids:
        save_pid_history(tmp_path, p, mark_current=True)

    loaded = load_pid_history(tmp_path)
    assert loaded["current_pid"] == 106
    assert len(loaded["history"]) == 5
    assert loaded["history"] == [106, 105, 104, 103, 102]

    # Saving an already existing PID brings it to the top without duplicates
    save_pid_history(tmp_path, 104, mark_current=True)
    refreshed = load_pid_history(tmp_path)
    assert refreshed["current_pid"] == 104
    assert refreshed["history"] == [104, 106, 105, 103, 102]

    # Clearing current_pid marks current_pid as None while retaining history
    clear_current_pid(tmp_path)
    cleared = load_pid_history(tmp_path)
    assert cleared["current_pid"] is None
    assert cleared["history"] == [104, 106, 105, 103, 102]


def test_is_pid_running_invalid_pid():
    """is_pid_running should reject None or non-positive PIDs immediately."""
    assert not is_pid_running(None)
    assert not is_pid_running(0)
    assert not is_pid_running(-1)


def test_terminate_pid_mocked():
    """terminate_pid should return True if process is not running or is terminated."""
    with patch("mizalla.gowa_manager.is_pid_running", side_effect=[True, False]):
        with patch("subprocess.run") as mock_sub:
            success = terminate_pid(1234, timeout=0.5)
            assert success is True
            assert mock_sub.called


def test_cleanup_old_pids(tmp_path):
    """cleanup_old_pids should check each PID in history and terminate active ones."""
    save_pid_history(tmp_path, 201)
    save_pid_history(tmp_path, 202)
    save_pid_history(tmp_path, 203)

    # Pretend 203 and 201 are running, but 202 is dead
    def fake_running(pid):
        return pid in (203, 201)

    with patch("mizalla.gowa_manager.is_pid_running", side_effect=fake_running):
        with patch("mizalla.gowa_manager.terminate_pid", return_value=True) as mock_term:
            terminated = cleanup_old_pids(tmp_path)
            assert 203 in terminated
            assert 201 in terminated
            assert 202 not in terminated
            assert mock_term.call_count == 2


def test_build_command_line():
    """build_command_line should format commands for binary and shell scripts."""
    # Binary
    cmd_exe = build_command_line(Path("/opt/gowa/gowa.exe"), 5000)
    assert cmd_exe == [str(Path("/opt/gowa/gowa.exe")), "--port", "5000"]

    # Shell script
    cmd_sh = build_command_line(Path("/opt/gowa/start.sh"), 5050)
    assert cmd_sh == ["sh", str(Path("/opt/gowa/start.sh")), "--port", "5050"]


def test_start_gowa_service_disabled(test_app):
    """start_gowa_service returns False immediately if MANAGE_GOWA is False."""
    test_app.config["MANAGE_GOWA"] = False
    assert start_gowa_service(test_app) is False


def test_start_gowa_service_missing_executable(test_app, tmp_path):
    """start_gowa_service logs a warning and returns False if executable does not exist."""
    test_app.config["MANAGE_GOWA"] = True
    test_app.config["GOWA_EXECUTABLE"] = str(tmp_path / "non_existent_binary.exe")

    result = start_gowa_service(test_app)
    assert result is False


def test_start_gowa_service_spawns_and_records_pid(test_app, tmp_path):
    """start_gowa_service spawns the process, sets PORT env and --port arg, and writes PID history."""
    exec_file = tmp_path / "gowa.exe"
    exec_file.write_text("binary content", encoding="utf-8")

    test_app.config["MANAGE_GOWA"] = True
    test_app.config["GOWA_EXECUTABLE"] = str(exec_file)
    test_app.config["GOWA_PORT"] = 5123

    mock_proc = MagicMock()
    mock_proc.pid = 7890
    mock_proc.poll.return_value = None

    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        with patch("mizalla.gowa_manager.cleanup_old_pids"):
            with patch("mizalla.gowa_manager.register_shutdown_hooks"):
                started = start_gowa_service(test_app)
                assert started is True

                # Check Popen arguments
                assert mock_popen.called
                args, kwargs = mock_popen.call_args
                cmd = args[0]
                assert str(exec_file) in cmd[0]
                assert "--port" in cmd
                assert "5123" in cmd
                assert kwargs["env"]["PORT"] == "5123"

                # Check PID history
                state = load_pid_history(tmp_path)
                assert state["current_pid"] == 7890
                assert 7890 in state["history"]


def test_stop_gowa_service(tmp_path):
    """stop_gowa_service should terminate active process and clear current PID."""
    save_pid_history(tmp_path, 8888, mark_current=True)

    with patch("mizalla.gowa_manager.is_pid_running", return_value=True):
        with patch("mizalla.gowa_manager.terminate_pid", return_value=True) as mock_term:
            stop_gowa_service(tmp_path)
            assert mock_term.called
            state = load_pid_history(tmp_path)
            assert state["current_pid"] is None
            assert 8888 in state["history"]


def test_get_gowa_process_status(test_app, tmp_path):
    """get_gowa_process_status should report execution status, port, and history."""
    test_app.config["MANAGE_GOWA"] = True
    test_app.config["GOWA_PORT"] = 5000
    save_pid_history(tmp_path, 4321, mark_current=True)

    with patch("mizalla.gowa_manager.is_pid_running", return_value=True):
        status = get_gowa_process_status(test_app)
        assert status["managed"] is True
        assert status["running"] is True
        assert status["pid"] == 4321
        assert status["port"] == 5000
        assert 4321 in status["history"]


def test_werkzeug_reloader_guard_logic():
    """Werkzeug reloader worker triggers start, parent reloader skips start."""
    with patch("mizalla.gowa_manager.start_gowa_service") as mock_start:
        # Reloader parent process (WERKZEUG_RUN_MAIN="false")
        with patch.dict(os.environ, {"WERKZEUG_RUN_MAIN": "false", "MANAGE_GOWA": "true"}):
            app = create_app({"TESTING": True, "MANAGE_GOWA": True})
            assert mock_start.call_count == 0

    with patch("mizalla.gowa_manager.start_gowa_service") as mock_start:
        # Reloader worker process (WERKZEUG_RUN_MAIN="true")
        with patch.dict(os.environ, {"WERKZEUG_RUN_MAIN": "true", "MANAGE_GOWA": "true"}):
            app = create_app({"TESTING": True, "MANAGE_GOWA": True})
            assert mock_start.call_count == 1

    with patch("mizalla.gowa_manager.start_gowa_service") as mock_start:
        # Non-reloader / production (WERKZEUG_RUN_MAIN not set)
        env = dict(os.environ)
        env.pop("WERKZEUG_RUN_MAIN", None)
        with patch.dict(os.environ, env, clear=True):
            app = create_app({"TESTING": True, "MANAGE_GOWA": True})
            assert mock_start.call_count == 1


def test_get_mcp_base_url_default_with_manage_gowa(test_app):
    """get_mcp_base_url should use http://127.0.0.1:{GOWA_PORT} when MANAGE_GOWA is active."""
    test_app.config["MANAGE_GOWA"] = True
    test_app.config["GOWA_PORT"] = 5000

    with test_app.app_context():
        # Without explicit DB override
        url = get_mcp_base_url()
        assert url == "http://127.0.0.1:5000"

        # With explicit DB setting override
        custom_setting = AppSetting(key="gowa_mcp_base_url", value="http://192.168.1.100:8080")
        db.session.add(custom_setting)
        db.session.commit()

        url_custom = get_mcp_base_url()
        assert url_custom == "http://192.168.1.100:8080"


def test_admin_whatsapp_devices_view_renders_lifecycle_card(app, client):
    """WhatsApp devices template displays GOWA lifecycle manager card when enabled."""
    app.config["MANAGE_GOWA"] = True
    app.config["GOWA_PORT"] = 5000

    with app.app_context():
        admin = Member(name="Admin", email="admin_gowa@example.com", is_admin=True)
        admin.set_password("AdminPass1234!")
        db.session.add(admin)
        db.session.commit()

    client.post("/apps/login", data={"email": "admin_gowa@example.com", "password": "AdminPass1234!"})

    with patch("mizalla.whatsapp.check_health", return_value={"online": False, "status_code": None, "url": "http://127.0.0.1:5000", "error": "Offline"}):
        with patch("mizalla.gowa_manager.get_gowa_process_status", return_value={
            "managed": True,
            "running": True,
            "pid": 6543,
            "port": 5000,
            "executable": "/gowa/gowa.exe",
            "log_file": "instance/gowa.log",
            "history": [6543, 6200],
        }):
            res = client.get("/apps/whatsapp/devices")
            assert res.status_code == 200
            html = res.data.decode("utf-8")
            assert "GOWA Auto-Lifecycle" in html
            assert "Running (PID: 6543)" in html
            assert "6543, 6200" in html
            assert "Fetch Devices from GOWA" in html
