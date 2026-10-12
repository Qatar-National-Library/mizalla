from datetime import date, datetime

from mizalla.extensions import db
from mizalla.models import Member, Project, ProjectDocument, ProjectMembership, Meeting, Task
from mizalla.secure_links import generate_signed_document_token, verify_signed_document_token
from mizalla_mcp import (
    add_project_member,
    add_task_step,
    create_meeting,
    create_project,
    create_task,
    get_highlight_types,
    get_milestones,
    get_project_details,
    get_project_summary,
    update_project,
    update_task_step,
)


def test_signed_document_token_generation_and_verification(app):
    with app.app_context():
        token = generate_signed_document_token(document_id=42, member_id=7, secret_key=app.config["SECRET_KEY"])
        assert token is not None

        payload = verify_signed_document_token(token, max_age=3600, secret_key=app.config["SECRET_KEY"])
        assert payload == {"doc_id": 42, "mem_id": 7, "action": "doc_download"}

        invalid_payload = verify_signed_document_token(token + "tampered", max_age=3600, secret_key=app.config["SECRET_KEY"])
        assert invalid_payload is None


def test_signed_document_download_route(app, client, tmp_path):
    with app.app_context():
        member = Member(name="Doc Member", email="doc@example.com")
        member.set_password("doc-member-pass-123")
        project = Project(project_id="SEC-1", name="Secure Doc Project")
        project.memberships.append(ProjectMembership(member=member, role="team"))

        upload_folder = tmp_path / "uploads"
        upload_folder.mkdir(parents=True, exist_ok=True)
        file_path = upload_folder / "test_stored.txt"
        file_path.write_text("Confidential document content", encoding="utf-8")

        document = ProjectDocument(
            project=project,
            filename="report.txt",
            stored_name="test_stored.txt",
            description="Test report",
        )
        db.session.add_all([member, project, document])
        db.session.commit()
        doc_id = document.id
        member_id = member.id
        app.config["UPLOAD_FOLDER"] = str(upload_folder)

    client.post("/apps/login", data={"email": "doc@example.com", "password": "doc-member-pass-123"})
    res = client.get(f"/apps/mizalla/documents/{doc_id}/secure-link?json=1")
    assert res.status_code == 200
    data = res.get_json()
    assert data["filename"] == "report.txt"
    assert "/apps/mizalla/documents/signed/" in data["signed_url"]

    client.get("/apps/logout")
    token = generate_signed_document_token(doc_id, member_id, secret_key=app.config["SECRET_KEY"])
    download_res = client.get(f"/apps/mizalla/documents/signed/{token}")
    assert download_res.status_code == 200
    assert download_res.data == b"Confidential document content"

    assert client.get("/apps/mizalla/documents/signed/invalid-token").status_code == 403


def test_deep_integration_mcp_tools(app):
    with app.app_context():
        user = Member(name="Lead Dev", email="lead@example.com")
        user.set_password("lead-pass-12345")
        client_user = Member(name="Client Rep", email="client@example.com")
        client_user.set_password("client-pass-123")
        db.session.add_all([user, client_user])
        db.session.commit()

        # 1. Create project via MCP
        res = create_project("lead@example.com", "API-01", "API Project", "Deep Integration", "Scope statement")
        assert "id" in res
        proj_id = res["id"]

        # 2. Add member with multiple team roles via MCP
        mem_res = add_project_member(
            proj_id,
            "lead@example.com",
            "client@example.com",
            "team",
            "Project Lead & QA",
            team_roles=["Project Manager", "Developer/Implementer", "Qa/Tester"],
        )
        assert mem_res["role"] == "team"
        assert mem_res["team_roles"] == ["Project Manager", "Developer/Implementer", "Qa/Tester"]

        # 3. Create task via MCP
        task_res = create_task(proj_id, "lead@example.com", "Implement OAuth2", "Development", priority=2, weight=8)
        assert "id" in task_res
        task_id = task_res["id"]

        # 4. Add task step via MCP
        step_res = add_task_step(task_id, "lead@example.com", "Database Schema", priority=1, percentage_complete=100)
        assert "id" in step_res
        assert step_res["percentage_complete"] == 100

        step2_res = add_task_step(task_id, "lead@example.com", "Endpoint Logic", priority=2, percentage_complete=50)
        step2_id = step2_res["id"]

        # 5. Update task step via MCP
        upd_step = update_task_step(step2_id, "lead@example.com", percentage_complete=75)
        assert upd_step["percentage_complete"] == 75

        # 6. Create meeting via MCP
        meet_res = create_meeting(proj_id, "lead@example.com", "2026-10-15", meeting_time="10:00", agenda="Sprint Sync")
        assert "id" in meet_res

        # 7. Update project via MCP
        upd_proj = update_project(proj_id, "lead@example.com", scope="Updated scope statement")
        assert upd_proj["message"] == "Project updated successfully."

        # 8. Verify project details
        details = get_project_details(proj_id, "lead@example.com")
        assert details["name"] == "API Project"
        assert details["scope"] == "Updated scope statement"
        assert len(details["tasks"]) == 1
        assert len(details["meetings"]) == 1


