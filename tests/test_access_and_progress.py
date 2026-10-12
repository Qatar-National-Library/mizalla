import re
import sqlite3
from datetime import date
from urllib.parse import parse_qs, urlsplit

from mizalla import _load_or_create_secret_key, create_app
from mizalla.access import visible_projects_query
from mizalla.extensions import db
from mizalla.jira import is_valid_jira_ticket_id, jira_ticket_url
from mizalla.models import Meeting, Member, Project, ProjectAuditEvent, ProjectMembership, Server, Task, TaskStep, TaskOwner
from mizalla.progress import current_step, project_completion


def make_member(name, email):
    member = Member(name=name, email=email, password_hash="unused")
    db.session.add(member)
    return member


def test_project_visibility_requires_an_explicit_project_role(app):
    with app.app_context():
        member = make_member("A Member", "member@example.com")
        project = Project(project_id="P-1", name="Visible")
        hidden = Project(project_id="P-2", name="Task owner only")
        db.session.add_all([project, hidden])
        db.session.flush()
        db.session.add(ProjectMembership(project=project, member=member, role="vendor"))
        task = Task(project=hidden, task_type="Development", name="Unrelated task")
        db.session.add(task)
        db.session.flush()
        db.session.add(TaskOwner(task=task, member=member))
        db.session.commit()

        class User:
            id = member.id

        from unittest.mock import patch

        with patch("mizalla.access.current_user", User()):
            assert [item.project_id for item in visible_projects_query().all()] == ["P-1"]


def test_project_completion_weights_each_step_by_parent_task_weight(app):
    with app.app_context():
        lighter = Task(project_id=1, task_type="Development", name="Light", weight=1)
        heavier = Task(project_id=1, task_type="Testing", name="Heavy", weight=3)
        lighter.steps = [TaskStep(priority=1, header="A", percentage_complete=100)]
        heavier.steps = [
            TaskStep(priority=1, header="B", percentage_complete=50),
            TaskStep(priority=2, header="C", percentage_complete=0),
        ]
        assert project_completion([lighter, heavier]) == 36


def test_project_without_steps_has_zero_completion(app):
    with app.app_context():
        task = Task(project_id=1, task_type="Development", name="No steps", weight=7)
        assert project_completion([task]) == 0


def test_current_step_uses_first_priority_between_zero_and_one_hundred(app):
    with app.app_context():
        task = Task(project_id=1, task_type="Development", name="Ordered")
        task.steps = [
            TaskStep(priority=1, header="Not started", percentage_complete=0),
            TaskStep(priority=2, header="In progress", percentage_complete=40),
            TaskStep(priority=3, header="Later", percentage_complete=60),
            TaskStep(priority=4, header="Complete", percentage_complete=100),
        ]
        assert current_step(task).header == "In progress"


def test_portfolio_and_direct_project_access_are_membership_scoped(app, client):
    with app.app_context():
        member = Member(name="Project member", email="visible@example.com")
        member.set_password("a-long-enough-password")
        outsider = Member(name="Task owner", email="outsider@example.com", password_hash="unused")
        visible = Project(project_id="VIS-1", name="Visible Project")
        hidden = Project(project_id="HID-1", name="Hidden Project")
        visible.memberships.append(ProjectMembership(member=member, role="client", responsibility="Review"))
        task = Task(project=hidden, task_type="Development", name="Private task")
        task.owners.append(TaskOwner(member=member, responsibility="Implementation"))
        db.session.add_all([member, outsider, visible, hidden, task])
        db.session.commit()
        visible_id = visible.id
        hidden_id = hidden.id

    login = client.post("/apps/login", data={"email": "visible@example.com", "password": "a-long-enough-password"})
    assert login.status_code == 302

    portfolio = client.get("/apps/mizalla/")
    assert portfolio.status_code == 200
    assert b"Visible Project" in portfolio.data
    assert b"Hidden Project" not in portfolio.data
    visible_detail = client.get(f"/apps/mizalla/projects/{visible_id}")
    assert visible_detail.status_code == 200
    assert b"Edit details" not in visible_detail.data
    assert b"Add task" not in visible_detail.data
    assert client.get(f"/apps/mizalla/projects/{hidden_id}").status_code == 404
    assert client.get(f"/apps/mizalla/projects/{visible_id}/edit").status_code == 403
    assert client.post(f"/apps/mizalla/projects/{visible_id}/tasks", data={"name": "Forbidden"}).status_code == 403
    assert client.post(f"/apps/mizalla/projects/{visible_id}/delete").status_code == 403
    assert client.get("/apps/admin/members").status_code == 403


