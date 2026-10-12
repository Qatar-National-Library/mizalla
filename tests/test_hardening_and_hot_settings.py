"""Regression tests for the hardening and hot-settings pass.

Covers: API-key-only authentication, bounded API input, hot setting resolution,
member-picker scoping, PM digest recipient rules, and ad-hoc meeting attendees.
"""

import re
from datetime import date, timedelta

import pytest

from mizalla.extensions import db
from mizalla.jobs import (
    run_all_scheduled_jobs,
    send_pm_status_reports,
    send_upcoming_meeting_reminders,
)
from mizalla.email import SENT_EMAILS_LOG
from mizalla.models import JobRunLog, Meeting, Member, Project, ProjectMembership, Task, TaskStep
from mizalla.settings import get_project_team_roles_list, get_setting, seed_default_settings, set_setting
from mizalla.validators import bounded_int, is_valid_email, optional_date, parse_email_list


# --------------------------------------------------------------------------- #
# Shared validators
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [(5, 5), ("5", 5), (5.0, 5), (" 7 ", 7), (1, 1), (10, 10)],
)
def test_bounded_int_accepts_valid_input(value, expected):
    assert bounded_int(value, "priority", 1, 10) == expected


@pytest.mark.parametrize("value", [0, 11, -1, "abc", "", None, True, False, "1e3", 99999999999])
def test_bounded_int_rejects_out_of_range_and_junk(value):
    with pytest.raises(ValueError):
        bounded_int(value, "priority", 1, 10)


def test_optional_date_handles_empty_and_junk():
    assert optional_date("", "Due date") is None
    assert optional_date(None, "Due date") is None
    assert optional_date("2026-03-04", "Due date") == date(2026, 3, 4)
    with pytest.raises(ValueError, match="valid date"):
        optional_date("not-a-date", "Due date")
    with pytest.raises(ValueError, match="valid date"):
        optional_date("2026-13-45", "Due date")


def test_email_helpers():
    assert is_valid_email("someone@example.com")
    assert not is_valid_email("")
    assert not is_valid_email("no-at-sign")
    assert not is_valid_email("two@at@sign.com")
    assert not is_valid_email("spaces in@example.com")
    assert not is_valid_email("@example.com")
    assert not is_valid_email("user@")

    valid, invalid = parse_email_list("A@Example.com, bad, b@example.com; a@example.com")
    assert valid == ["a@example.com", "b@example.com"]
    assert invalid == ["bad"]


# --------------------------------------------------------------------------- #
# API authentication: API key only
# --------------------------------------------------------------------------- #


def test_api_rejects_session_cookie_authentication(app, client):
    """A logged-in session must not grant access to the CSRF-exempt API."""
    with app.app_context():
        team = Member(name="Session User", email="session@example.com")
        team.set_password("session-password-long")
        project = Project(project_id="SESSION-1", name="Session project")
        project.memberships.append(ProjectMembership(member=team, role="team"))
        db.session.add_all([team, project])
        db.session.commit()

    client.post("/apps/login", data={"email": "session@example.com", "password": "session-password-long"})
    assert client.get("/apps/mizalla/").status_code == 200

    response = client.get("/apps/mizalla/api/v1/projects")
    assert response.status_code == 401

    write = client.post(
        "/apps/mizalla/api/v1/projects",
        json={"project_id": "SESSION-2", "name": "Injected"},
    )
    assert write.status_code == 401
    with app.app_context():
        assert Project.query.filter_by(project_id="SESSION-2").first() is None


