"""Comprehensive lifecycle and permissions tests for Command Items and Command Groups:
- Create Command Items & Command Groups
- Drag and drop / reorder sequence in Command Groups
- Duplicate Command Items & Command Groups
- Delete Command Items & Command Groups
- Run and Rerun Command Groups with live step status (running spinner, green check, red check)
- View logs via polling and last-10 output
- Granular permissions and access control for logged-in users
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    COMMAND_STATUS_COMPLETED,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PENDING,
    COMMAND_STATUS_RUNNING,
    COMMAND_TYPE_LOCAL,
    COMMAND_TYPE_REMOTE,
    COMMAND_TYPE_UPLOAD,
    FEATURE_REMOTE_EXEC,
    CommandAudit,
    CommandGroup,
    CommandGroupItem,
    CommandGroupPermission,
    CommandItem,
    CommandItemPermission,
    Member,
    Server,
    ServerAccess,
    ServerCredential,
)
from mizalla.remote_exec_client import (
    dispatch_command_group,
    user_can_add_group,
    user_can_add_item,
    user_can_delete_group,
    user_can_delete_item,
    user_can_duplicate_group,
    user_can_duplicate_item,
    user_can_edit_group,
    user_can_edit_item,
    user_can_run_group,
    user_can_view_group,
    user_can_view_group_logs,
)


@pytest.fixture
def setup_lifecycle_data(app):
    """Sets up an admin, a standard user, and a target server."""
    with app.app_context():
        # Admin user
        admin = Member(
            name="Lifecycle Admin",
            email="admin_lifecycle@example.com",
            is_admin=True,
            can_add_command_items=True,
            can_add_command_groups=True,
        )
        admin.set_password("AdminPass123!")
        db.session.add(admin)

        # Standard user without initial permissions
        user = Member(
            name="Standard Operator",
            email="operator_lifecycle@example.com",
            is_admin=False,
            can_add_command_items=False,
            can_add_command_groups=False,
        )
        user.set_password("UserPass123!")
        db.session.add(user)

        # Server
        srv = Server(
            name="Integration Test Server",
            ip="192.168.10.150",
            os_type="ubuntu",
            ssh_port=22,
            status="available",
        )
        db.session.add(srv)
        db.session.flush()

        # Credentials
        cred = ServerCredential(
            server_id=srv.id,
            is_default=True,
            username="testadmin",
        )
        cred.password = "SecretPass!"
        db.session.add(cred)

        db.session.commit()

        sync_member_features(admin, [FEATURE_REMOTE_EXEC])
        sync_member_features(user, [FEATURE_REMOTE_EXEC])

        # Grant remote_exec feature access
        with app.test_request_context():
            db.session.add(ServerAccess(server_id=srv.id, member_id=user.id, can_view=True, can_edit=False, connection_status="available"))
            db.session.commit()

        return {
            "admin_id": admin.id,
            "user_id": user.id,
            "server_id": srv.id,
        }


def test_command_items_and_groups_crud_and_reorder(client, app, setup_lifecycle_data):
    """Verifies creating items, adding/reordering them in a group, and drag-and-drop sequence updating."""
    data = setup_lifecycle_data
    admin_id = data["admin_id"]
    srv_id = data["server_id"]

    # Log in as Admin
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)

    # 1. Create 3 Command Items via AJAX API
    resp1 = client.post(
        "/apps/remote-exec/items/save",
        data={
            "ajax": "1",
            "name": "Step 1: Check Disk",
            "command_type": "remote",
            "command_content": "df -h",
        },
    )
    assert resp1.status_code == 200
    item1_id = resp1.get_json()["item"]["id"]

    resp2 = client.post(
        "/apps/remote-exec/items/save",
        data={
            "ajax": "1",
            "name": "Step 2: Check Memory",
            "command_type": "remote",
            "command_content": "free -m",
        },
    )
    assert resp2.status_code == 200
    item2_id = resp2.get_json()["item"]["id"]

    resp3 = client.post(
        "/apps/remote-exec/items/save",
        data={
            "ajax": "1",
            "name": "Step 3: Host Info",
            "command_type": "local",
            "command_content": "hostname",
        },
    )
    assert resp3.status_code == 200
    item3_id = resp3.get_json()["item"]["id"]

    # 2. Create Command Group with initial sequence [Item 1, Item 2, Item 3]
    grp_resp = client.post(
        "/apps/remote-exec/groups/new",
        data={
            "name": "Server Diagnostic Suite",
            "server_id": srv_id,
            "description": "Comprehensive diagnostic steps",
            "item_ids[]": [str(item1_id), str(item2_id), str(item3_id)],
        },
        follow_redirects=True,
    )
    assert grp_resp.status_code == 200

    with app.app_context():
        group = CommandGroup.query.filter_by(name="Server Diagnostic Suite").first()
        assert group is not None
        assert len(group.items) == 3
        assert [gi.command_item_id for gi in group.items] == [item1_id, item2_id, item3_id]
        assert [gi.sequence_order for gi in group.items] == [1, 2, 3]
        group_id = group.id

    # 3. Simulate Drag & Drop reordering in the group builder: [Item 3, Item 1, Item 2]
    edit_resp = client.post(
        f"/apps/remote-exec/groups/{group_id}/edit",
        data={
            "name": "Server Diagnostic Suite (Reordered)",
            "server_id": srv_id,
            "description": "Reordered sequence via drag and drop",
            "item_ids[]": [str(item3_id), str(item1_id), str(item2_id)],
        },
        follow_redirects=True,
    )
    assert edit_resp.status_code == 200

    with app.app_context():
        refreshed_group = db.session.get(CommandGroup, group_id)
        assert refreshed_group.name == "Server Diagnostic Suite (Reordered)"
        assert len(refreshed_group.items) == 3
        assert [gi.command_item_id for gi in refreshed_group.items] == [item3_id, item1_id, item2_id]
        assert [gi.sequence_order for gi in refreshed_group.items] == [1, 2, 3]


def test_duplicate_and_delete_items_and_groups(client, app, setup_lifecycle_data):
    """Verifies duplication and deletion of both Command Items and Command Groups."""
    data = setup_lifecycle_data
    admin_id = data["admin_id"]
    srv_id = data["server_id"]

    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)

    with app.app_context():
        item = CommandItem(
            name="Template Build Script",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="echo 'Building project'",
            created_by_id=admin_id,
        )
        db.session.add(item)
        db.session.commit()
        item_id = item.id

        group = CommandGroup(
            name="Deployment Pipeline",
            server_id=srv_id,
            created_by_id=admin_id,
        )
        db.session.add(group)
        db.session.flush()

        link = CommandGroupItem(group_id=group.id, command_item_id=item.id, sequence_order=1)
        db.session.add(link)
        db.session.commit()
        group_id = group.id

    # 1. Duplicate Command Item
    dup_item_resp = client.post(f"/apps/remote-exec/items/{item_id}/duplicate", follow_redirects=True)
    assert dup_item_resp.status_code == 200

    with app.app_context():
        cloned_item = CommandItem.query.filter_by(name="Template Build Script (Copy)").first()
        assert cloned_item is not None
        assert cloned_item.id != item_id
        assert cloned_item.command_content == "echo 'Building project'"
        cloned_item_id = cloned_item.id

    # 2. Duplicate Command Group
    dup_grp_resp = client.post(f"/apps/remote-exec/groups/{group_id}/duplicate", follow_redirects=True)
    assert dup_grp_resp.status_code == 200

    with app.app_context():
        cloned_group = CommandGroup.query.filter_by(name="Deployment Pipeline (Copy)").first()
        assert cloned_group is not None
        assert cloned_group.id != group_id
        assert len(cloned_group.items) == 1
        assert cloned_group.items[0].command_item_id == item_id
        cloned_group_id = cloned_group.id

    # 3. Delete original and cloned Command Groups
    del_grp1 = client.post(f"/apps/remote-exec/groups/{group_id}/delete", follow_redirects=True)
    assert del_grp1.status_code == 200
    del_grp2 = client.post(f"/apps/remote-exec/groups/{cloned_group_id}/delete", follow_redirects=True)
    assert del_grp2.status_code == 200

    with app.app_context():
        assert db.session.get(CommandGroup, group_id) is None
        assert db.session.get(CommandGroup, cloned_group_id) is None
        # Verify the underlying Command Items still exist (isolation)
        assert db.session.get(CommandItem, item_id) is not None

    # 4. Delete Command Items
    del_item1 = client.post(f"/apps/remote-exec/items/{item_id}/delete", follow_redirects=True)
    assert del_item1.status_code == 200
    del_item2 = client.post(f"/apps/remote-exec/items/{cloned_item_id}/delete", follow_redirects=True)
    assert del_item2.status_code == 200

    with app.app_context():
        assert db.session.get(CommandItem, item_id) is None
        assert db.session.get(CommandItem, cloned_item_id) is None


def test_run_rerun_and_step_status_indicators(client, app, setup_lifecycle_data):
    """Verifies running and rerunning a group, showing running spinner, green check for success, and red check for error."""
    data = setup_lifecycle_data
    admin_id = data["admin_id"]
    srv_id = data["server_id"]

    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)

    with app.app_context():
        # Item 1: Succeeds
        item_ok = CommandItem(
            name="Success Step",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="echo 'All good'",
            created_by_id=admin_id,
        )
        # Item 2: Fails
        item_err = CommandItem(
            name="Failure Step",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="exit 1",
            created_by_id=admin_id,
        )
        db.session.add_all([item_ok, item_err])
        db.session.flush()

        group = CommandGroup(
            name="Status Indicator Test Group",
            server_id=srv_id,
            run_in_parallel=False,
            stop_on_error=False,
            created_by_id=admin_id,
        )
        db.session.add(group)
        db.session.flush()

        db.session.add(CommandGroupItem(group_id=group.id, command_item_id=item_ok.id, sequence_order=1))
        db.session.add(CommandGroupItem(group_id=group.id, command_item_id=item_err.id, sequence_order=2))
        db.session.commit()

        group_id = group.id
        ok_id = item_ok.id
        err_id = item_err.id

    # Mock the sequential runner to simulate real step-by-step progress callback
    async def mock_runner(ip, port, username, password, private_key, group_name, items, stop_on_error, os_type, on_step_change):
        # Step 1: Success Step transitions to running, then completed (exit 0)
        if on_step_change:
            await on_step_change(0, items[0], "running", None, "")
            await on_step_change(0, items[0], "completed", 0, "Step 1 output: All good")

        # Step 2: Failure Step transitions to running, then failed (exit 1)
        if on_step_change:
            await on_step_change(1, items[1], "running", None, "")
            await on_step_change(1, items[1], "failed", 1, "Step 2 output: command failed with code 1")

        return 1, "Group execution finished with errors", None

    # 1. Run Command Group
    with patch("mizalla.remote_exec_client.execute_sequential_command_group", side_effect=mock_runner):
        run_resp = client.post(
            f"/apps/remote-exec/run/group/{group_id}",
        )
        assert run_resp.status_code == 200
        run_data = run_resp.get_json()
        assert run_data["success"] is True
        audit_id = run_data["audit_id"]

        # Wait briefly for thread to finish
        import time
        from mizalla.remote_exec_client import _active_threads, _threads_lock
        th = None
        with _threads_lock:
            th = _active_threads.get(run_data["task_id"])
        if th:
            th.join(timeout=5)

    # 2. Check Step Status & Outputs
    with app.app_context():
        audit = db.session.get(CommandAudit, audit_id)
        assert audit is not None
        assert audit.status == COMMAND_STATUS_FAILED
        assert audit.exit_code == 1

        steps = audit.steps_data
        assert len(steps) == 2

        # Step 1: Green check (completed, exit_code 0)
        assert steps[0]["name"] == "Success Step"
        assert steps[0]["status"] == "completed"
        assert steps[0]["exit_code"] == 0
        assert "All good" in steps[0]["output"]

        # Step 2: Red check/cross (failed, exit_code 1)
        assert steps[1]["name"] == "Failure Step"
        assert steps[1]["status"] == "failed"
        assert steps[1]["exit_code"] == 1
        assert "code 1" in steps[1]["output"]

    # 3. View logs endpoint (last-10)
    log_resp = client.get(f"/apps/remote-exec/commands/{audit_id}/last-10")
    assert log_resp.status_code == 200
    log_data = log_resp.get_json()
    assert log_data["success"] is True
    assert "Group execution finished" in log_data["last_10"]

    # 4. Rerun Command Group
    with patch("mizalla.remote_exec_client.execute_sequential_command_group", side_effect=mock_runner):
        rerun_resp = client.post(
            f"/apps/remote-exec/run/group/{group_id}",
        )
        assert rerun_resp.status_code == 200
        rerun_data = rerun_resp.get_json()
        assert rerun_data["success"] is True
        rerun_audit_id = rerun_data["audit_id"]
        assert rerun_audit_id != audit_id  # New distinct audit trail created for rerun!

        th2 = None
        with _threads_lock:
            th2 = _active_threads.get(rerun_data["task_id"])
        if th2:
            th2.join(timeout=5)

    with app.app_context():
        rerun_audit = db.session.get(CommandAudit, rerun_audit_id)
        assert rerun_audit.status == COMMAND_STATUS_FAILED
        rerun_steps = rerun_audit.steps_data
        assert rerun_steps[0]["status"] == "completed" and rerun_steps[0]["exit_code"] == 0
        assert rerun_steps[1]["status"] == "failed" and rerun_steps[1]["exit_code"] == 1


def test_user_permissions_and_access_control(client, app, setup_lifecycle_data):
    """Verifies that unprivileged logged-in users are blocked from unauthorized actions and only permitted when granted access."""
    data = setup_lifecycle_data
    admin_id = data["admin_id"]
    user_id = data["user_id"]
    srv_id = data["server_id"]

    with app.app_context():
        admin_item = CommandItem(
            name="Admin Protected Script",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="whoami",
            created_by_id=admin_id,
        )
        db.session.add(admin_item)
        db.session.flush()

        admin_group = CommandGroup(
            name="Admin Protected Group",
            server_id=srv_id,
            created_by_id=admin_id,
        )
        db.session.add(admin_group)
        db.session.flush()

        db.session.add(CommandGroupItem(group_id=admin_group.id, command_item_id=admin_item.id, sequence_order=1))
        db.session.commit()

        item_id = admin_item.id
        group_id = admin_group.id

    # Switch session to standard unprivileged user
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)

    with app.app_context():
        user = db.session.get(Member, user_id)
        item = db.session.get(CommandItem, item_id)
        group = db.session.get(CommandGroup, group_id)

        # Baseline: Unprivileged user cannot add, edit, duplicate, delete, or run
        assert user_can_add_item(user) is False
        assert user_can_add_group(user) is False
        assert user_can_edit_item(user, item) is False
        assert user_can_duplicate_item(user, item) is False
        assert user_can_delete_item(user, item) is False
        assert user_can_edit_group(user, group) is False
        assert user_can_duplicate_group(user, group) is False
        assert user_can_delete_group(user, group) is False
        assert user_can_run_group(user, group) is False

    # 1. Attempting unauthorized create item
    unauth_item_create = client.post(
        "/apps/remote-exec/items/save",
        data={"ajax": "1", "name": "Illegal Item", "command_type": "remote", "command_content": "ls"},
    )
    assert unauth_item_create.status_code == 403

    # 2. Attempting unauthorized create group
    unauth_grp_create = client.post(
        "/apps/remote-exec/groups/new",
        data={"name": "Illegal Group", "server_id": srv_id},
        follow_redirects=False,
    )
    assert unauth_grp_create.status_code == 302  # Redirected with flash error

    # 3. Attempting unauthorized duplicate item & group
    unauth_dup_item = client.post(f"/apps/remote-exec/items/{item_id}/duplicate", follow_redirects=True)
    assert unauth_dup_item.status_code == 200
    with app.app_context():
        assert CommandItem.query.filter_by(name="Admin Protected Script (Copy)").first() is None

    unauth_dup_grp = client.post(f"/apps/remote-exec/groups/{group_id}/duplicate", follow_redirects=True)
    assert unauth_dup_grp.status_code == 200
    with app.app_context():
        assert CommandGroup.query.filter_by(name="Admin Protected Group (Copy)").first() is None

    # 4. Attempting unauthorized delete item & group
    unauth_del_item = client.post(f"/apps/remote-exec/items/{item_id}/delete", follow_redirects=True)
    assert unauth_del_item.status_code == 200
    with app.app_context():
        assert db.session.get(CommandItem, item_id) is not None

    unauth_del_grp = client.post(f"/apps/remote-exec/groups/{group_id}/delete", follow_redirects=True)
    assert unauth_del_grp.status_code == 200
    with app.app_context():
        assert db.session.get(CommandGroup, group_id) is not None

    # 5. Attempting unauthorized run
    unauth_run = client.post(f"/apps/remote-exec/run/group/{group_id}")
    assert unauth_run.status_code == 403

    # 6. Now Admin grants permissions to standard user:
    with app.app_context():
        user = db.session.get(Member, user_id)
        user.can_add_command_items = True
        user.can_add_command_groups = True

        # Grant group permissions: can_run=True, can_duplicate=True
        g_perm = CommandGroupPermission(
            group_id=group_id,
            user_id=user_id,
            can_run=True,
            can_duplicate=True,
            can_edit=False,
            can_view_logs=True,
        )
        # Grant item permission: can_duplicate=True
        i_perm = CommandItemPermission(
            command_item_id=item_id,
            user_id=user_id,
            can_run=True,
            can_duplicate=True,
            can_edit=False,
        )
        db.session.add_all([g_perm, i_perm])
        db.session.commit()

        # Check permissions updated
        assert user_can_add_item(user) is True
        assert user_can_add_group(user) is True
        assert user_can_duplicate_item(user, item) is True
        assert user_can_duplicate_group(user, group) is True
        assert user_can_run_group(user, group) is True
        assert user_can_view_group_logs(user, group) is True
        # Edit and delete remain disallowed
        assert user_can_edit_group(user, group) is False
        assert user_can_delete_group(user, group) is False

    # 7. Standard user can now create items, duplicate, and run
    ok_create = client.post(
        "/apps/remote-exec/items/save",
        data={"ajax": "1", "name": "User Allowed Item", "command_type": "remote", "command_content": "echo 'ok'"},
    )
    assert ok_create.status_code == 200

    ok_dup_item = client.post(f"/apps/remote-exec/items/{item_id}/duplicate", follow_redirects=True)
    assert ok_dup_item.status_code == 200
    with app.app_context():
        assert CommandItem.query.filter_by(name="Admin Protected Script (Copy)").first() is not None

    ok_dup_grp = client.post(f"/apps/remote-exec/groups/{group_id}/duplicate", follow_redirects=True)
    assert ok_dup_grp.status_code == 200
    with app.app_context():
        assert CommandGroup.query.filter_by(name="Admin Protected Group (Copy)").first() is not None


def test_upload_files_duplication_omits_files(client, setup_lifecycle_data, app):
    """Duplicating an Upload Files Command Item should NOT copy uploaded files and reset command_content."""
    admin_id = setup_lifecycle_data["admin_id"]
    server_id = setup_lifecycle_data["server_id"]

    with app.app_context():
        item = CommandItem(
            name="Upload Config Artifacts",
            command_type=COMMAND_TYPE_UPLOAD,
            command_content="sample1.conf\nsample2.conf",
            created_by_id=admin_id,
            server_id=server_id,
        )
        db.session.add(item)
        db.session.commit()
        item_id = item.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)

    # Duplicate the upload item
    resp = client.post(f"/apps/remote-exec/items/{item_id}/duplicate", follow_redirects=True)
    assert resp.status_code == 200

    with app.app_context():
        duplicated = CommandItem.query.filter_by(name="Upload Config Artifacts (Copy)").first()
        assert duplicated is not None
        assert duplicated.command_type == COMMAND_TYPE_UPLOAD
        # Confirms files were NOT copied over and content is empty placeholder
        assert duplicated.command_content == "(No files uploaded)"


def test_run_modal_data_grouping_and_type_validation(client, setup_lifecycle_data, app):
    """Test run-modal-data grouping per Command Item, distinct variables, and type validation on run."""
    admin_id = setup_lifecycle_data["admin_id"]
    server_id = setup_lifecycle_data["server_id"]

    with app.app_context():
        from mizalla.models import CommandVariable, VAR_TYPE_NUMBER, VAR_TYPE_BOOLEAN, VAR_TYPE_TEXT

        # Item 1 has PORT (number) and SHARED_ENV (text)
        it1 = CommandItem(
            name="Step 1 - Port Config",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="echo {{ PORT }} {{ SHARED_ENV }}",
            created_by_id=admin_id,
            server_id=server_id,
        )
        db.session.add(it1)
        db.session.flush()

        v1 = CommandVariable(command_item_id=it1.id, name="PORT", var_type=VAR_TYPE_NUMBER, default_value="8080")
        v2 = CommandVariable(command_item_id=it1.id, name="SHARED_ENV", var_type=VAR_TYPE_TEXT, default_value="production")
        db.session.add_all([v1, v2])

        # Item 2 has SHARED_ENV (re-declared text) and IS_DEBUG (boolean)
        it2 = CommandItem(
            name="Step 2 - Debug Check",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="echo {{ SHARED_ENV }} {{ IS_DEBUG }}",
            created_by_id=admin_id,
            server_id=server_id,
        )
        db.session.add(it2)
        db.session.flush()

        v3 = CommandVariable(command_item_id=it2.id, name="SHARED_ENV", var_type=VAR_TYPE_TEXT, default_value="production")
        v4 = CommandVariable(command_item_id=it2.id, name="IS_DEBUG", var_type=VAR_TYPE_BOOLEAN, default_value="false")
        db.session.add_all([v3, v4])

        # Group containing both
        group = CommandGroup(
            name="Multi-step Variable Group",
            server_id=server_id,
            created_by_id=admin_id,
        )
        db.session.add(group)
        db.session.flush()

        gi1 = CommandGroupItem(group_id=group.id, command_item_id=it1.id, sequence_order=1)
        gi2 = CommandGroupItem(group_id=group.id, command_item_id=it2.id, sequence_order=2)
        db.session.add_all([gi1, gi2])
        db.session.commit()
        group_id = group.id

    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin_id)

    # 1. Fetch run modal data
    resp = client.get(f"/apps/remote-exec/run-modal/group/{group_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    # Distinct variable count: PORT, SHARED_ENV, IS_DEBUG -> 3
    assert data["total_variables"] == 3
    assert len(data["items"]) == 2
    assert data["items"][0]["name"] == "Step 1 - Port Config"
    assert len(data["items"][0]["variables"]) == 2
    assert data["items"][1]["name"] == "Step 2 - Debug Check"
    assert len(data["items"][1]["variables"]) == 2

    # 2. Test type validation: Passing invalid string for numeric variable PORT fails with 400
    with patch("mizalla.remote_exec.dispatch_command_group") as mock_dispatch:
        bad_run = client.post(
            f"/apps/remote-exec/run/group/{group_id}",
            data={
                "var_PORT": "not_a_number",
                "var_SHARED_ENV": "staging",
                "var_IS_DEBUG": "true",
            },
        )
        assert bad_run.status_code == 400
        assert "requires a numeric value" in bad_run.get_json()["error"]
        mock_dispatch.assert_not_called()

    # 3. Test type validation: Passing valid numeric PORT succeeds and normalizes boolean
    with patch("mizalla.remote_exec.dispatch_command_group") as mock_dispatch:
        mock_audit = CommandAudit(
            task_id="test_mock_task",
            user_id=admin_id,
            server_id=server_id,
            group_id=group_id,
            entry_type="group",
            title="Multi-step Variable Group",
            command="echo",
            executed_command="echo 9000 staging\necho staging true",
            status=COMMAND_STATUS_PENDING,
        )
        mock_audit.id = 9999
        mock_dispatch.return_value = mock_audit

        good_run = client.post(
            f"/apps/remote-exec/run/group/{group_id}",
            data={
                "var_PORT": "9000",
                "var_SHARED_ENV": "staging",
                "var_IS_DEBUG": "on",
            },
        )
        assert good_run.status_code == 200
        assert good_run.get_json()["success"] is True
        mock_dispatch.assert_called_once()
        # Verify IS_DEBUG was normalized to "true"
        passed_vars = mock_dispatch.call_args[1]["variables"]
        assert passed_vars["PORT"] == "9000"
        assert passed_vars["IS_DEBUG"] == "true"

