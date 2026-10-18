"""Tests for Pure Flask + AsyncSSH Agentless Remote Command Orchestration."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mizalla.asyncssh_engine import (
    extract_variable_names,
    normalize_line_endings,
    substitute_variables,
)
from mizalla.crypto_vault import decrypt_secret, encrypt_secret
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    COMMAND_STATUS_COMPLETED,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PENDING,
    COMMAND_STATUS_RUNNING,
    COMMAND_STATUS_TERMINATED,
    COMMAND_TYPE_LOCAL,
    COMMAND_TYPE_REMOTE,
    COMMAND_TYPE_UPLOAD,
    FEATURE_REMOTE_EXEC,
    VAR_TYPE_BOOLEAN,
    VAR_TYPE_NUMBER,
    VAR_TYPE_TEXT,
    CommandAudit,
    CommandGroup,
    CommandGroupItem,
    CommandGroupPermission,
    CommandItem,
    CommandItemPermission,
    CommandVariable,
    Member,
    Server,
    ServerAccess,
    ServerCredential,
)
from mizalla.remote_exec_client import (
    get_connectable_servers_for_user,
    get_effective_credential,
    user_can_add_group,
    user_can_add_item,
    user_can_delete_group,
    user_can_delete_item,
    user_can_duplicate_group,
    user_can_duplicate_item,
    user_can_edit_group,
    user_can_edit_item,
    user_can_edit_server,
    user_can_run_group,
    user_can_run_item,
    user_can_view_group,
    user_can_view_group_logs,
    user_can_view_item,
    user_can_view_item_logs,
    user_can_view_server,
)

PASSWORD = "test-password-123"


def make_member(name="Test User", email="user@example.com", is_admin=False, **kwargs):
    kwargs.setdefault("can_add_command_items", True)
    kwargs.setdefault("can_add_command_groups", True)
    member = Member(name=name, email=email, is_admin=is_admin, **kwargs)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def make_server(name="Production Node 1", ip="192.168.1.50", os_type="linux", os_version="Ubuntu 22.04 LTS", connected_user=None, with_cred=False):
    server = Server(name=name, ip=ip, os_type=os_type, os_version=os_version, status="available", last_checked=datetime.now(timezone.utc))
    db.session.add(server)
    db.session.flush()
    if with_cred:
        cred = ServerCredential(server_id=server.id, username="root", is_default=True)
        cred.password = "root_pass"
        db.session.add(cred)
    if connected_user:
        sa = ServerAccess(
            server_id=server.id,
            member_id=connected_user.id,
            can_view=True,
            connection_status="available",
            last_connected_at=datetime.now(timezone.utc),
        )
        db.session.add(sa)
    db.session.commit()
    return server


def login(client, email="user@example.com"):
    return client.post("/apps/login", data={"email": email, "password": PASSWORD})


# ─── 1. Encryption & Crypto Vault Tests ──────────────────────────────────────

def test_fernet_credential_encryption_and_decryption(app):
    """Test passwords and private keys are encrypted at rest with Fernet."""
    with app.app_context():
        secret_pass = "SuperSecret_P@ssw0rd!_2026"
        secret_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest_mock_key\n-----END OPENSSH PRIVATE KEY-----"

        enc_pass = encrypt_secret(secret_pass)
        enc_key = encrypt_secret(secret_key)

        assert enc_pass != secret_pass
        assert enc_key != secret_key
        assert decrypt_secret(enc_pass) == secret_pass
        assert decrypt_secret(enc_key) == secret_key


def test_server_credential_model_property_encryption(app):
    """Verify ServerCredential model automatically handles encryption via getter/setter."""
    with app.app_context():
        s = make_server()
        u = make_member()
        cred = ServerCredential(server_id=s.id, user_id=u.id, username="admin_ssh")
        cred.password = "PlainPassword99"
        cred.private_key = "PrivateKeyData"
        db.session.add(cred)
        db.session.commit()

        # Check raw stored value in database is encrypted
        raw = db.session.get(ServerCredential, cred.id)
        assert raw.encrypted_password != "PlainPassword99"
        assert raw.encrypted_private_key != "PrivateKeyData"

        # Check model properties decrypt back to plaintext
        assert raw.password == "PlainPassword99"
        assert raw.private_key == "PrivateKeyData"


# ─── 2. Engine Helpers: Variables & Line Endings ─────────────────────────────

def test_variable_extraction_and_set_deduplication():
    """Verify {{ Variable }} placeholders are extracted into a deduplicated Set."""
    cmd = "cd {{ Working-Dir }}; ./run.sh --port={{ Port }} --dir={{ Working-Dir }}"
    tokens = extract_variable_names(cmd)
    assert isinstance(tokens, set)
    assert tokens == {"Working-Dir", "Port"}


def test_variable_substitution():
    """Verify variables are injected cleanly into the command text."""
    cmd = "echo 'Host: {{ Host }} on port {{ Port }} with flag {{ Debug }}'"
    result = substitute_variables(cmd, {
        "Host": "api.qnl.qa",
        "Port": 443,
        "Debug": True,
    })
    assert result == "echo 'Host: api.qnl.qa on port 443 with flag true'"


def test_cross_platform_line_ending_normalization():
    """Verify line endings are normalized for Linux (LF) and Windows (CRLF)."""
    mixed = "line1\r\nline2\nline3\rline4"
    linux_res = normalize_line_endings(mixed, os_type="linux")
    assert "\r\n" not in linux_res
    assert "\r" not in linux_res
    assert linux_res == "line1\nline2\nline3\nline4"

    win_res = normalize_line_endings(mixed, os_type="windows")
    assert win_res == "line1\r\nline2\r\nline3\r\nline4"


# ─── 3. Access Control & Dashboard ───────────────────────────────────────────

def test_remote_exec_access_control(app, client):
    """Ungranted members receive 403; granted members access dashboard."""
    with app.app_context():
        make_member()

    login(client)
    resp = client.get("/apps/remote-exec/")
    assert resp.status_code == 403

    # Grant access
    with app.app_context():
        u = Member.query.filter_by(email="user@example.com").first()
        sync_member_features(u, [FEATURE_REMOTE_EXEC])
        db.session.commit()

    resp = client.get("/apps/remote-exec/")
    assert resp.status_code == 200
    assert "Remote Command Orchestration" in resp.get_data(as_text=True)


def test_effective_credential_admin_default_fallback(app):
    """Users without personal credentials automatically fallback to Admin default credentials."""
    with app.app_context():
        s = make_server("Target Server", "10.0.0.1")
        admin = make_member("Admin", "admin@example.com", is_admin=True)
        user = make_member("Junior", "junior@example.com")

        # Admin creates default server credential
        default_cred = ServerCredential(
            server_id=s.id,
            user_id=None,
            is_default=True,
            username="ops_admin",
        )
        default_cred.password = "OpsPassword123"
        db.session.add(default_cred)
        db.session.commit()

        # Effective credential for junior user (who has no personal credential) resolves to default
        resolved = get_effective_credential(s.id, user.id)
        assert resolved is not None
        assert resolved.username == "ops_admin"
        assert resolved.password == "OpsPassword123"


# ─── 4. Command Items & Duplication ──────────────────────────────────────────

def test_command_item_creation_and_duplication(app, client):
    """Test Command Item creation and DUPLICATE flow."""
    with app.app_context():
        u = make_member("Developer", "dev@example.com")
        sync_member_features(u, [FEATURE_REMOTE_EXEC])
        s = make_server(connected_user=u, with_cred=True)
        s_id = s.id
        u_id = u.id

    login(client, "dev@example.com")

    # Create Item
    resp = client.post("/apps/remote-exec/items/save", data={
        "name": "Restart Nginx",
        "server_id": str(s_id),
        "command_type": "remote",
        "command_content": "systemctl restart {{ ServiceName }}",
        "var_name[]": ["ServiceName"],
        "var_desc[]": ["Service systemd unit"],
        "var_type[]": ["text"],
        "var_default[]": ["nginx"],
    })
    assert resp.status_code == 302

    with app.app_context():
        item = CommandItem.query.filter_by(name="Restart Nginx").first()
        assert item is not None
        assert item.server_id is None
        assert len(item.variables) == 1
        assert item.variables[0].name == "ServiceName"
        item_id = item.id

    # Duplicate Item
    dup_resp = client.post(f"/apps/remote-exec/items/{item_id}/duplicate")
    assert dup_resp.status_code == 302

    with app.app_context():
        copies = CommandItem.query.filter_by(name="Restart Nginx (Copy)").all()
        assert len(copies) == 1
        copy_item = copies[0]
        assert copy_item.created_by_id == u_id
        assert copy_item.server_id is None
        assert len(copy_item.variables) == 1
        assert copy_item.variables[0].default_value == "nginx"


# ─── 5. Command Groups & Orchestration Sequencing ────────────────────────────

def test_command_group_creation_and_duplication_with_subitems(app, client):
    """Test Command Group creation, sub-items ordering, and deep DUPLICATE."""
    with app.app_context():
        u = make_member("Lead", "lead@example.com")
        sync_member_features(u, [FEATURE_REMOTE_EXEC])
        s = make_server(connected_user=u, with_cred=True)
        s_id = s.id
        u_id = u.id

        # Seed two command items
        i1 = CommandItem(server_id=s.id, name="Step 1 - Pull", command_type="remote", command_content="git pull", created_by_id=u.id)
        i2 = CommandItem(server_id=s.id, name="Step 2 - Build", command_type="remote", command_content="make build", created_by_id=u.id)
        db.session.add_all([i1, i2])
        db.session.commit()
        i1_id, i2_id = i1.id, i2.id

    login(client, "lead@example.com")

    # Create Group
    resp = client.post("/apps/remote-exec/groups/new", data={
        "name": "Deployment Workflow",
        "server_id": str(s_id),
        "description": "Continuous deploy sequence",
        "stop_on_error": "1",
    })
    assert resp.status_code == 302

    with app.app_context():
        grp = CommandGroup.query.filter_by(name="Deployment Workflow").first()
        assert grp is not None
        grp_id = grp.id

    # Add items to group
    client.post(f"/apps/remote-exec/groups/{grp_id}/add-item", data={"item_id": str(i1_id)})
    client.post(f"/apps/remote-exec/groups/{grp_id}/add-item", data={"item_id": str(i2_id)})

    with app.app_context():
        grp = db.session.get(CommandGroup, grp_id)
        assert len(grp.items) == 2
        assert grp.items[0].command_item.name == "Step 1 - Pull"
        assert grp.items[1].command_item.name == "Step 2 - Build"

    # Deep Duplicate Group
    dup_resp = client.post(f"/apps/remote-exec/groups/{grp_id}/duplicate")
    assert dup_resp.status_code == 302

    with app.app_context():
        dup_grp = CommandGroup.query.filter_by(name="Deployment Workflow (Copy)").first()
        assert dup_grp is not None
        assert dup_grp.created_by_id == u_id
        assert len(dup_grp.items) == 2
        # Verify sub-items link the same modular Command Items
        assert dup_grp.items[0].command_item.name == "Step 1 - Pull"
        assert dup_grp.items[1].command_item.name == "Step 2 - Build"


# ─── 6. Pre-flight Variable Modal (Set-based deduplication) ──────────────────

def test_run_modal_set_deduplication(app, client):
    """Verify pre-flight variable modal prompts for shared variable names only once."""
    with app.app_context():
        u = make_member("Admin", "admin_modal@example.com", is_admin=True)
        sync_member_features(u, [FEATURE_REMOTE_EXEC])
        s = make_server()

        # Two items using the SAME variable token: {{ Target-Path }}
        i1 = CommandItem(server_id=s.id, name="Check Dir", command_type="remote", command_content="ls -la {{ Target-Path }}", created_by_id=u.id)
        i2 = CommandItem(server_id=s.id, name="Clean Dir", command_type="remote", command_content="rm -rf {{ Target-Path }}/*.tmp", created_by_id=u.id)
        grp = CommandGroup(server_id=s.id, name="Clean Workflow", created_by_id=u.id)
        db.session.add_all([i1, i2, grp])
        db.session.flush()

        db.session.add(CommandGroupItem(group_id=grp.id, command_item_id=i1.id, sequence_order=0))
        db.session.add(CommandGroupItem(group_id=grp.id, command_item_id=i2.id, sequence_order=1))
        db.session.commit()
        grp_id = grp.id

    login(client, "admin_modal@example.com")
    resp = client.get(f"/apps/remote-exec/run-modal/group/{grp_id}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    # The variable {{ Target-Path }} must appear only ONCE in the returned list
    var_names = [v["name"] for v in data["variables"]]
    assert len(var_names) == 1
    assert var_names[0] == "Target-Path"


# ─── 7. Security: Can View Logs Permission Enforcement ───────────────────────

def test_security_can_view_logs_restriction(app, client):
    """Users with can_run but WITHOUT can_view_logs are blocked from seeing stdout/stderr."""
    with app.app_context():
        admin = make_member("Admin", "admin_sec@example.com", is_admin=True)
        operator = make_member("Operator", "operator@example.com", is_admin=False)
        sync_member_features(operator, [FEATURE_REMOTE_EXEC])
        s = make_server()

        grp = CommandGroup(server_id=s.id, name="Sensitive DB Dump", created_by_id=admin.id)
        db.session.add(grp)
        db.session.flush()

        # Grant operator can_run=True, but can_view_logs=False!
        perm = CommandGroupPermission(group_id=grp.id, user_id=operator.id, can_run=True, can_view_logs=False)
        db.session.add(perm)

        # Audit with sensitive output
        audit = CommandAudit(
            task_id="sec_task_1",
            user_id=operator.id,
            server_id=s.id,
            group_id=grp.id,
            entry_type="group",
            title="Sensitive DB Dump",
            command="pg_dump db",
            status=COMMAND_STATUS_COMPLETED,
            exit_code=0,
            full_output="DB_PASSWORD=SuperSecretAdminPassword123\nDUMP COMPLETED",
        )
        db.session.add(audit)
        db.session.commit()
        audit_id = audit.id

    login(client, "operator@example.com")

    # 1. Attempt to lazy-load last 10 lines -> Must receive 403 Forbidden
    resp_lazy = client.get(f"/apps/remote-exec/commands/{audit_id}/last-10")
    assert resp_lazy.status_code == 403
    assert resp_lazy.get_json()["restricted"] is True

    # 2. Attempt to view full log page -> Redirects with flash error
    resp_full = client.get(f"/apps/remote-exec/commands/{audit_id}/full-log")
    assert resp_full.status_code == 302


def test_security_can_view_logs_allowed(app, client):
    """Users granted can_view_logs can read stdout/stderr."""
    with app.app_context():
        admin = make_member("Admin", "admin_sec2@example.com", is_admin=True)
        auditor = make_member("Auditor", "auditor@example.com", is_admin=False)
        sync_member_features(auditor, [FEATURE_REMOTE_EXEC])
        s = make_server()

        grp = CommandGroup(server_id=s.id, name="Public Logs", created_by_id=admin.id)
        db.session.add(grp)
        db.session.flush()

        # Grant can_view_logs=True
        perm = CommandGroupPermission(group_id=grp.id, user_id=auditor.id, can_run=True, can_view_logs=True)
        db.session.add(perm)

        audit = CommandAudit(
            task_id="sec_task_2",
            user_id=auditor.id,
            server_id=s.id,
            group_id=grp.id,
            entry_type="group",
            title="Public Logs",
            command="uptime",
            status=COMMAND_STATUS_COMPLETED,
            exit_code=0,
            full_output="load average: 0.05, 0.03, 0.00",
        )
        db.session.add(audit)
        db.session.commit()
        audit_id = audit.id

    login(client, "auditor@example.com")

    resp = client.get(f"/apps/remote-exec/commands/{audit_id}/last-10")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "load average" in data["last_10"]


# ─── 8. Active Status Polling (Spinner & RUNNING detection) ──────────────────

def test_active_status_polling_identifies_running_jobs(app, client):
    """Verify /apps/remote-exec/active-status reports running items/groups for UI spinners."""
    with app.app_context():
        u = make_member("Admin", "admin_poll@example.com", is_admin=True)
        sync_member_features(u, [FEATURE_REMOTE_EXEC])
        s = make_server()

        i = CommandItem(server_id=s.id, name="Long Job", command_content="sleep 60", created_by_id=u.id)
        db.session.add(i)
        db.session.flush()

        audit = CommandAudit(
            task_id="poll_task_1",
            user_id=u.id,
            server_id=s.id,
            command_item_id=i.id,
            entry_type="item",
            title="Long Job",
            command="sleep 60",
            status=COMMAND_STATUS_RUNNING,
        )
        db.session.add(audit)
        db.session.commit()
        item_id = i.id

    login(client, "admin_poll@example.com")
    resp = client.get("/apps/remote-exec/active-status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["has_running"] is True
    assert item_id in data["running_item_ids"]


# ─── 9. Single Session Sequential Group Execution Simulation ─────────────────

def test_sequential_single_session_group_execution(app):
    """Simulate sequential group execution piping output and preserving context."""
    from mizalla.asyncssh_engine import execute_sequential_command_group

    items = [
        {"name": "Step 1", "command_type": "local", "command": "echo 'Hello from Step 1'"},
        {"name": "Step 2", "command_type": "local", "command": "echo 'Hello from Step 2'"},
    ]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        exit_code, output, _ = loop.run_until_complete(
            execute_sequential_command_group(
                ip="127.0.0.1",
                port=22,
                username="fake",
                password="",
                private_key="",
                group_name="Test Workflow",
                items=items,
            )
        )
    finally:
        loop.close()

    assert exit_code == 0
    assert "[Test Workflow] [Step 1]" in output and "Hello from Step 1" in output
    assert "[Test Workflow] [Step 2]" in output and "Hello from Step 2" in output


def test_spinner_and_running_visible_to_can_run_users(app, client):
    """Verify the SPINNER and the word 'RUNNING' are visible to all users with can_run permissions."""
    with app.app_context():
        admin = make_member("Admin", "admin_run_view@example.com", is_admin=True)
        runner = make_member("Runner", "runner_view@example.com", is_admin=False)
        sync_member_features(runner, [FEATURE_REMOTE_EXEC])
        s = make_server()

        # Group with runner having can_run=True
        grp = CommandGroup(server_id=s.id, name="Nightly Sync", created_by_id=admin.id)
        db.session.add(grp)
        db.session.flush()

        perm_grp = CommandGroupPermission(group_id=grp.id, user_id=runner.id, can_run=True)
        db.session.add(perm_grp)

        # Item with runner having can_run=True
        item = CommandItem(server_id=s.id, name="Backup DB", command_type="remote", command_content="pg_dump", created_by_id=admin.id)
        db.session.add(item)
        db.session.flush()

        perm_item = CommandItemPermission(command_item_id=item.id, user_id=runner.id, can_run=True)
        db.session.add(perm_item)

        # Set audits in RUNNING status
        audit_grp = CommandAudit(
            task_id="run_task_grp",
            user_id=admin.id,
            server_id=s.id,
            group_id=grp.id,
            entry_type="group",
            title="Nightly Sync",
            command="sync.sh",
            status=COMMAND_STATUS_RUNNING,
        )
        audit_item = CommandAudit(
            task_id="run_task_item",
            user_id=admin.id,
            server_id=s.id,
            command_item_id=item.id,
            entry_type="item",
            title="Backup DB",
            command="pg_dump",
            status=COMMAND_STATUS_RUNNING,
        )
        db.session.add_all([audit_grp, audit_item])
        db.session.commit()
        grp_id = grp.id
        item_id = item.id

    login(client, "runner_view@example.com")

    # 1. API endpoint /apps/remote-exec/active-status reports running group & item to can_run user
    resp = client.get("/apps/remote-exec/active-status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert grp_id in data["running_group_ids"]
    assert item_id in data["running_item_ids"]

    # 2. Main dashboard renders RUNNING badge with status-spinner and disabled buttons for both
    dash_resp = client.get("/apps/remote-exec/")
    assert dash_resp.status_code == 200
    html = dash_resp.get_data(as_text=True)

    # Spinner and RUNNING badge rendered as inline-flex (visible) for running group
    assert f'id="group-running-{grp_id}" style="display:inline-flex;' in html
    assert "RUNNING" in html
    assert "status-spinner" in html


# ─── 10. Command Group Accordion Look & Nested Items Tests ───────────────────

def test_command_group_accordion_listing_and_history_look(app, client):
    """Verify Command Group displays as Accordion Head with Command Items as nested accordions,
    showing SPINNER + RUNNING on running items, green tick on completed, red cross on failed,
    and disabled styling on pending items."""
    import json

    with app.app_context():
        admin = make_member("Admin", "admin_grp_ui@example.com", is_admin=True)
        sync_member_features(admin, [FEATURE_REMOTE_EXEC])
        s = make_server()

        # Create Group & Items
        grp = CommandGroup(server_id=s.id, name="Deploy Pipeline", run_in_parallel=False, created_by_id=admin.id)
        db.session.add(grp)
        db.session.flush()

        it1 = CommandItem(server_id=s.id, name="Git Pull", command_type="remote", command_content="git pull", created_by_id=admin.id)
        it2 = CommandItem(server_id=s.id, name="Build Assets", command_type="local", command_content="npm run build", created_by_id=admin.id)
        it3 = CommandItem(server_id=s.id, name="Restart Service", command_type="remote", command_content="systemctl restart app", created_by_id=admin.id)
        db.session.add_all([it1, it2, it3])
        db.session.flush()

        g_item1 = CommandGroupItem(group_id=grp.id, command_item_id=it1.id, sequence_order=0)
        g_item2 = CommandGroupItem(group_id=grp.id, command_item_id=it2.id, sequence_order=1)
        g_item3 = CommandGroupItem(group_id=grp.id, command_item_id=it3.id, sequence_order=2)
        db.session.add_all([g_item1, g_item2, g_item3])

        # Step results: it1 completed (tick), it2 failed (cross), it3 pending (disabled)
        steps_payload = [
            {"item_id": it1.id, "name": "Git Pull", "command_type": "remote", "status": "completed", "exit_code": 0, "output": "Already up to date."},
            {"item_id": it2.id, "name": "Build Assets", "command_type": "local", "status": "failed", "exit_code": 1, "output": "Build error in index.js"},
            {"item_id": it3.id, "name": "Restart Service", "command_type": "remote", "status": "pending", "exit_code": None, "output": ""},
        ]

        audit = CommandAudit(
            task_id="grp_ui_audit_1",
            user_id=admin.id,
            server_id=s.id,
            group_id=grp.id,
            entry_type="group",
            title="Deploy Pipeline",
            command="git pull && npm run build",
            status=COMMAND_STATUS_FAILED,
            exit_code=1,
            step_results=json.dumps(steps_payload),
            full_output="--- Step 1: Git Pull ---\nAlready up to date.\n--- Step 2: Build Assets ---\nBuild error in index.js",
        )
        db.session.add(audit)
        db.session.commit()
        audit_id = audit.id
        grp_id = grp.id
        it1_id, it2_id, it3_id = it1.id, it2.id, it3.id

    login(client, "admin_grp_ui@example.com")

    # 1. Verify dashboard rendering
    resp = client.get("/apps/remote-exec/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Accordion head with group name in listing
    assert f'id="group-card-{grp_id}"' in html
    assert "Deploy Pipeline" in html
    assert f'toggleGroupAccordion({grp_id})' in html

    # Nested item accordions in listing
    assert f'id="group-{grp_id}-item-{it1_id}"' in html
    assert f'id="group-{grp_id}-item-{it2_id}"' in html
    assert f'id="group-{grp_id}-item-{it3_id}"' in html

    # History audit rendering: Command Items nested accordions
    assert f'id="audit-{audit_id}-step-{it1_id}"' in html
    assert f'id="audit-{audit_id}-step-{it2_id}"' in html
    assert f'id="audit-{audit_id}-step-{it3_id}"' in html

    # Item 1 completed success shows green tick icon
    assert 'data-lucide="check"' in html
    # Item 2 failed shows red cross icon
    assert 'data-lucide="x"' in html
    # Item 3 not started shows disabled pending
    assert "(Disabled - Pending)" in html


def test_command_group_full_log_accordion_look(app, client):
    """Verify the Full Log view for a Command Group renders the unified accordion head and nested items."""
    import json

    with app.app_context():
        admin = make_member("Admin", "admin_log_look@example.com", is_admin=True)
        sync_member_features(admin, [FEATURE_REMOTE_EXEC])
        s = make_server()

        grp = CommandGroup(server_id=s.id, name="Nightly Maintenance", run_in_parallel=True, created_by_id=admin.id)
        db.session.add(grp)
        db.session.flush()

        it1 = CommandItem(server_id=s.id, name="Clean Temp Files", command_type="remote", command_content="rm -rf /tmp/*", created_by_id=admin.id)
        it2 = CommandItem(server_id=s.id, name="Backup DB", command_type="remote", command_content="pg_dump", created_by_id=admin.id)
        db.session.add_all([it1, it2])
        db.session.flush()

        # Audit currently running with it1 running and it2 running (parallel!)
        steps_payload = [
            {"item_id": it1.id, "name": "Clean Temp Files", "command_type": "remote", "status": "running", "exit_code": None, "output": "Removing temp cache..."},
            {"item_id": it2.id, "name": "Backup DB", "command_type": "remote", "status": "running", "exit_code": None, "output": "Dumping database..."},
        ]

        audit = CommandAudit(
            task_id="full_log_audit_grp",
            user_id=admin.id,
            server_id=s.id,
            group_id=grp.id,
            entry_type="group",
            title="Nightly Maintenance",
            command="parallel maintenance",
            status=COMMAND_STATUS_RUNNING,
            exit_code=None,
            step_results=json.dumps(steps_payload),
            full_output="Starting parallel tasks...",
        )
        db.session.add(audit)
        db.session.commit()
        audit_id = audit.id
        it1_id, it2_id = it1.id, it2.id

    login(client, "admin_log_look@example.com")

    resp = client.get(f"/apps/remote-exec/commands/{audit_id}/full-log")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Accordion head has Command Group Name & parallel badge
    assert "Nightly Maintenance" in html
    assert "Parallel Execution" in html
    assert 'data-lucide="zap"' in html
    assert "group-log-accordion" in html

    # Both parallel running items show SPINNER and RUNNING badge next to item names
    assert f'id="group-step-{it1_id}"' in html
    assert f'id="group-step-{it2_id}"' in html
    assert f'id="step-running-{it1_id}"' in html
    assert f'id="step-running-{it2_id}"' in html
    assert "Removing temp cache..." in html
    assert "Dumping database..." in html


def test_remote_exec_dashboard_displays_server_ram_disk_and_core_tags(client, app):
    """Verify server cards on /apps/remote-exec/ display RAM/Disk bar graphs and Core tags capsules."""
    with app.app_context():
        user = make_member(name="Telemetry User", email="telem_user@example.com")
        sync_member_features(user, [FEATURE_REMOTE_EXEC])

        server = Server(
            name="Orchestration Cluster Node 1",
            ip="10.200.1.15",
            os_type="linux",
            os_version="Ubuntu 22.04 LTS",
            status="available",
            ram_total="32.0 GB",
            ram_used="8.0 GB",
            ram_percent=25.0,
            disk_total="500.0 GB",
            disk_used="100.0 GB",
            disk_percent=20.0,
            core_tags="drupal: Drupal 10.1 [/var/www/drupal] [Running on port 80]\r\ncontainerizers: Docker 24.0.5 [Compose: /opt/app/docker-compose.yml]\r\npython: Python 3.11.4",
        )
        db.session.add(server)
        db.session.commit()
        server_id = server.id

    login(client, "telem_user@example.com")
    resp = client.get("/apps/remote-exec/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # Server card presence
    assert "Orchestration Cluster Node 1" in html
    assert "10.200.1.15" in html

    # Packages line is removed / ignored
    assert "Packages: 887 packages installed" not in html
    assert "<strong>Packages:</strong>" not in html

    # RAM Bar Graph & Tabular Readout
    assert f'id="ram-bar-fill-{server_id}"' in html
    assert "width:25.0%" in html
    assert f'id="ram-text-{server_id}"' in html
    assert "8.0 GB / 32.0 GB" in html

    # Storage Bar Graph & Tabular Readout
    assert f'id="disk-bar-fill-{server_id}"' in html
    assert "width:20.0%" in html
    assert f'id="disk-text-{server_id}"' in html
    assert "100.0 GB / 500.0 GB" in html

    # Core Tags Capsule Badges (No redundant Drupal: / Containerizers: prefix label)
    assert f'id="telemetry-details-{server_id}"' in html
    assert "Drupal 10.1 [/var/www/drupal] [Running on port 80]" in html
    assert "Docker 24.0.5 [Compose: /opt/app/docker-compose.yml]" in html
    assert "Python 3.11.4" in html
    assert "<strong>Drupal:</strong>" not in html
    assert "<strong>Containerizers:</strong>" not in html

    # Lucide icons standard
    assert 'data-lucide="cpu"' in html
    assert 'data-lucide="hard-drive"' in html


# ─── 11. Reconnect, Copy Password Security & Permissions Matrix Tests ───────

def test_server_reconnect_verify_endpoint(client, app):
    """Test POST /apps/remote-exec/servers/<id>/verify updates last_connected_at and telemetry."""
    with app.app_context():
        user = make_member(name="Verify User", email="verify_user@example.com")
        sync_member_features(user, [FEATURE_REMOTE_EXEC])
        server = make_server(name="Verify Server", ip="10.0.0.100")
        cred = ServerCredential(server_id=server.id, user_id=user.id, username="testuser")
        cred.password = "secretpass"
        db.session.add(cred)
        # Give view access
        sa = ServerAccess(server_id=server.id, member_id=user.id, can_view=True, connection_status="unverified")
        db.session.add(sa)
        db.session.commit()
        server_id = server.id
        user_id = user.id

    login(client, "verify_user@example.com")

    # Mock test_ssh_connection success
    mock_telemetry = {
        "operating_system": "Ubuntu 22.04 LTS",
        "os_version": "Ubuntu 22.04 LTS",
        "ram_total": "16.0 GB",
        "ram_used": "4.0 GB",
        "ram_percent": 25.0,
        "disk_total": "200.0 GB",
        "disk_used": "50.0 GB",
        "disk_percent": 25.0,
        "tag_lines": ["drupal: Drupal 10"],
        "k8s_pods": [{"name": "web-pod-1", "status": "Running", "age": "2d", "namespace": "default"}],
    }

    with patch("mizalla.remote_exec.test_ssh_connection", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = (True, "Connected", mock_telemetry)
        resp = client.post(f"/apps/remote-exec/servers/{server_id}/verify")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "last_connected_at" in data
        assert data["ram_total"] == "16.0 GB"
        assert len(data["k8s_pods"]) == 1

    # Verify database state
    with app.app_context():
        sa = ServerAccess.query.filter_by(server_id=server_id, member_id=user_id).first()
        assert sa.connection_status == "available"
        assert sa.last_connected_at is not None

    # Test failure case
    with patch("mizalla.remote_exec.test_ssh_connection", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = (False, "Authentication rejected", {})
        resp = client.post(f"/apps/remote-exec/servers/{server_id}/verify")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is False
        assert "Authentication rejected" in data["error"]

    with app.app_context():
        sa = ServerAccess.query.filter_by(server_id=server_id, member_id=user_id).first()
        assert sa.connection_status == "failed"


def test_server_password_security_copy_permission(client, app):
    """Verify regular members cannot copy Admin fallback passwords (403), but can copy their own."""
    with app.app_context():
        admin = make_member(name="Admin Boss", email="admin_boss@example.com", is_admin=True)
        member = make_member(name="Reg Member", email="reg_member@example.com")
        sync_member_features(member, [FEATURE_REMOTE_EXEC])
        server = make_server(name="Protected Server", ip="10.0.0.101")
        # Admin entered credential
        admin_cred = ServerCredential(server_id=server.id, user_id=admin.id, username="root")
        admin_cred.password = "AdminSuperSecret"
        db.session.add(admin_cred)
        # Give member view access
        sa = ServerAccess(server_id=server.id, member_id=member.id, can_view=True)
        db.session.add(sa)
        db.session.commit()
        server_id = server.id
        member_id = member.id

    # 1. Regular member attempts to copy fallback password -> 403 Forbidden
    login(client, "reg_member@example.com")
    resp = client.get(f"/apps/remote-exec/servers/{server_id}/password")
    assert resp.status_code == 403

    # 2. Admin copies password -> 200 with password
    login(client, "admin_boss@example.com")
    resp = client.get(f"/apps/remote-exec/servers/{server_id}/password")
    assert resp.status_code == 200
    assert resp.get_json()["password"] == "AdminSuperSecret"

    # 3. Regular member saves own credential -> 200 with their own password
    with app.app_context():
        mem_cred = ServerCredential(server_id=server_id, user_id=member_id, username="member_ssh")
        mem_cred.password = "MemberPersonalPass"
        db.session.add(mem_cred)
        db.session.commit()

    login(client, "reg_member@example.com")
    resp = client.get(f"/apps/remote-exec/servers/{server_id}/password")
    assert resp.status_code == 200
    assert resp.get_json()["password"] == "MemberPersonalPass"


def test_connectable_servers_user_filtering(client, app):
    """Verify only servers connected at least once appear in connectable_servers and dropdowns."""
    with app.app_context():
        member = make_member(name="Filtering User", email="filter_user@example.com")
        sync_member_features(member, [FEATURE_REMOTE_EXEC])
        s1 = make_server(name="Unconnected Server", ip="10.0.0.201", with_cred=True)
        s2 = make_server(name="Connected Server", ip="10.0.0.202", with_cred=True)

        # Member can view s1, but has never connected (last_connected_at is None)
        sa1 = ServerAccess(server_id=s1.id, member_id=member.id, can_view=True, last_connected_at=None, connection_status="unverified")
        # Member can view s2, and has connected
        sa2 = ServerAccess(server_id=s2.id, member_id=member.id, can_view=True, last_connected_at=datetime.now(timezone.utc), connection_status="available")
        db.session.add_all([sa1, sa2])
        db.session.commit()

        # Check get_connectable_servers_for_user
        connectable = get_connectable_servers_for_user(member)
        connectable_ids = [s.id for s in connectable]
        assert s1.id not in connectable_ids
        assert s2.id in connectable_ids

    login(client, "filter_user@example.com")
    resp = client.get("/apps/remote-exec/groups/new")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # In group form dropdown, Connected Server should be present as an option, Unconnected should not
    assert f'<option value="{s2.id}">' in html
    assert f'<option value="{s1.id}">' not in html


def test_admin_permissions_matrix_access_and_updates(client, app):
    """Verify Admin Permissions Matrix RBAC, view, and update operations."""
    with app.app_context():
        admin = make_member(name="Matrix Admin", email="matrix_admin@example.com", is_admin=True)
        member = make_member(name="Matrix Member", email="matrix_member@example.com")
        sync_member_features(member, [FEATURE_REMOTE_EXEC])
        server = make_server(name="Matrix Node", ip="10.0.0.220")
        item = CommandItem(server_id=server.id, name="Matrix Item", command_type=COMMAND_TYPE_REMOTE, command_content="uptime", created_by_id=admin.id)
        group = CommandGroup(name="Matrix Group", server_id=server.id, created_by_id=admin.id)
        db.session.add_all([item, group])
        db.session.commit()
        member_id = member.id
        server_id = server.id
        item_id = item.id
        group_id = group.id

    # 1. Non-admin forbidden
    login(client, "matrix_member@example.com")
    resp = client.get("/apps/admin/permissions-matrix")
    assert resp.status_code == 403

    # 2. Admin GET
    login(client, "matrix_admin@example.com")
    resp = client.get("/apps/admin/permissions-matrix")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Server Access Grid" in html
    assert "Member Authoring Capabilities" in html
    assert "Command Items Permissions Grid" in html
    assert "Command Groups Permissions Grid" in html

    # 3. POST member_capabilities
    resp = client.post("/apps/admin/permissions-matrix", data={
        "matrix_type": "member_capabilities",
        f"can_add_items_{member_id}": "1",
        f"can_add_groups_{member_id}": "1",
    })
    assert resp.status_code in (200, 302)

    with app.app_context():
        m = Member.query.get(member_id)
        assert user_can_add_item(m) is True
        assert user_can_add_group(m) is True

    # 4. POST server_access
    resp = client.post("/apps/admin/permissions-matrix", data={
        "matrix_type": "server_access",
        f"srv_view_{server_id}_{member_id}": "1",
        f"srv_edit_{server_id}_{member_id}": "1",
    })
    assert resp.status_code in (200, 302)

    with app.app_context():
        m = Member.query.get(member_id)
        s = Server.query.get(server_id)
        assert user_can_view_server(m, s) is True
        assert user_can_edit_server(m, s) is True

    # 5. POST command_items (VIEW = RUN, EDIT = RUN/DELETE)
    resp = client.post("/apps/admin/permissions-matrix", data={
        "matrix_type": "command_items",
        f"item_view_{item_id}_{member_id}": "1",
        f"item_edit_{item_id}_{member_id}": "1",
    })
    assert resp.status_code in (200, 302)

    with app.app_context():
        m = Member.query.get(member_id)
        ci = CommandItem.query.get(item_id)
        assert user_can_view_item(m, ci) is True
        assert user_can_run_item(m, ci) is False
        assert user_can_edit_item(m, ci) is True
        assert user_can_delete_item(m, ci) is True

    # 6. POST command_groups (VIEW = RUN, EDIT = RUN/DELETE)
    resp = client.post("/apps/admin/permissions-matrix", data={
        "matrix_type": "command_groups",
        f"group_view_{group_id}_{member_id}": "1",
        f"group_edit_{group_id}_{member_id}": "1",
    })
    assert resp.status_code in (200, 302)

    with app.app_context():
        m = Member.query.get(member_id)
        cg = CommandGroup.query.get(group_id)
        assert user_can_view_group(m, cg) is True
        assert user_can_run_group(m, cg) is True
        assert user_can_edit_group(m, cg) is True
        assert user_can_delete_group(m, cg) is True

def test_test_ssh_connection_accepts_timeout():
    """Verify test_ssh_connection and _build_client_options accept timeout argument without TypeError."""
    from mizalla.asyncssh_engine import _build_client_options, test_ssh_connection
    import inspect

    sig = inspect.signature(test_ssh_connection)
    assert "timeout" in sig.parameters

    opts = _build_client_options("admin", "secret", timeout=5)
    assert opts["connect_timeout"] == 5.0

    opts_default = _build_client_options("admin", "secret")
    assert opts_default["connect_timeout"] == 7.0


def test_save_fallback_credential_with_timeout(client, app):
    """Verify POST /apps/remote-exec/servers/<id>/fallback-credential works without TypeError on timeout."""
    with app.app_context():
        user = make_member(name="Fallback User", email="fallback_user@example.com")
        server = Server(
            name="Fallback Server",
            ip="192.168.1.199",
            operating_system="Ubuntu 22.04",
            agent_status="online",
        )
        db.session.add(server)
        db.session.commit()
        server_id = server.id

        grant = ServerAccess(server_id=server.id, member_id=user.id, can_view=True)
        db.session.add(grant)
        sync_member_features(user, [FEATURE_REMOTE_EXEC])
        db.session.commit()

    login(client, "fallback_user@example.com")

    # Call fallback-credential with mock test_ssh_connection
    mock_telemetry = {
        "operating_system": "Ubuntu 22.04 LTS",
        "ram_total": "8.0 GB",
        "disk_total": "100.0 GB",
        "core_tags": "",
    }
    with patch("mizalla.remote_exec.test_ssh_connection", new_callable=AsyncMock) as mock_ssh:
        mock_ssh.return_value = (True, "Connected", mock_telemetry)
        resp = client.post(
            f"/apps/remote-exec/servers/{server_id}/fallback-credential",
            data={
                "username": "ubuntu",
                "password": "mypassword123",
            },
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        # Verify test_ssh_connection was called with timeout=5
        assert mock_ssh.called
        _, kwargs = mock_ssh.call_args
        assert kwargs.get("timeout") == 5


# ─── 13. Command Item / Command Group Revamp Tests ──────────────────────────

def test_command_item_server_independent_and_inferred_variables(client, app):
    """Verify Command Items are saved server-independent (server_id=None) and
    inferred variables are auto-extracted from script with user type/default overrides."""
    import json

    with app.app_context():
        admin = make_member("Admin Revamp", "admin_revamp@example.com", is_admin=True)
        sync_member_features(admin, [FEATURE_REMOTE_EXEC])

    login(client, "admin_revamp@example.com")

    # 1. Create remote item without server_id with inferred variables
    script = 'echo "Deploying {{ app_name }} version {{ version_tag }} with debug={{ is_debug }}"'
    var_overrides = {
        "app_name": {"var_type": VAR_TYPE_TEXT, "default_value": "Mizalla", "description": "Application name"},
        "version_tag": {"var_type": VAR_TYPE_TEXT, "default_value": "v1.0.0", "description": "Release tag"},
        "is_debug": {"var_type": VAR_TYPE_BOOLEAN, "default_value": "false", "description": "Debug flag"},
    }

    resp = client.post(
        "/apps/remote-exec/items/save",
        data={
            "name": "Deploy Script",
            "command_type": COMMAND_TYPE_REMOTE,
            "command_content": script,
            "variables_json": json.dumps(var_overrides),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        item = CommandItem.query.filter_by(name="Deploy Script").first()
        assert item is not None
        assert item.server_id is None
        assert len(item.variables) == 3
        var_dict = {v.name: v for v in item.variables}
        assert "app_name" in var_dict
        assert var_dict["app_name"].var_type == VAR_TYPE_TEXT
        assert var_dict["app_name"].default_value == "Mizalla"
        assert var_dict["is_debug"].var_type == VAR_TYPE_BOOLEAN
        assert var_dict["is_debug"].default_value == "false"
        item_id = item.id

    # 2. GET /apps/remote-exec/items/<id>/data returns item details and inferred variables
    resp_data = client.get(f"/apps/remote-exec/items/{item_id}/data")
    assert resp_data.status_code == 200
    item_json = resp_data.get_json()
    assert item_json["id"] == item_id
    assert item_json["name"] == "Deploy Script"
    assert len(item_json["variables"]) == 3

    # 3. Update script to remove {{ is_debug }} and add {{ port }}
    new_script = 'echo "Deploying {{ app_name }} version {{ version_tag }} on port {{ port }}"'
    resp_update = client.post(
        "/apps/remote-exec/items/save",
        data={
            "item_id": item_id,
            "name": "Deploy Script Updated",
            "command_type": COMMAND_TYPE_REMOTE,
            "command_content": new_script,
            "variables_json": json.dumps({"port": {"var_type": VAR_TYPE_NUMBER, "default_value": "8080"}}),
        },
        follow_redirects=True,
    )
    assert resp_update.status_code == 200

    with app.app_context():
        updated_item = CommandItem.query.get(item_id)
        assert updated_item.name == "Deploy Script Updated"
        vars_after = {v.name: v for v in updated_item.variables}
        # is_debug should be pruned, port added, app_name and version_tag preserved
        assert "is_debug" not in vars_after
        assert "port" in vars_after
        assert vars_after["port"].var_type == VAR_TYPE_NUMBER
        assert vars_after["port"].default_value == "8080"
        assert vars_after["app_name"].default_value == "Mizalla"


def test_command_item_direct_execution_blocked(client, app):
    """Verify Command Items cannot be executed directly; only Command Groups can be executed."""
    with app.app_context():
        admin = make_member("Admin DirectExec", "admin_directexec@example.com", is_admin=True)
        sync_member_features(admin, [FEATURE_REMOTE_EXEC])
        item = CommandItem(name="Direct Item", command_type=COMMAND_TYPE_REMOTE, command_content="uptime", created_by_id=admin.id)
        db.session.add(item)
        db.session.commit()
        item_id = item.id

    login(client, "admin_directexec@example.com")
    resp = client.post(
        f"/apps/remote-exec/run/item/{item_id}",
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["success"] is False
    assert "Command Items cannot be run directly" in data["message"]


def test_command_item_file_upload_and_size_limit(client, app):
    """Verify Upload File(s) command type accepts any file type up to 5 MB and rejects files > 5 MB."""
    import io

    with app.app_context():
        admin = make_member("Admin Upload", "admin_upload@example.com", is_admin=True)
        sync_member_features(admin, [FEATURE_REMOTE_EXEC])

    login(client, "admin_upload@example.com")

    # 1. Attempt uploading a file > 5 MB (e.g. 5.1 MB = 5.1 * 1024 * 1024 bytes)
    big_file_data = b"x" * (5 * 1024 * 1024 + 1024)
    resp_big = client.post(
        "/apps/remote-exec/items/save",
        data={
            "name": "Big File Item",
            "command_type": COMMAND_TYPE_UPLOAD,
            "destination_path": "/var/tmp/big.bin",
            "files": (io.BytesIO(big_file_data), "big.bin"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    # Flash error message for file exceeding 5 MB limit
    html_big = resp_big.get_data(as_text=True)
    assert "exceeds maximum allowed size" in html_big

    # 2. Upload valid file under 5 MB
    valid_file_data = b"Hello, this is a config payload for remote server deployment."
    resp_valid = client.post(
        "/apps/remote-exec/items/save",
        data={
            "name": "Valid Config Item",
            "command_type": COMMAND_TYPE_UPLOAD,
            "destination_path": "/etc/mizalla/app.conf",
            "files": (io.BytesIO(valid_file_data), "app.conf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp_valid.status_code == 200

    with app.app_context():
        item = CommandItem.query.filter_by(name="Valid Config Item").first()
        assert item is not None
        assert item.command_type == COMMAND_TYPE_UPLOAD
        assert "app.conf" in item.command_content
        item_id = item.id

    # 3. Verify file appears in /apps/remote-exec/items/<id>/data
    resp_data = client.get(f"/apps/remote-exec/items/{item_id}/data")
    assert resp_data.status_code == 200
    attached_files = resp_data.get_json()["files"]
    assert any(f["name"] == "app.conf" for f in attached_files)

    # 4. Delete file via /apps/remote-exec/items/<id>/delete-file
    resp_del = client.post(
        f"/apps/remote-exec/items/{item_id}/delete-file",
        data={"filename": "app.conf"},
    )
    assert resp_del.status_code == 200
    assert resp_del.get_json()["success"] is True

    # Confirm it's deleted
    resp_data2 = client.get(f"/apps/remote-exec/items/{item_id}/data")
    assert len(resp_data2.get_json()["files"]) == 0


def test_command_group_ui_revamp_dashboard_and_builder(client, app):
    """Verify Command Group UI revamp:
    1. Dashboard has no top action buttons and Command Groups panel is full width.
    2. Group builder has 70/30 split layout with available items in right column.
    3. Group creation directly persists initial ordered item_ids[].
    4. Group edit persists reordered item_ids[]."""
    with app.app_context():
        admin = make_member("Admin CGUI", "admin_cgui@example.com", is_admin=True)
        sync_member_features(admin, [FEATURE_REMOTE_EXEC])
        server = make_server("Target Node", "10.0.0.100", with_cred=True)

        item1 = CommandItem(name="First Item", command_type=COMMAND_TYPE_REMOTE, command_content="echo 1", created_by_id=admin.id)
        item2 = CommandItem(name="Second Item", command_type=COMMAND_TYPE_LOCAL, command_content="echo 2", created_by_id=admin.id)
        grp = CommandGroup(name="Initial Group", server_id=server.id, created_by_id=admin.id)
        db.session.add_all([item1, item2, grp])
        db.session.commit()

        server_id = server.id
        item1_id = item1.id
        item2_id = item2.id
        grp_id = grp.id

    login(client, "admin_cgui@example.com")

    # 1. Verify Dashboard has no top buttons and has full-width Command Groups
    dash_resp = client.get("/apps/remote-exec/")
    assert dash_resp.status_code == 200
    dash_html = dash_resp.get_data(as_text=True)
    assert "+ New Command Item" not in dash_html
    # In top header row, action buttons were removed
    assert f'/apps/remote-exec/groups/{grp_id}/edit' in dash_html
    assert "Command Groups" in dash_html

    # 2. Verify /apps/remote-exec/groups/new has 70/30 split layout and items browser
    new_resp = client.get("/apps/remote-exec/groups/new")
    assert new_resp.status_code == 200
    new_html = new_resp.get_data(as_text=True)
    assert "group-builder-grid" in new_html
    assert "builder-left-col" in new_html
    assert "builder-right-col" in new_html
    assert "Command Items in this Group" in new_html
    assert "First Item" in new_html
    assert "Second Item" in new_html
    assert "Created by: Admin CGUI" in new_html
    assert "appendItemToGroup" in new_html

    # 3. Create group with initial ordered items [item2, item1]
    create_resp = client.post(
        "/apps/remote-exec/groups/new",
        data={
            "name": "Ordered Pipeline",
            "server_id": server_id,
            "description": "Multi-item pipeline",
            "item_ids[]": [item2_id, item1_id],
        },
        follow_redirects=True,
    )
    assert create_resp.status_code == 200

    with app.app_context():
        created_grp = CommandGroup.query.filter_by(name="Ordered Pipeline").first()
        assert created_grp is not None
        assert len(created_grp.items) == 2
        # Check order: item2 first (#1), item1 second (#2)
        assert created_grp.items[0].command_item_id == item2_id
        assert created_grp.items[0].sequence_order == 1
        assert created_grp.items[1].command_item_id == item1_id
        assert created_grp.items[1].sequence_order == 2
        new_grp_id = created_grp.id

    # 4. Edit group to reorder / repeat: [item1, item2, item1]
    edit_resp = client.post(
        f"/apps/remote-exec/groups/{new_grp_id}/edit",
        data={
            "name": "Reordered Pipeline",
            "description": "Updated sequence",
            "item_ids[]": [item1_id, item2_id, item1_id],
        },
        follow_redirects=True,
    )
    assert edit_resp.status_code == 200

    with app.app_context():
        updated_grp = CommandGroup.query.get(new_grp_id)
        assert updated_grp.name == "Reordered Pipeline"
        assert len(updated_grp.items) == 3
        assert updated_grp.items[0].command_item_id == item1_id
        assert updated_grp.items[0].sequence_order == 1
        assert updated_grp.items[1].command_item_id == item2_id
        assert updated_grp.items[1].sequence_order == 2
        assert updated_grp.items[2].command_item_id == item1_id
        assert updated_grp.items[2].sequence_order == 3


def test_command_group_item_modal_and_edit_permissions(client, app):
    """Test Command Group New/Edit page item creation modal, edit button visibility, and AJAX save."""
    with app.app_context():
        user1 = make_member(name="User Alpha", email="alpha@mizalla.test", is_admin=False, can_add_command_items=True)
        user2 = make_member(name="User Beta", email="beta@mizalla.test", is_admin=False, can_add_command_items=False)
        sync_member_features(user1, [FEATURE_REMOTE_EXEC])
        sync_member_features(user2, [FEATURE_REMOTE_EXEC])
        server = make_server(name="AlphaServer", ip="10.0.0.99", connected_user=user1, with_cred=True)

        # Item 1 created by user1
        item1 = CommandItem(
            name="Alpha Own Script",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="echo 'hello {{ user_name }}'",
            created_by_id=user1.id,
        )
        # Item 2 created by user2
        item2 = CommandItem(
            name="Beta Own Script",
            command_type=COMMAND_TYPE_REMOTE,
            command_content="echo 'beta secret'",
            created_by_id=user2.id,
        )
        db.session.add_all([item1, item2])
        db.session.commit()

        # Grant user1 read-only (view) access to item2
        perm = CommandItemPermission(
            command_item_id=item2.id,
            user_id=user1.id,
            can_view_logs=True,
            can_edit=False,
        )
        db.session.add(perm)
        db.session.commit()

        user1_id = user1.id
        item1_id = item1.id
        item2_id = item2.id

    # 1. Login as user1
    login(client, "alpha@mizalla.test")

    # 2. View New Command Group page
    new_grp_resp = client.get("/apps/remote-exec/groups/new")
    assert new_grp_resp.status_code == 200
    html = new_grp_resp.get_data(as_text=True)

    # Check New Command Item button and modals exist
    assert "btn-new-command-item" in html
    assert "New Command Item" in html
    assert 'id="item-modal"' in html
    assert 'id="variables-modal"' in html

    # Check Edit button is present for item1 (user1 is creator)
    assert f"openEditItemModal({item1_id})" in html
    # Check Edit button is NOT present for item2 (user1 has can_edit=False)
    assert f"openEditItemModal({item2_id})" not in html

    # 3. Test item data endpoint
    data_resp = client.get(f"/apps/remote-exec/items/{item1_id}/data")
    assert data_resp.status_code == 200
    item_json = data_resp.get_json()
    assert item_json["name"] == "Alpha Own Script"
    assert item_json["command_type"] == "remote"
    assert "user_name" in item_json["command_content"]

    # 4. Test AJAX save for updating item1
    update_resp = client.post(
        "/apps/remote-exec/items/save",
        data={
            "ajax": "1",
            "item_id": item1_id,
            "name": "Alpha Own Script Updated",
            "command_type": "remote",
            "command_content": "echo 'greetings {{ user_name }} and {{ role }}'",
            "var_name[]": ["user_name", "role"],
            "var_type[]": ["text", "text"],
            "var_default[]": ["Guest", "Viewer"],
            "var_desc[]": ["User name", "User role"],
        },
    )
    assert update_resp.status_code == 200
    update_data = update_resp.get_json()
    assert update_data["success"] is True
    assert update_data["item"]["name"] == "Alpha Own Script Updated"
    assert update_data["item"]["can_edit"] is True
    assert "user_name" in update_data["item"]["variables"]
    assert "role" in update_data["item"]["variables"]

    # Verify update in DB
    with app.app_context():
        refreshed_item1 = CommandItem.query.get(item1_id)
        assert refreshed_item1.name == "Alpha Own Script Updated"
        assert len(refreshed_item1.variables) == 2

    # 5. Test AJAX save for creating a new item
    create_resp = client.post(
        "/apps/remote-exec/items/save",
        data={
            "ajax": "1",
            "name": "Brand New Modal Item",
            "command_type": "remote",
            "command_content": "uptime",
        },
    )
    assert create_resp.status_code == 200
    create_data = create_resp.get_json()
    assert create_data["success"] is True
    assert create_data["item"]["name"] == "Brand New Modal Item"
    assert create_data["item"]["creator_name"] == "User Alpha"
    assert create_data["item"]["can_edit"] is True

    # 6. Test unauthorized edit on item2
    unauth_resp = client.post(
        "/apps/remote-exec/items/save",
        data={
            "ajax": "1",
            "item_id": item2_id,
            "name": "Hacked Beta Script",
            "command_type": "remote",
            "command_content": "rm -rf /",
        },
    )
    assert unauth_resp.status_code == 403
    unauth_data = unauth_resp.get_json()
    assert unauth_data["success"] is False
    assert "permission" in unauth_data["error"]