def test_api_rejects_blank_credentials_without_error(app, client):
    assert client.get("/apps/mizalla/api/v1/projects", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/apps/mizalla/api/v1/projects", headers={"X-API-Key": "   "}).status_code == 401
    assert client.get("/apps/mizalla/api/v1/projects", headers={"Authorization": "Basic abc"}).status_code == 401


# --------------------------------------------------------------------------- #
# API input bounds
# --------------------------------------------------------------------------- #


@pytest.fixture
def api_client(app):
    with app.app_context():
        user = Member(name="API Bound", email="bounds@example.com")
        user.set_password("bounds-password-long")
        key = user.generate_api_key()
        project = Project(project_id="BOUND-1", name="Bounds project")
        project.memberships.append(ProjectMembership(member=user, role="team"))
        db.session.add_all([user, project])
        db.session.commit()
        project_id = project.id

    client = app.test_client()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return client, project_id, headers


@pytest.mark.parametrize("field,value", [("priority", 0), ("priority", 11), ("priority", "high"), ("weight", 0), ("weight", 11)])
def test_api_create_task_bounds_priority_and_weight(api_client, field, value):
    client, project_id, headers = api_client
    response = client.post(
        f"/apps/mizalla/api/v1/projects/{project_id}/tasks",
        headers=headers,
        json={"name": "Out of range", "task_type": "Development", "status": "Not Started", field: value},
    )
    assert response.status_code == 400
    assert "error" in response.get_json()
    with client.application.app_context():
        assert Task.query.filter_by(name="Out of range").first() is None


def test_api_create_task_rejects_boolean_priority(api_client):
    client, project_id, headers = api_client
    response = client.post(
        f"/apps/mizalla/api/v1/projects/{project_id}/tasks",
        headers=headers,
        json={"name": "Bool priority", "task_type": "Development", "status": "Not Started", "priority": True},
    )
    assert response.status_code == 400


def test_api_step_bounds_percentage_and_priority(api_client):
    client, project_id, headers = api_client
    task_id = client.post(
        f"/apps/mizalla/api/v1/projects/{project_id}/tasks",
        headers=headers,
        json={"name": "Bounds task", "task_type": "Development", "status": "Not Started"},
    ).get_json()["id"]

    for payload in (
        {"header": "Over", "percentage_complete": 101},
        {"header": "Under", "percentage_complete": -5},
        {"header": "Zero priority", "priority": 0},
        {"header": "Junk", "percentage_complete": "half"},
    ):
        assert client.post(f"/apps/mizalla/api/v1/tasks/{task_id}/steps", headers=headers, json=payload).status_code == 400


def test_api_step_bounds_on_patch(api_client):
    client, project_id, headers = api_client
    task_id = client.post(
        f"/apps/mizalla/api/v1/projects/{project_id}/tasks",
        headers=headers,
        json={"name": "Patch bounds", "task_type": "Development", "status": "Not Started"},
    ).get_json()["id"]
    step_id = client.post(f"/apps/mizalla/api/v1/tasks/{task_id}/steps", headers=headers, json={"header": "Step"}).get_json()["id"]

    assert client.patch(f"/apps/mizalla/api/v1/steps/{step_id}", headers=headers, json={"percentage_complete": 250}).status_code == 400
    assert client.patch(f"/apps/mizalla/api/v1/steps/{step_id}", headers=headers, json={"priority": -1}).status_code == 400
    assert client.patch(f"/apps/mizalla/api/v1/steps/{step_id}", headers=headers, json={"header": "   "}).status_code == 400
    with client.application.app_context():
        step = db.session.get(TaskStep, step_id)
        assert step.percentage_complete == 0
        assert step.header == "Step"


def test_api_update_task_rejects_empty_name(api_client):
    client, project_id, headers = api_client
    task_id = client.post(
        f"/apps/mizalla/api/v1/projects/{project_id}/tasks",
        headers=headers,
        json={"name": "Keeps its name", "task_type": "Development", "status": "Not Started"},
    ).get_json()["id"]

    assert client.patch(f"/apps/mizalla/api/v1/tasks/{task_id}", headers=headers, json={"name": "   "}).status_code == 400
    with client.application.app_context():
        assert db.session.get(Task, task_id).name == "Keeps its name"


def test_api_rejects_malformed_dates(api_client):
    client, _project_id, headers = api_client
    response = client.post(
        "/apps/mizalla/api/v1/projects",
        headers=headers,
        json={"project_id": "BOUND-2", "name": "Bad date", "estimated_completion_date": "31-12-2026"},
    )
    assert response.status_code == 400
    assert "valid date" in response.get_json()["error"]


# --------------------------------------------------------------------------- #
# Hot settings
# --------------------------------------------------------------------------- #


def test_setting_resolution_order_is_db_then_config_then_default(tmp_path):
    from mizalla import create_app

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'settings.db'}",
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "JIRA_BASE_URL": "https://from-config.example/",
            "PROJECT_TEAM_ROLES": "Project Manager, Config Only Role",
        }
    )
    with app.app_context():
        # No database row yet: the boot config value is used, not the built-in default.
        assert get_setting("jira_base_url") == "https://from-config.example/"
        assert "Config Only Role" in get_project_team_roles_list()

        # Seeding records the resolved value rather than the built-in default.
        seed_default_settings()
        assert get_setting("jira_base_url") == "https://from-config.example/"

        # A database override wins over the boot config.
        set_setting("jira_base_url", "https://from-db.example/")
        assert get_setting("jira_base_url") == "https://from-db.example/"

        # A key with no config and no row falls back to the built-in default.
        assert get_setting("email_status_report_days") == "7"