def test_portfolio_search_filters_tasks_without_exposing_hidden_projects(app, client):
    with app.app_context():
        viewer = Member(name="Viewer", email="viewer@example.com")
        viewer.set_password("viewer-password-long")
        owner = Member(name="Owner", email="owner@example.com", password_hash="unused")
        visible = Project(project_id="SEARCH-1", name="Searchable release")
        visible.memberships.extend(
            [
                ProjectMembership(member=viewer, role="team"),
                ProjectMembership(member=owner, role="vendor"),
            ]
        )
        hidden = Project(project_id="SEARCH-2", name="Secret release")
        task = Task(
            project=visible,
            task_type="Testing",
            name="Verify payment flow",
            status="Blocked",
            due_date=date(2026, 10, 1),
            jira_ticket_id="PAY-449",
        )
        task.owners.append(TaskOwner(member=owner, responsibility="Testing"))
        hidden.tasks.append(Task(project=hidden, task_type="Development", name="PAY-449 secret"))
        db.session.add_all([viewer, owner, visible, hidden, task])
        db.session.commit()
        owner_id = owner.id

    client.post("/apps/login", data={"email": "viewer@example.com", "password": "viewer-password-long"})

    by_ticket = client.get("/apps/mizalla/?q=PAY-449")
    assert b"Searchable release" in by_ticket.data
    assert b"Secret release" not in by_ticket.data
    assert b"Verify payment flow" in by_ticket.data

    by_status = client.get("/apps/mizalla/?status=Blocked")
    assert b"Verify payment flow" in by_status.data
    assert b"MATCHING TASKS" in by_status.data

    by_owner = client.get(f"/apps/mizalla/?owner_id={owner_id}")
    assert b"Verify payment flow" in by_owner.data

    by_due_date = client.get("/apps/mizalla/?due_from=2026-10-01&due_to=2026-10-01")
    assert b"Verify payment flow" in by_due_date.data

    invalid_range = client.get("/apps/mizalla/?due_from=2026-10-02&due_to=2026-10-01", follow_redirects=True)
    assert b"Due from must be on or before Due to" in invalid_range.data


def test_login_post_requires_csrf_token(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-key",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'csrf.db'}",
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "WTF_CSRF_ENABLED": True,
        }
    )
    client = app.test_client()
    response = client.get("/apps/login")
    token = re.search(rb'name="csrf_token" value="([^"]+)"', response.data).group(1).decode()
    assert client.post("/apps/login", data={"email": "nobody@example.com", "password": "nope"}).status_code == 400
    assert client.post(
        "/apps/login",
        data={"csrf_token": token, "email": "nobody@example.com", "password": "nope"},
    ).status_code == 200


def test_about_guide_and_html_deck_are_public_while_projects_stay_protected(client):
    about = client.get("/apps/mizalla/about")
    guide = client.get("/apps/mizalla/user-guide")
    deck = client.get("/static/mizalla-feature-deck.html")

    assert about.status_code == 200
    assert b"Mizalla" in about.data
    assert b"mizalla-feature-deck.html" in about.data
    assert guide.status_code == 200
    assert b"User guide" in guide.data
    assert deck.status_code == 200
    assert b"class=\"slide" in deck.data
    assert client.get("/apps/mizalla/").status_code == 302


def test_external_jira_and_teams_links_open_in_new_tabs(app, client):
    with app.app_context():
        organizer = Member(name="Organizer", email="tabs-organizer@example.com")
        organizer.set_password("organizer-password-long")
        project = Project(project_id="TABS-1", name="Tabs project")
        project.memberships.append(ProjectMembership(member=organizer, role="team"))
        db.session.add(project)
        db.session.commit()
        project_id = project.id

    client.post("/apps/login", data={"email": "tabs-organizer@example.com", "password": "organizer-password-long"})
    client.post(
        f"/apps/mizalla/projects/{project_id}/meetings",
        data={
            "meeting_date": "2099-04-03",
            "meeting_time": "14:15",
            "meeting_utc_offset_minutes": "300",
            "agenda": "Future meeting",
        },
    )
    with app.app_context():
        project_page = client.get(f"/apps/mizalla/projects/{project_id}")
        assert b'target="_blank" rel="noopener noreferrer">Open in Teams' in project_page.data

    app.config["JIRA_BASE_URL"] = "https://jira.example.com/browse"
    with app.app_context():
        project = db.session.get(Project, project_id)
        task = Task(project=project, task_type="Development", name="JIRA-linked task", jira_ticket_id="APP-5")
        db.session.add(task)
        db.session.commit()
        project_page = client.get(f"/apps/mizalla/projects/{project_id}")
    assert b'target="_blank" rel="noopener noreferrer">JIRA APP-5' in project_page.data
    # The href must carry the configured base URL, not just render the label.
    assert b'href="https://jira.example.com/browse/APP-5"' in project_page.data
    static_script = client.get("/static/app.js")
    assert b'form.target = "_blank"' in static_script.data
    assert b'form.removeAttribute("target")' in static_script.data


