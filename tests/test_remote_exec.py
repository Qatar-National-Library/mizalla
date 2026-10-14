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
    ServerCredential,
)
from mizalla.remote_exec_client import (
    get_effective_credential,
    user_can_duplicate_group,
    user_can_duplicate_item,
    user_can_edit_group,
    user_can_edit_item,
    user_can_run_group,
    user_can_run_item,
    user_can_view_group_logs,
    user_can_view_item_logs,
)

PASSWORD = "test-password-123"


def make_member(name="Test User", email="user@example.com", is_admin=False, **kwargs):
    member = Member(name=name, email=email, is_admin=is_admin, **kwargs)
    member.set_password(PASSWORD)
    db.session.add(member)
    db.session.commit()
    return member


def make_server(name="Production Node 1", ip="192.168.1.50", os_type="linux", os_version="Ubuntu 22.04 LTS"):
    server = Server(name=name, ip=ip, os_type=os_type, os_version=os_version, status="available")
    db.session.add(server)
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
        s = make_server()
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
        assert item.server_id == s_id
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
        assert copy_item.server_id == s_id
        assert len(copy_item.variables) == 1
        assert copy_item.variables[0].default_value == "nginx"


# ─── 5. Command Groups & Orchestration Sequencing ────────────────────────────

def test_command_group_creation_and_duplication_with_subitems(app, client):
    """Test Command Group creation, sub-items ordering, and deep DUPLICATE."""
    with app.app_context():
        u = make_member("Lead", "lead@example.com")
        sync_member_features(u, [FEATURE_REMOTE_EXEC])
        s = make_server()
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
        # Verify sub-items were duplicated under the current user
        assert dup_grp.items[0].command_item.name == "Step 1 - Pull (Copy)"
        assert dup_grp.items[0].command_item.created_by_id == u_id


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

    # Spinner and RUNNING badge rendered as inline-flex (visible)
    assert f'id="group-running-{grp_id}" style="display:inline-flex;' in html
    assert f'id="item-running-{item_id}" style="display:inline-flex;' in html
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