def test_team_role_setting_change_is_honoured_without_restart(app, client):
    with app.app_context():
        editor = Member(name="Role Editor", email="editor@example.com")
        editor.set_password("editor-password-long")
        newcomer = Member(name="Newcomer", email="newcomer@example.com", password_hash="unused")
        project = Project(project_id="HOT-1", name="Hot roles project")
        # Authority over a project comes from a role on that project, not from the
        # member's global is_pm flag, so the editor needs the PM team role here.
        editor_membership = ProjectMembership(member=editor, role="team")
        editor_membership.set_team_roles_list(["Project Manager"])
        project.memberships.append(editor_membership)
        db.session.add_all([editor, newcomer, project])
        db.session.commit()
        project_id = project.id
        newcomer_id = newcomer.id

    client.post("/apps/login", data={"email": "editor@example.com", "password": "editor-password-long"})
    assert b"Brand New Role" not in client.get(f"/apps/mizalla/projects/{project_id}").data

    with app.app_context():
        set_setting("project_team_roles", "Project Manager, Brand New Role")

    # The form reads the hot setting, so the new role appears with no restart.
    assert b"Brand New Role" in client.get(f"/apps/mizalla/projects/{project_id}").data

    client.post(
        f"/apps/mizalla/projects/{project_id}/memberships",
        data={"member_id": str(newcomer_id), "role": "team", "team_roles": ["Brand New Role", "Not A Role"]},
    )
    with app.app_context():
        membership = ProjectMembership.query.filter_by(member_id=newcomer_id, role="team").one()
        # The unconfigured role is dropped, the hot-configured one is kept.
        assert membership.get_team_roles_list() == ["Brand New Role"]


def test_jira_link_uses_hot_setting(app, client):
    with app.app_context():
        member = Member(name="Jira User", email="jira@example.com")
        member.set_password("jira-password-long")
        project = Project(project_id="JIRA-1", name="Jira project")
        project.memberships.append(ProjectMembership(member=member, role="team"))
        task = Task(project=project, task_type="Development", name="Linked task", status="Not Started", jira_ticket_id="ABC-1")
        db.session.add_all([member, project, task])
        db.session.commit()
        project_id = project.id

    client.post("/apps/login", data={"email": "jira@example.com", "password": "jira-password-long"})
    with app.app_context():
        set_setting("jira_base_url", "https://hot-jira.example/")

    body = client.get(f"/apps/mizalla/projects/{project_id}").data
    assert b'href="https://hot-jira.example/ABC-1"' in body


# --------------------------------------------------------------------------- #
# Member picker scoping
# --------------------------------------------------------------------------- #


def test_meeting_attendee_picker_hides_unrelated_members(app, client):
    """A user who sees one project must not learn about the whole member directory."""
    with app.app_context():
        lead = Member(name="Scoped Lead", email="scoped@example.com")
        lead.set_password("scoped-password-long")
        insider = Member(name="Insider Only", email="insider@example.com", password_hash="unused")
        outsider = Member(name="Other Project Only", email="other@example.com", password_hash="unused")

        visible = Project(project_id="SCOPE-1", name="Visible project")
        visible.memberships.append(ProjectMembership(member=lead, role="team"))
        visible.memberships.append(ProjectMembership(member=insider, role="client"))

        hidden = Project(project_id="SCOPE-2", name="Hidden project")
        hidden.memberships.append(ProjectMembership(member=outsider, role="team"))
        db.session.add_all([lead, insider, outsider, visible, hidden])
        db.session.commit()
        project_id = visible.id
        lead_id = lead.id
        insider_id = insider.id

    client.post("/apps/login", data={"email": "scoped@example.com", "password": "scoped-password-long"})

    page = client.get(f"/apps/mizalla/projects/{project_id}").data
    assert b"insider@example.com" in page
    assert b"other@example.com" not in page

    # The edit form is scoped the same way, and only posts the scoped members.
    with app.app_context():
        meeting = Meeting(
            project=db.session.get(Project, project_id),
            meeting_date=date.today() + timedelta(days=1),
            agenda="Scope check",
            attendees=[db.session.get(Member, insider_id)],
        )
        db.session.add(meeting)
        db.session.commit()
        meeting_id = meeting.id

    form = client.get(f"/apps/mizalla/projects/{project_id}/meetings/{meeting_id}/edit").data
    assert b"insider@example.com" in form
    assert b"other@example.com" not in form
    # On edit only the current attendees are pre-checked, so an unrelated save
    # never bulk-adds the whole directory.
    assert re.search(rb'name="attendee_ids" value="%d" checked' % insider_id, form)
    assert not re.search(rb'name="attendee_ids" value="%d" checked' % lead_id, form)