def test_admin_can_open_management_screens_and_server_ip_is_validated(app, client):
    with app.app_context():
        admin = Member(name="Administrator", email="admin@example.com", is_admin=True)
        admin.set_password("another-long-password")
        db.session.add(admin)
        db.session.commit()

    client.post("/apps/login", data={"email": "admin@example.com", "password": "another-long-password"})
    assert client.get("/apps/admin/members").status_code == 200
    assert client.get("/apps/admin/servers").status_code == 200

    invalid = client.post(
        "/apps/admin/servers",
        data={"name": "Bad address", "ip": "not-an-ip", "operating_system": "Linux"},
        follow_redirects=True,
    )
    assert b"valid IP address" in invalid.data
    with app.app_context():
        assert Server.query.count() == 0

    valid = client.post(
        "/apps/admin/servers",
        data={"name": "Test node", "ip": "192.0.2.10", "operating_system": "Linux", "technology_packages": "Python"},
    )
    assert valid.status_code == 302
    with app.app_context():
        assert Server.query.filter_by(name="Test node").one().ip == "192.0.2.10"


def test_project_metadata_meetings_and_task_sources(app, client):
    with app.app_context():
        lead = Member(name="Project lead", email="lead@example.com")
        lead.set_password("project-lead-password")
        attendee = Member(name="Meeting attendee", email="attendee@example.com", password_hash="unused")
        project = Project(project_id="MEET-1", name="Meeting project")
        project.memberships.append(ProjectMembership(member=lead, role="team"))
        other_project = Project(project_id="MEET-2", name="Other project")
        other_project.memberships.append(ProjectMembership(member=lead, role="team"))
        db.session.add_all([lead, attendee, project, other_project])
        db.session.commit()
        project_id = project.id
        attendee_id = attendee.id
        other_project_id = other_project.id

    client.post("/apps/login", data={"email": "lead@example.com", "password": "project-lead-password"})
    updated = client.post(
        f"/apps/mizalla/projects/{project_id}/edit",
        data={
            "project_id": "MEET-1",
            "name": "Meeting project",
            "description": "Description",
            "scope": "Deliver the agreed release.",
            "estimated_completion_date": "2027-04-15",
            "actual_start_date": "2026-09-01",
            "actual_completion_date": "",
        },
    )
    assert updated.status_code == 302

    added_meeting = client.post(
        f"/apps/mizalla/projects/{project_id}/meetings",
        data={
            "meeting_date": "2026-09-20",
            "agenda": "Release readiness",
            "location": "Room 4",
            "notes": "Confirm test coverage.",
            "attendee_ids": [str(attendee_id)],
        },
    )
    assert added_meeting.status_code == 302
    with app.app_context():
        meeting = Meeting.query.filter_by(project_id=project_id).one()
        meeting_id = meeting.id
        assert meeting.attendees[0].id == attendee_id
        updated_project = db.session.get(Project, project_id)
        assert updated_project.scope == "Deliver the agreed release."
        assert updated_project.estimated_completion_date.isoformat() == "2027-04-15"

    created_task = client.post(
        f"/apps/mizalla/projects/{project_id}/tasks",
        data={
            "name": "Implement release gate",
            "task_type": "Development",
            "status": "Not Started",
            "priority": "2",
            "weight": "4",
            "source_type": "Meeting",
            "meeting_id": str(meeting_id),
            "jira_ticket_id": "APP-214",
        },
    )
    assert created_task.status_code == 302
    with app.app_context():
        task = Task.query.filter_by(name="Implement release gate").one()
        assert task.source_type == "Meeting"
        assert task.meeting_id == meeting_id
        assert task.jira_ticket_id == "APP-214"
        task_id = task.id

    app.config["JIRA_BASE_URL"] = "https://work.example.atlassian.net/browse"
    project_page = client.get(f"/apps/mizalla/projects/{project_id}")
    assert b"Add action task" in project_page.data
    assert b"https://work.example.atlassian.net/browse/APP-214" in project_page.data

    client.post(
        f"/apps/mizalla/projects/{project_id}/tasks",
        data={
            "name": "Action created from meeting",
            "task_type": "Testing",
            "status": "Not Started",
            "priority": "5",
            "weight": "5",
            "source_type": "Meeting",
            "meeting_id": str(meeting_id),
        },
    )
    with app.app_context():
        action_task = Task.query.filter_by(name="Action created from meeting").one()
        assert action_task.source_type == "Meeting"
        assert action_task.meeting_id == meeting_id

    rejected_jira = client.post(
        f"/apps/mizalla/projects/{project_id}/tasks",
        data={
            "name": "Invalid JIRA ID",
            "task_type": "Testing",
            "status": "Not Started",
            "priority": "5",
            "weight": "5",
            "jira_ticket_id": "<script>alert(1)</script>",
        },
        follow_redirects=True,
    )
    assert b"Enter a valid JIRA ticket ID" in rejected_jira.data
    with app.app_context():
        assert Task.query.filter_by(name="Invalid JIRA ID").count() == 0

    client.post(
        f"/apps/mizalla/tasks/{task_id}/edit",
        data={
            "name": "Implement release gate",
            "task_type": "Development",
            "status": "In Progress",
            "priority": "2",
            "weight": "4",
            "source_type": "Meeting",
            "meeting_id": str(meeting_id),
            "jira_ticket_id": "APP-214",
        },
    )
    with app.app_context():
        events = ProjectAuditEvent.query.filter_by(project_id=project_id).order_by(ProjectAuditEvent.id).all()
        assert any(event.actor_email == "lead@example.com" and event.entity_type == "Meeting" for event in events)
        assert any(event.entity_type == "Task" and "status Not Started to In Progress" in event.summary for event in events)
    assert b"Activity history" in client.get(f"/apps/mizalla/projects/{project_id}").data

    client.post(
        f"/apps/mizalla/projects/{other_project_id}/meetings",
        data={"meeting_date": "2026-09-21", "agenda": "Other project"},
    )
    with app.app_context():
        other_meeting_id = Meeting.query.filter_by(project_id=other_project_id).one().id
    rejected = client.post(
        f"/apps/mizalla/projects/{project_id}/tasks",
        data={
            "name": "Invalid source link",
            "task_type": "Development",
            "status": "Not Started",
            "priority": "3",
            "weight": "1",
            "source_type": "Meeting",
            "meeting_id": str(other_meeting_id),
        },
    )
    assert rejected.status_code == 302
    with app.app_context():
        assert Task.query.filter_by(name="Invalid source link").count() == 0


