import uuid
import pytest
from bs4 import BeautifulSoup
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    COMMAND_STATUS_COMPLETED,
    COMMAND_STATUS_RUNNING,
    CommandAudit,
    CommandGroup,
    CommandGroupItem,
    CommandItem,
    FEATURE_REMOTE_EXEC,
    Member,
    Server,
)

PASSWORD = "member-password-long"

@pytest.fixture
def test_setup(app):
    with app.app_context():
        member = Member.query.filter_by(email="streamtester@example.com").first()
        if not member:
            member = Member(
                email="streamtester@example.com",
                name="Stream Tester",
                is_admin=True,
            )
            member.set_password(PASSWORD)
            db.session.add(member)
            sync_member_features(member, [FEATURE_REMOTE_EXEC])
            db.session.commit()

        server = Server.query.filter_by(name="Terminal Test Server").first()
        if not server:
            server = Server(
                name="Terminal Test Server",
                ip="192.168.10.50",
                ssh_port=22,
                status="online",
            )
            db.session.add(server)
            db.session.commit()

        item = CommandItem.query.filter_by(name="Terminal Step 1").first()
        if not item:
            item = CommandItem(
                server_id=server.id,
                name="Terminal Step 1",
                command_type="remote",
                command_content="echo 'Testing Green Command Highlighting'",
                created_by_id=member.id,
            )
            db.session.add(item)
            db.session.commit()

        group = CommandGroup.query.filter_by(name="Terminal Test Group").first()
        if not group:
            group = CommandGroup(
                name="Terminal Test Group",
                server_id=server.id,
                created_by_id=member.id,
                run_in_parallel=False,
            )
            db.session.add(group)
            db.session.commit()

            gi = CommandGroupItem(
                group_id=group.id,
                command_item_id=item.id,
                sequence_order=1,
            )
            db.session.add(gi)
            db.session.commit()

        audit = CommandAudit(
            task_id=str(uuid.uuid4()),
            user_id=member.id,
            server_id=server.id,
            group_id=group.id,
            entry_type="group",
            title=group.name,
            command="Group: Terminal Test Group",
            status=COMMAND_STATUS_RUNNING,
            steps_data=[
                {
                    "item_id": item.id,
                    "name": item.name,
                    "command_type": item.command_type,
                    "status": "running",
                    "exit_code": None,
                    "script": item.command_content,
                    "output": "Starting test step...\nProcessing line 1\n",
                }
            ],
        )
        db.session.add(audit)
        db.session.commit()

        return {
            "member_id": member.id,
            "server_id": server.id,
            "group_id": group.id,
            "item_id": item.id,
            "audit_id": audit.id,
        }

@pytest.fixture
def auth_client(client, test_setup):
    client.post("/apps/login", data={"email": "streamtester@example.com", "password": PASSWORD})
    return client

def test_live_stream_endpoint_returns_steps_and_script(auth_client, test_setup):
    """Verify that /apps/remote-exec/commands/<audit_id>/live-stream returns structured steps and scripts."""
    audit_id = test_setup["audit_id"]
    res = auth_client.get(f"/apps/remote-exec/commands/{audit_id}/live-stream")
    assert res.status_code == 200
    data = res.get_json()
    assert data["audit_id"] == audit_id
    assert data["is_running"] is True
    assert len(data["steps"]) == 1
    assert data["steps"][0]["name"] == "Terminal Step 1"
    assert "echo 'Testing Green Command Highlighting'" in data["steps"][0]["script"]
    assert "Starting test step..." in data["steps"][0]["output"]

def test_dashboard_renders_live_terminal_modal(auth_client):
    """Verify remote_exec_dashboard.html renders #live-terminal-modal with Consolas styling and green commands."""
    res = auth_client.get("/apps/remote-exec/", follow_redirects=True)
    assert res.status_code == 200
    html = res.data.decode("utf-8")

    # Verify modal exists
    assert 'id="live-terminal-modal"' in html
    assert 'id="live-terminal-body"' in html
    assert 'id="live-terminal-steps"' in html

    # Verify Consolas font styling is in template
    assert "Consolas" in html

    # Verify bright green color for executed scripts
    assert "#00e676" in html

    # Verify window.location.reload is removed from submitRunModal
    assert "window.location.reload" not in html