# --------------------------------------------------------------------------- #
# PM status digest
# --------------------------------------------------------------------------- #


def _project_with_pm(team_roles: str, member: Member) -> Project:
    project = Project(project_id="PM-X", name="PM digest project")
    project.memberships.append(ProjectMembership(member=member, role="team", team_roles=team_roles))
    return project


def test_pm_digest_skips_projects_without_a_project_manager(app):
    with app.app_context():
        seed_default_settings()
        SENT_EMAILS_LOG.clear()
        lead = Member(name="No PM", email="nopm@example.com")
        lead.set_password("no-pm-password-long")
        project = _project_with_pm("Developer/Implementer", lead)
        db.session.add_all([lead, project])
        db.session.commit()

        assert send_pm_status_reports() == 0
        assert SENT_EMAILS_LOG == []


def test_pm_digest_goes_only_to_project_managers(app):
    with app.app_context():
        seed_default_settings()
        SENT_EMAILS_LOG.clear()
        pm = Member(name="The PM", email="pm-only@example.com")
        pm.set_password("pm-only-password-long")
        dev = Member(name="Just Dev", email="dev-only@example.com", password_hash="unused")
        project = Project(project_id="PM-Y", name="PM digest roles")
        project.memberships.append(ProjectMembership(member=pm, role="team", team_roles="Project Manager"))
        project.memberships.append(ProjectMembership(member=dev, role="team", team_roles="Developer/Implementer"))
        db.session.add_all([pm, dev, project])
        db.session.commit()

        assert send_pm_status_reports() == 1
        assert [record["to"] for record in SENT_EMAILS_LOG] == ["pm-only@example.com"]


def test_pm_digest_respects_the_configured_interval(app):
    with app.app_context():
        seed_default_settings()
        SENT_EMAILS_LOG.clear()
        set_setting("email_status_report_days", "3")
        pm = Member(name="Interval PM", email="interval-pm@example.com")
        pm.set_password("interval-pm-password")
        project = _project_with_pm("Project Manager", pm)
        db.session.add_all([pm, project])
        db.session.commit()

        assert send_pm_status_reports() == 1
        # A second run on the same day is suppressed by the interval.
        assert send_pm_status_reports() == 0
        assert len(SENT_EMAILS_LOG) == 1

        # Backdating the last run past the interval re-opens the digest.
        log = db.session.scalar(db.select(JobRunLog))
        log.created_at = datetime_now(days_ago=5)
        db.session.commit()
        assert send_pm_status_reports() == 1
        assert len(SENT_EMAILS_LOG) == 2

        # A zero interval always re-sends.
        set_setting("email_status_report_days", "0")
        assert send_pm_status_reports() == 1


def datetime_now(days_ago: int):
    from datetime import datetime, timezone

    return datetime.now(timezone.utc) - timedelta(days=days_ago)