def test_rest_api_endpoints(app, client):
    with app.app_context():
        user = Member(name="API User", email="api@example.com")
        user.set_password("api-user-password")
        key = user.generate_api_key()
        other = Member(name="Team Dev", email="dev@example.com", password_hash="unused")
        db.session.add_all([user, other])
        db.session.commit()
        user_key = key

    headers = {"Authorization": f"Bearer {user_key}", "Content-Type": "application/json"}

    # 1. Create project via REST API
    create_res = client.post(
        "/apps/mizalla/api/v1/projects",
        headers=headers,
        json={"project_id": "REST-1", "name": "REST App", "description": "Built via API"},
    )
    assert create_res.status_code == 201
    proj_id = create_res.get_json()["id"]

    # 2. Add member with multiple team roles via REST API
    mem_res = client.post(
        f"/apps/mizalla/api/v1/projects/{proj_id}/members",
        headers=headers,
        json={
            "email": "dev@example.com",
            "role": "team",
            "team_roles": ["Project Manager", "Analyst", "Deployment Manager"],
        },
    )
    assert mem_res.status_code == 201
    assert mem_res.get_json()["team_roles"] == ["Project Manager", "Analyst", "Deployment Manager"]

    # 3. Create task via REST API
    task_res = client.post(
        f"/apps/mizalla/api/v1/projects/{proj_id}/tasks",
        headers=headers,
        json={"name": "API Endpoint Task", "task_type": "Development", "status": "In Progress"},
    )
    assert task_res.status_code == 201
    task_id = task_res.get_json()["id"]

    # 4. Add step via REST API
    step_res = client.post(
        f"/apps/mizalla/api/v1/tasks/{task_id}/steps",
        headers=headers,
        json={"header": "Initial Step", "percentage_complete": 30},
    )
    assert step_res.status_code == 201
    step_id = step_res.get_json()["id"]

    # 5. Patch step via REST API
    patch_step = client.patch(
        f"/apps/mizalla/api/v1/steps/{step_id}",
        headers=headers,
        json={"percentage_complete": 100},
    )
    assert patch_step.status_code == 200
    assert patch_step.get_json()["percentage_complete"] == 100


def test_project_team_roles_configuration_and_multi_role_form(app, client):
    with app.app_context():
        roles = app.config["PROJECT_TEAM_ROLES"]
        assert "Project Manager" in roles
        assert "Analyst" in roles
        assert "Developer/Implementer" in roles
        assert "Qa/Tester" in roles
        assert "Infrastructure Management" in roles
        assert "Integration Manager" in roles
        assert "Configuration Manager" in roles
        assert "Deployment Manager" in roles

        pm = Member(name="PM Lead", email="pm@example.com")
        pm.set_password("pm-password-long")
        member_to_add = Member(name="Multi Role Dev", email="multidev@example.com", password_hash="unused")
        project = Project(project_id="ROLES-1", name="Role Test Project")
        # Authority comes from a role on the project, not the global is_pm flag.
        pm_membership = ProjectMembership(member=pm, role="delegated")
        project.memberships.append(pm_membership)
        db.session.add_all([pm, member_to_add, project])
        db.session.commit()
        proj_id = project.id
        add_id = member_to_add.id

    client.post("/apps/login", data={"email": "pm@example.com", "password": "pm-password-long"})

    # Post membership with multiple team_roles checkboxes selected
    post_res = client.post(
        f"/apps/mizalla/projects/{proj_id}/memberships",
        data={
            "member_id": str(add_id),
            "role": "team",
            "responsibility": "Full stack lead",
            "team_roles": ["Project Manager", "Developer/Implementer", "Deployment Manager"],
        },
    )
    assert post_res.status_code == 302

    with app.app_context():
        added_mem = ProjectMembership.query.filter_by(project_id=proj_id, member_id=add_id, role="team").one()
        assert added_mem.get_team_roles_list() == ["Project Manager", "Developer/Implementer", "Deployment Manager"]

    # Render project detail page and verify role tags are rendered
    detail_res = client.get(f"/apps/mizalla/projects/{proj_id}")
    assert detail_res.status_code == 200
    assert b"Developer/Implementer" in detail_res.data
    assert b"Deployment Manager" in detail_res.data


def test_mcp_get_project_details_includes_highlights_and_milestones(app, client):
    with app.app_context():
        user = Member(name="Lead Dev", email="lead@example.com")
        user.set_password("lead-pass-12345")
        db.session.add(user)
        db.session.commit()
        project = Project(project_id="MCP-DET", name="Detail Test Project")
        project.memberships.append(ProjectMembership(member=user, role="team"))
        milestone = Task(
            project=project, name="Milestone one", task_type="Development", status="Not Started",
            is_milestone=True, due_date=date(2026, 12, 31),
        )
        normal_task = Task(
            project=project, name="Normal task", task_type="Development", status="In Progress",
            highlighted_on=date(2026, 9, 22),
        )
        normal_task.set_highlight_list(["Key Finding"])
        db.session.add_all([milestone, normal_task])
        db.session.commit()
        proj_id = project.id

        details = get_project_details(proj_id, "lead@example.com")
    assert details["name"] == "Detail Test Project"
    assert "highlight_types" in details
    assert "milestones" in details
    assert "at_risk_count" in details
    milestone_task = [t for t in details["tasks"] if t["name"] == "Milestone one"][0]
    assert milestone_task["is_milestone"] is True
    assert milestone_task["highlights"] == []
    normal = [t for t in details["tasks"] if t["name"] == "Normal task"][0]
    assert normal["highlights"] == ["Key Finding"]
    assert normal["highlighted_on"] == "2026-09-22"
    assert normal["milestone_achieved_on"] is None
    # current_step is per-task, not at project level
    task_current_steps = [t["current_step"] for t in details["tasks"]]
    assert isinstance(task_current_steps, list)