def test_project_delete_requires_admin_who_is_also_on_team(app, client):
    with app.app_context():
        member = Member(name="Team member", email="team@example.com")
        member.set_password("team-member-password")
        project = Project(project_id="DEL-1", name="Delete policy")
        project.memberships.append(ProjectMembership(member=member, role="team"))
        db.session.add(project)
        db.session.commit()
        project_id = project.id

    client.post("/apps/login", data={"email": "team@example.com", "password": "team-member-password"})
    assert client.get(f"/apps/mizalla/projects/{project_id}/edit").status_code == 200
    assert client.post(f"/apps/mizalla/projects/{project_id}/delete").status_code == 403

    with app.app_context():
        member = Member.query.filter_by(email="team@example.com").one()
        member.is_admin = True
        db.session.commit()

    assert client.post(f"/apps/mizalla/projects/{project_id}/delete").status_code == 302
    with app.app_context():
        assert db.session.get(Project, project_id) is None
        deletion = ProjectAuditEvent.query.filter_by(project_key="DEL-1", entity_type="Project", action="DELETE").one()
        assert deletion.project_id is None
        assert deletion.actor_email == "team@example.com"


def test_legacy_sqlite_schema_upgrade_preserves_existing_records(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE project (
            id INTEGER PRIMARY KEY,
            project_id VARCHAR(40) NOT NULL UNIQUE,
            name VARCHAR(180) NOT NULL,
            description TEXT NOT NULL,
            test_server_id INTEGER,
            production_server_id INTEGER,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE task (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            task_type VARCHAR(30) NOT NULL,
            name VARCHAR(180) NOT NULL,
            status VARCHAR(30) NOT NULL,
            due_date DATE,
            priority INTEGER NOT NULL,
            weight INTEGER NOT NULL
        );
        INSERT INTO project VALUES (1, 'OLD-1', 'Existing project', '', NULL, NULL, '2026-01-01');
        INSERT INTO task VALUES (1, 1, 'Development', 'Existing task', 'Not Started', NULL, 5, 2);
        """
    )
    connection.commit()
    connection.close()

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "migration-test",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database_path}",
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        }
    )
    with app.app_context():
        project = db.session.get(Project, 1)
        task = db.session.get(Task, 1)
        assert project.name == "Existing project"
        assert project.scope == ""
        assert project.estimated_completion_date is None
        assert task.name == "Existing task"
        assert task.source_type == "Project Plan"
        assert task.meeting_id is None
        assert task.jira_ticket_id is None


def test_jira_ticket_id_validation_and_safe_url_construction():
    assert is_valid_jira_ticket_id("APP-214")
    assert not is_valid_jira_ticket_id("<script>alert(1)</script>")
    assert jira_ticket_url("APP-214", "https://jira.example.com/browse") == "https://jira.example.com/browse/APP-214"
    assert jira_ticket_url("APP-214", "javascript:alert(1)") is None
    assert jira_ticket_url("APP-214", "https://user:pass@jira.example.com/browse") is None


def test_local_secret_key_is_generated_once_and_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    instance_path = tmp_path / "instance"

    first_key = _load_or_create_secret_key(str(instance_path))
    second_key = _load_or_create_secret_key(str(instance_path))

    assert len(first_key) == 64
    assert first_key == second_key
    assert (instance_path / ".secret_key").read_text(encoding="utf-8") == first_key

    monkeypatch.setenv("SECRET_KEY", "deployment-secret-from-environment")
    assert _load_or_create_secret_key(str(instance_path)) == "deployment-secret-from-environment"


def test_future_meeting_redirects_to_prefilled_teams_compose_link(app, client):
    with app.app_context():
        organizer = Member(name="Organizer", email="organizer@example.com")
        organizer.set_password("organizer-password-long")
        attendee = Member(name="Invitee", email="invitee@example.com", password_hash="unused")
        project = Project(project_id="TEAMS-1", name="Teams project")
        project.memberships.append(ProjectMembership(member=organizer, role="team"))
        db.session.add_all([organizer, attendee, project])
        db.session.commit()
        project_id = project.id
        attendee_id = attendee.id

    client.post("/apps/login", data={"email": "organizer@example.com", "password": "organizer-password-long"})
    response = client.post(
        f"/apps/mizalla/projects/{project_id}/meetings",
        data={
            "meeting_date": "2099-04-03",
            "meeting_time": "14:15",
            "meeting_utc_offset_minutes": "300",
            "agenda": "Review roadmap & risks",
            "location": "Room 2",
            "attendee_ids": [str(attendee_id)],
        },
    )
    assert response.status_code == 302
    assert response.location.startswith("https://teams.microsoft.com/l/meeting/new?")
    parameters = parse_qs(urlsplit(response.location).query)
    assert parameters["subject"] == ["Teams project meeting"]
    assert parameters["content"] == ["Review roadmap & risks"]
    assert parameters["startTime"] == ["2099-04-03T14:15:00"]
    assert parameters["endTime"] == ["2099-04-03T14:45:00"]
    assert parameters["attendees"] == ["invitee@example.com"]

    with app.app_context():
        meeting = Meeting.query.filter_by(project_id=project_id).one()
        meeting_id = meeting.id
        assert meeting.meeting_start_at.isoformat(sep=" ") == "2099-04-03 14:15:00"
        assert meeting.meeting_utc_offset_minutes == 300

    reopen = client.get(f"/apps/mizalla/projects/{project_id}/meetings/{meeting_id}/open-teams")
    assert reopen.status_code == 302
    assert reopen.location == response.location


def test_past_meeting_does_not_redirect_to_teams(app, client):
    with app.app_context():
        organizer = Member(name="Organizer", email="past-organizer@example.com")
        organizer.set_password("organizer-password-long")
        project = Project(project_id="TEAMS-PAST", name="Past project")
        project.memberships.append(ProjectMembership(member=organizer, role="team"))
        db.session.add_all([organizer, project])
        db.session.commit()
        project_id = project.id

    client.post("/apps/login", data={"email": "past-organizer@example.com", "password": "organizer-password-long"})
    response = client.post(
        f"/apps/mizalla/projects/{project_id}/meetings",
        data={
            "meeting_date": "2020-04-03",
            "meeting_time": "14:15",
            "meeting_utc_offset_minutes": "300",
            "agenda": "Past meeting",
        },
    )
    assert response.status_code == 302
    assert "teams.microsoft.com" not in response.location
    assert response.location.endswith(f"/apps/mizalla/projects/{project_id}")