def test_pm_digest_completion_matches_the_project_page(app):
    """The digest reports the step-weighted figure, not completed-task count."""
    with app.app_context():
        seed_default_settings()
        SENT_EMAILS_LOG.clear()
        pm = Member(name="Weight PM", email="weight-pm@example.com")
        pm.set_password("weight-pm-password-long")
        project = _project_with_pm("Project Manager", pm)
        task = Task(project=project, task_type="Development", name="Half done", status="In Progress", weight=4)
        task.steps.append(TaskStep(priority=1, header="Step one", percentage_complete=50))
        db.session.add_all([pm, project, task])
        db.session.commit()

        from mizalla.progress import project_completion

        assert send_pm_status_reports() == 1
        body = SENT_EMAILS_LOG[0]["html_body"]
        # The step-weighted figure is reported, not the completed-task count
        # (which would be 0 here, since no task is "Completed").
        assert f"<strong>{project_completion([task])}%</strong>" in body


# --------------------------------------------------------------------------- #
# Ad-hoc meeting attendees
# --------------------------------------------------------------------------- #


def test_meeting_stores_ad_hoc_attendees_and_normalises_them(app, client):
    with app.app_context():
        lead = Member(name="Meeting Lead", email="meeting-lead@example.com")
        lead.set_password("meeting-lead-password")
        project = Project(project_id="MEET-1", name="Meeting project")
        project.memberships.append(ProjectMembership(member=lead, role="team"))
        db.session.add_all([lead, project])
        db.session.commit()
        project_id = project.id
        lead_id = lead.id

    client.post("/apps/login", data={"email": "meeting-lead@example.com", "password": "meeting-lead-password"})

    client.post(
        f"/apps/mizalla/projects/{project_id}/meetings",
        data={
            "meeting_date": "2099-01-02",
            "agenda": "Ad hoc review",
            "attendee_ids": [str(lead_id)],
            "external_attendee_emails": "Consultant@Example.com , consultant@example.com, auditor@example.com",
        },
    )
    with app.app_context():
        meeting = Meeting.query.filter_by(project_id=project_id).one()
        # Lowercased and de-duplicated.
        assert meeting.get_external_attendee_list() == ["consultant@example.com", "auditor@example.com"]

    body = client.get(f"/apps/mizalla/projects/{project_id}").data
    assert b"consultant@example.com" in body
    assert b"auditor@example.com" in body

    # The create form offers members pre-checked plus the ad-hoc field.
    assert re.search(rb'name="attendee_ids" value="%d" checked' % lead_id, body)
    assert b'name="external_attendee_emails"' in body


def test_meeting_rejects_invalid_ad_hoc_addresses(app, client):
    with app.app_context():
        lead = Member(name="Bad Address Lead", email="bad-address@example.com")
        lead.set_password("bad-address-password")
        project = Project(project_id="MEET-2", name="Bad address project")
        project.memberships.append(ProjectMembership(member=lead, role="team"))
        db.session.add_all([lead, project])
        db.session.commit()
        project_id = project.id

    client.post("/apps/login", data={"email": "bad-address@example.com", "password": "bad-address-password"})
    response = client.post(
        f"/apps/mizalla/projects/{project_id}/meetings",
        data={"meeting_date": "2099-01-02", "agenda": "Bad", "external_attendee_emails": "not-an-email"},
        follow_redirects=True,
    )
    assert b"not valid email addresses" in response.data
    with app.app_context():
        assert Meeting.query.filter_by(project_id=project_id).count() == 0


def test_meeting_reminders_reach_ad_hoc_attendees(app):
    with app.app_context():
        seed_default_settings()
        SENT_EMAILS_LOG.clear()
        set_setting("email_upcoming_meeting_hours", "72")
        lead = Member(name="Reminder Lead", email="reminder-lead@example.com")
        lead.set_password("reminder-lead-password")
        project = Project(project_id="MEET-3", name="Reminder project")
        project.memberships.append(ProjectMembership(member=lead, role="team"))
        meeting = Meeting(
            project=project,
            meeting_date=date.today() + timedelta(days=2),
            agenda="Reminder agenda",
            attendees=[lead],
        )
        meeting.set_external_attendee_list(["guest@example.com"])
        db.session.add_all([lead, project, meeting])
        db.session.commit()

        assert send_upcoming_meeting_reminders() == 2
        assert sorted(record["to"] for record in SENT_EMAILS_LOG) == [
            "guest@example.com",
            "reminder-lead@example.com",
        ]
        # The member keeps their name; the ad-hoc invitee is addressed directly.
        guest_email = next(r for r in SENT_EMAILS_LOG if r["to"] == "guest@example.com")
        assert "Hello <strong>guest@example.com</strong>" in guest_email["html_body"]