def test_mcp_get_highlight_types(app, client):
    with app.app_context():
        pm = Member(name="PM", email="pm@example.com")
        pm.set_password("pm-password-long")
        project = Project(project_id="HLT-1", name="Highlight Project")
        project.memberships.append(ProjectMembership(member=pm, role="team"))
        db.session.add_all([pm, project])
        db.session.commit()

    result = get_highlight_types()
    assert isinstance(result["highlight_types"], list)
    # Confirm the setting is populated and a label is present
    assert len(result["highlight_types"]) > 0


def test_mcp_get_milestones(app, client):
    with app.app_context():
        user = Member(name="Dev", email="dev@example.com")
        user.set_password("dev-pass-12345")
        project = Project(project_id="MIL-1", name="Milestone Project")
        project.memberships.append(ProjectMembership(member=user, role="team"))
        m1 = Task(project=project, name="Achieved milestone", task_type="Development", status="Completed",
                  is_milestone=True, milestone_achieved_on=date(2026, 6, 1))
        m2 = Task(project=project, name="Upcoming milestone", task_type="Development", status="Not Started",
                  is_milestone=True, due_date=date(2027, 1, 15))
        db.session.add_all([user, project, m1, m2])
        db.session.commit()
        proj_id = project.id

        result = get_milestones(proj_id, "dev@example.com")
    assert len(result["achieved"]) == 1
    assert result["achieved"][0]["name"] == "Achieved milestone"
    assert len(result["upcoming"]) == 1
    assert result["upcoming"][0]["name"] == "Upcoming milestone"


def test_mcp_get_project_summary_access_control(app, client):
    with app.app_context():
        user = Member(name="Dev", email="dev@example.com")
        user.set_password("dev-pass-12345")
        outsider = Member(name="Stranger", email="stranger@example.com")
        outsider.set_password("stranger-pass-1234")
        project = Project(project_id="SUM-1", name="Summary Project")
        project.memberships.append(ProjectMembership(member=user, role="team"))
        db.session.add_all([user, outsider, project])
        db.session.commit()
        proj_id = project.id

        result = get_project_summary(proj_id, "dev@example.com")
    assert "project" in result
    assert result["project"]["name"] == "Summary Project"
    assert "pulse" in result
    assert "milestones" in result
    assert "highlights" in result
    assert "team" in result

    error = get_project_summary(proj_id, "stranger@example.com")
    assert "error" in error


def test_mcp_get_project_summary_pulse_and_highlights(app, client):
    with app.app_context():
        user = Member(name="PM", email="pm@example.com")
        user.set_password("pm-password-long")
        project = Project(project_id="PULSE-1", name="Pulse Project")
        project.memberships.append(ProjectMembership(member=user, role="team"))
        milestone = Task(project=project, name="Gate review", task_type="Development", status="Not Started",
                         is_milestone=True, due_date=date(2027, 3, 1))
        blocked = Task(project=project, name="Blocked task", task_type="Development", status="Blocked")
        db.session.add_all([user, project, milestone, blocked])
        db.session.commit()
        proj_id = project.id

        result = get_project_summary(proj_id, "pm@example.com")
    assert result["pulse"]["next_milestone"] == "Gate review"
    assert result["pulse"]["needs_attention"] >= 1
    assert len(result["tasks"]["at_risk"]) >= 1
    assert result["milestones"]["upcoming"][0]["name"] == "Gate review"


def test_mcp_get_project_summary_includes_meeting_teams_url(app, client):
    """A future meeting with a start time yields a Teams compose link in the summary."""
    with app.app_context():
        user = Member(name="PM", email="pm@example.com")
        user.set_password("pm-password-long")
        project = Project(project_id="TEAMS-1", name="Teams Link Project")
        project.memberships.append(ProjectMembership(member=user, role="team"))
        meeting = Meeting(
            project=project, meeting_date=date(2027, 6, 1),
            meeting_start_at=datetime(2027, 6, 1, 10, 0), meeting_utc_offset_minutes=-300, agenda="Sync",
        )
        db.session.add_all([user, project, meeting])
        db.session.commit()
        proj_id = project.id

        result = get_project_summary(proj_id, "pm@example.com")
    upcoming = [m for m in result["meetings"]["upcoming"]]
    assert len(upcoming) == 1
    assert upcoming[0]["teams_url"].startswith("https://teams.microsoft.com/l/meeting/new?")
