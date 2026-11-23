"""Tests for Command Group execution aborting, SSH connection termination,
and Admin UI Add Server button label.
"""

from unittest.mock import MagicMock, patch

import pytest
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    COMMAND_STATUS_PENDING,
    COMMAND_STATUS_RUNNING,
    COMMAND_STATUS_TERMINATED,
    COMMAND_TYPE_REMOTE,
    FEATURE_REMOTE_EXEC,
    CommandAudit,
    CommandGroup,
    CommandGroupItem,
    CommandItem,
    Member,
    Server,
    ServerAccess,
    ServerCredential,
)
from mizalla.remote_exec_client import (
    _active_connections,
    _active_loops,
    _threads_lock,
    terminate_running_audit,
)


@pytest.fixture
def abort_test_data(app):
    """Sets up an admin, regular user, and server."""
    with app.app_context():
        admin = Member(
            name="Admin Tester",
            email="admin_abort_test@example.com",
            is_admin=True,
            can_add_command_items=True,
            can_add_command_groups=True,
        )
        admin.set_password("AdminPass123!")
        db.session.add(admin)

        user = Member(
            name="Standard User",
            email="user_abort_test@example.com",
            is_admin=False,
            can_add_command_items=False,
            can_add_command_groups=False,
        )
        user.set_password("UserPass123!")
        db.session.add(user)

        srv = Server(
            name="Abort Target Server",
            ip="192.168.10.200",
            os_type="linux",
            ssh_port=22,
            status="available",
        )
        db.session.add(srv)
        db.session.flush()

        cred = ServerCredential(
            server_id=srv.id,
            is_default=True,
            username="root",
        )
        cred.password = "RootPass!"
        db.session.add(cred)

        item = CommandItem(
            name="Sleep Step",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="sleep 60",
            created_by_id=admin.id,
        )
        db.session.add(item)
        db.session.flush()

        grp = CommandGroup(
            name="Long Running Group",
            server_id=srv.id,
            run_in_parallel=False,
            created_by_id=admin.id,
        )
        db.session.add(grp)
        db.session.flush()

        db.session.add(CommandGroupItem(group_id=grp.id, command_item_id=item.id, sequence_order=1))
        db.session.commit()

        sync_member_features(admin, [FEATURE_REMOTE_EXEC])
        sync_member_features(user, [FEATURE_REMOTE_EXEC])

        with app.test_request_context():
            db.session.add(ServerAccess(server_id=srv.id, member_id=user.id, can_view=True, can_edit=False, connection_status="available"))
            db.session.commit()

        return {
            "admin_id": admin.id,
            "user_id": user.id,
            "server_id": srv.id,
            "group_id": grp.id,
            "item_id": item.id,
        }


def test_admin_ui_add_server_button_label(client, app, abort_test_data):
    """Verifies that in Admin UI (/apps/admin/servers), the button says 'Add Server' not 'Create server'."""
    admin_id = abort_test_data["admin_id"]
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)

    resp = client.get("/apps/admin/servers")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert "Add Server" in html
    assert "Create server" not in html


def test_dashboard_contains_stop_button(client, app, abort_test_data):
    """Verifies that the Remote Exec dashboard renders the Stop button for command groups."""
    admin_id = abort_test_data["admin_id"]
    group_id = abort_test_data["group_id"]
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)

    resp = client.get("/apps/remote-exec/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert f"group-abort-btn-{group_id}" in html
    assert f"abortCommandGroup({group_id})" in html
    assert "Stop" in html


def test_terminate_running_audit_cleans_up_ssh_connection(app, abort_test_data):
    """Verifies that terminate_running_audit aborts active SSH connection and cancels loop tasks."""
    srv_id = abort_test_data["server_id"]
    admin_id = abort_test_data["admin_id"]
    group_id = abort_test_data["group_id"]

    with app.app_context():
        audit = CommandAudit(
            task_id="grp_test_abort_123",
            entry_type="group",
            group_id=group_id,
            server_id=srv_id,
            user_id=admin_id,
            title="Long Running Group",
            command="echo test",
            status=COMMAND_STATUS_RUNNING,
            steps_data=[
                {"item_id": 1, "name": "Step 1", "status": "running", "output": "starting..."},
            ],
        )
        db.session.add(audit)
        db.session.commit()
        audit_id = audit.id

    # Mock SSH connection and active loop
    mock_conn = MagicMock()
    mock_loop = MagicMock()
    mock_loop.is_closed.return_value = False

    with _threads_lock:
        _active_connections[audit_id] = {mock_conn}
        _active_loops[audit_id] = mock_loop

    try:
        ok = terminate_running_audit(audit_id, app=app)
        assert ok is True

        # Check SSH connection abort or close was called
        assert mock_conn.abort.called or mock_conn.close.called

        # Check audit status in DB
        with app.app_context():
            updated = db.session.get(CommandAudit, audit_id)
            assert updated.status == COMMAND_STATUS_TERMINATED
            assert updated.exit_code == 130
            assert "Execution Aborted and SSH Connection Closed by User" in updated.full_output
            assert updated.steps_data[0]["status"] == "terminated"
    finally:
        with _threads_lock:
            _active_connections.pop(audit_id, None)
            _active_loops.pop(audit_id, None)


def test_abort_group_endpoint(client, app, abort_test_data):
    """Verifies the /apps/remote-exec/groups/<group_id>/abort endpoint."""
    admin_id = abort_test_data["admin_id"]
    group_id = abort_test_data["group_id"]
    srv_id = abort_test_data["server_id"]

    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)

    # 1. When no active execution exists
    resp_none = client.post(f"/apps/remote-exec/groups/{group_id}/abort")
    assert resp_none.status_code == 200
    data_none = resp_none.get_json()
    assert data_none["success"] is False
    assert "No active execution found" in data_none["message"]

    # 2. When an active execution is running
    with app.app_context():
        audit = CommandAudit(
            task_id="grp_test_active_456",
            entry_type="group",
            group_id=group_id,
            server_id=srv_id,
            user_id=admin_id,
            title="Running Group",
            command="echo test",
            status=COMMAND_STATUS_RUNNING,
        )
        db.session.add(audit)
        db.session.commit()
        active_audit_id = audit.id

    mock_conn = MagicMock()
    with _threads_lock:
        _active_connections[active_audit_id] = {mock_conn}

    try:
        resp_abort = client.post(f"/apps/remote-exec/groups/{group_id}/abort")
        assert resp_abort.status_code == 200
        data_abort = resp_abort.get_json()
        assert data_abort["success"] is True
        assert data_abort["audit_id"] == active_audit_id
        assert "aborted" in data_abort["message"].lower()

        # Verify DB status
        with app.app_context():
            a = db.session.get(CommandAudit, active_audit_id)
            assert a.status == COMMAND_STATUS_TERMINATED
    finally:
        with _threads_lock:
            _active_connections.pop(active_audit_id, None)