def test_meeting_reminders_do_not_duplicate_an_address(app):
    with app.app_context():
        seed_default_settings()
        SENT_EMAILS_LOG.clear()
        set_setting("email_upcoming_meeting_hours", "72")
        lead = Member(name="Dup Lead", email="dup@example.com")
        lead.set_password("dup-lead-password")
        project = Project(project_id="MEET-4", name="Duplicate project")
        project.memberships.append(ProjectMembership(member=lead, role="team"))
        meeting = Meeting(project=project, meeting_date=date.today() + timedelta(days=2), agenda="Dup")
        meeting.attendees = [lead]
        meeting.set_external_attendee_list(["DUP@example.com"])
        db.session.add_all([lead, project, meeting])
        db.session.commit()

        assert send_upcoming_meeting_reminders() == 1


def test_teams_link_includes_ad_hoc_attendees(app):
    with app.app_context():
        from datetime import datetime, time

        lead = Member(name="Teams Lead", email="teams-lead@example.com")
        lead.set_password("teams-lead-password")
        project = Project(project_id="MEET-5", name="Teams project")
        project.memberships.append(ProjectMembership(member=lead, role="team"))
        meeting = Meeting(
            project=project,
            meeting_date=date.today() + timedelta(days=30),
            meeting_start_at=datetime.combine(date.today() + timedelta(days=30), time(9, 30)),
            meeting_utc_offset_minutes=0,
            agenda="Teams agenda",
            attendees=[lead],
        )
        meeting.set_external_attendee_list(["guest@example.com"])
        db.session.add_all([lead, project, meeting])
        db.session.commit()

        from mizalla.teams import create_teams_meeting_url

        url = create_teams_meeting_url(project, meeting)
        assert url is not None
        assert "attendees=teams-lead%40example.com%2Cguest%40example.com" in url


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_run_scheduled_jobs_cli_command_exists(app):
    assert "run-scheduled-jobs" in app.cli.commands
    assert "create-admin" in app.cli.commands


def test_run_all_scheduled_jobs_reports_every_job(app):
    with app.app_context():
        results = run_all_scheduled_jobs()
    assert set(results) == {
        "pm_status_reports",
        "task_due_reminders",
        "stale_task_nudges",
        "overdue_task_alerts",
        "upcoming_meeting_reminders",
    }


def test_mcp_fallback_app_is_built_lazily_and_memoised(tmp_path, monkeypatch):
    """The MCP fallback app must not be built at import time, and must be built once.

    Importing mizalla_mcp used to call create_app() at module scope, which ran
    db.create_all() and upgrade_schema() against instance/mizalla.db. Because the
    test suite imports the module, simply running pytest created and migrated the real
    database. create_app is patched to a temp-backed app so this test cannot recreate it.
    """
    import mizalla_mcp
    from flask import has_app_context
    from mizalla import create_app as real_create_app

    created = []

    def fake_create_app(test_config=None):
        created.append(test_config)
        return real_create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-key",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'lazy.db'}",
                "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            }
        )

    monkeypatch.setattr(mizalla_mcp, "create_app", fake_create_app)
    monkeypatch.setattr(mizalla_mcp, "_default_app", None)

    # Nothing is built until it is actually needed.
    assert created == []

    fallback = mizalla_mcp._get_default_app()
    assert len(created) == 1
    assert mizalla_mcp._get_default_app() is fallback
    assert len(created) == 1

    # The stdio MCP server has no Flask app context, so every tool call goes through
    # this branch. It must yield a context with a working database session.
    assert not has_app_context()
    with mizalla_mcp._app_context():
        assert has_app_context()
        assert db.session.execute(db.text("select 1")).scalar() == 1

    with fallback.app_context():
        # Inside a context already, the fallback is not consulted again.
        assert len(created) == 1
        assert has_app_context()


def test_fresh_import_of_mcp_module_builds_no_app():
    """A clean interpreter must import mizalla_mcp without constructing an app.

    Runs in a subprocess so the assertion sees a genuinely un-imported module, rather
    than the copy already cached in this test process.
    """
    import pathlib
    import subprocess
    import sys

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mizalla_mcp;"
            "print('DEFAULT_APP', mizalla_mcp._default_app)",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "DEFAULT_APP None" in result.stdout

