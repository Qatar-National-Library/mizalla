import re
from datetime import date, timedelta
from urllib.parse import urlsplit

from mizalla import create_app
from mizalla.email import SENT_EMAILS_LOG, send_email
from mizalla.extensions import db
from mizalla.jobs import run_all_scheduled_jobs
from mizalla.models import Meeting, Member, Project, ProjectMembership, Task, TaskOwner
from mizalla.settings import get_project_team_roles_list, get_setting, seed_default_settings, set_setting


def test_settings_crud_and_defaults(tmp_path):
    db_path = tmp_path / "test_settings.db"
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
    })

    with app.app_context():
        seed_default_settings()

        # Check default values
        assert get_setting("jira_base_url") == "https://jira.qnl.qa/"
        assert "Project Manager" in get_setting("project_team_roles")

        roles_list = get_project_team_roles_list()
        assert "Project Manager" in roles_list
        assert "Developer/Implementer" in roles_list

        # Update setting
        set_setting("jira_base_url", "https://custom-jira.company.com/")
        assert get_setting("jira_base_url") == "https://custom-jira.company.com/"

        # Add custom role
        set_setting("project_team_roles", "Project Manager, Core Developer, Security Auditor")
        updated_roles = get_project_team_roles_list()
        assert updated_roles == ["Project Manager", "Core Developer", "Security Auditor"]


def test_send_email_logging_fallback(tmp_path, monkeypatch):
    db_path = tmp_path / "test_email.db"
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
    })
    # create_app loads the real .env, so the developer's key has to be cleared
    # explicitly. Left in place, this test would POST to the live SendGrid API.
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)

    with app.app_context():
        SENT_EMAILS_LOG.clear()
        success = send_email("pm@example.com", "Test Subject", "<p>Test Content</p>")
        assert success is True
        assert len(SENT_EMAILS_LOG) == 1
        record = SENT_EMAILS_LOG[0]
        assert record["to"] == "pm@example.com"
        assert record["subject"] == "Test Subject"
        assert record["sent_via_sendgrid"] is False


def test_scheduled_email_jobs(tmp_path):
    db_path = tmp_path / "test_jobs.db"
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
    })

    with app.app_context():
        seed_default_settings()
        SENT_EMAILS_LOG.clear()

        # Create members
        pm = Member(name="PM User", email="pm@test.com", is_admin=True)
        pm.set_password("Password123456")
        dev = Member(name="Dev User", email="dev@test.com")
        dev.set_password("Password123456")
        db.session.add_all([pm, dev])
        db.session.commit()

        # Create project and membership
        project = Project(
            project_id="PROJ-JOB-1",
            name="Job Test Project",
            description="Testing email jobs",
        )
        db.session.add(project)
        db.session.commit()

        pm_membership = ProjectMembership(
            project=project, member=pm, role="team", team_roles="Project Manager"
        )
        dev_membership = ProjectMembership(
            project=project, member=dev, role="team", team_roles="Developer/Implementer"
        )
        db.session.add_all([pm_membership, dev_membership])
        db.session.commit()

        # Create tasks: one due soon, one stale, one overdue
        today = date.today()
        task_due_soon = Task(
            project=project,
            task_type="Development",
            name="Due Soon Task",
            status="In Progress",
            due_date=today + timedelta(days=1),
        )
        task_overdue = Task(
            project=project,
            task_type="Testing",
            name="Overdue Task",
            status="In Progress",
            due_date=today - timedelta(days=2),
        )
        db.session.add_all([task_due_soon, task_overdue])
        db.session.commit()

        db.session.add_all([
            TaskOwner(task=task_due_soon, member=dev),
            TaskOwner(task=task_overdue, member=dev),
        ])
        db.session.commit()

        # Create upcoming meeting
        meeting = Meeting(
            project=project,
            meeting_date=today + timedelta(days=1),
            agenda="Sprint Review",
            location="Room 101",
            attendees=[pm, dev],
        )
        db.session.add(meeting)
        db.session.commit()

        # Run scheduled jobs
        results = run_all_scheduled_jobs()
        assert results["pm_status_reports"] >= 1
        assert results["task_due_reminders"] >= 1
        assert results["stale_task_nudges"] >= 2  # Active tasks without audit events
        assert results["overdue_task_alerts"] >= 1
        assert results["upcoming_meeting_reminders"] >= 2  # 2 attendees

        # Verify email logs
        subjects = [e["subject"] for e in SENT_EMAILS_LOG]
        assert any("Project Status Report" in s for s in subjects)
        assert any("Task Due Soon" in s for s in subjects)
        assert any("You've got this!" in s for s in subjects)
        assert any("Overdue Alert" in s for s in subjects)
        assert any("Meeting Reminder" in s for s in subjects)

        # Check links in sent emails
        for email in SENT_EMAILS_LOG:
            assert "http://127.0.0.1:5000/apps/mizalla/projects" in email["html_body"]

        # Every emailed link must resolve against a real route. A substring
        # check alone let broken links (wrong project id, dead /projects path)
        # pass, so match each href against the url map and follow its fragment.
        adapter = app.url_map.bind("127.0.0.1")
        checked = 0
        for email in SENT_EMAILS_LOG:
            for href in re.findall(r'href="(http://127\.0\.0\.1:5000[^"]*)"', email["html_body"]):
                parts = urlsplit(href)
                assert adapter.match(parts.path) is not None, f"Dead link in email: {href}"
                # A task anchor must point at a record that exists.
                if parts.fragment:
                    entity_id = int(parts.fragment.rsplit("-", 1)[1])
                    assert db.session.get(Project, entity_id) or db.session.get(
                        Task, entity_id
                    ), f"Link points at a missing record: {href}"
                checked += 1
        assert checked >= 10, f"Expected the job emails to carry many links, matched {checked}"


def test_admin_settings_web_ui(tmp_path):
    db_path = tmp_path / "test_ui.db"
    app = create_app({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
    })

    with app.app_context():
        admin = Member(name="Admin", email="admin@ui.com", is_admin=True)
        admin.set_password("Password123456")
        user = Member(name="Regular", email="user@ui.com", is_admin=False)
        user.set_password("Password123456")
        db.session.add_all([admin, user])
        db.session.commit()

    client = app.test_client()

    # Non-authenticated redirect to login
    resp = client.get("/apps/admin/settings")
    assert resp.status_code == 302

    # Regular user gets 403
    client.post("/apps/login", data={"email": "user@ui.com", "password": "Password123456"})
    resp = client.get("/apps/admin/settings")
    assert resp.status_code == 403

    # Admin access settings page
    client.post("/apps/login", data={"email": "admin@ui.com", "password": "Password123456"})
    resp = client.get("/apps/admin/settings")
    assert resp.status_code == 200
    assert b"System Settings" in resp.data
    assert b"jira_base_url" in resp.data

    # POST to update setting
    resp = client.post(
        "/apps/admin/settings",
        data={"jira_base_url": "https://new-jira.company.com/", "app_base_url": "https://projects.company.com"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"Settings saved successfully" in resp.data

    with app.app_context():
        assert get_setting("jira_base_url") == "https://new-jira.company.com/"
        assert get_setting("app_base_url") == "https://projects.company.com"

    # POST run-jobs trigger
    resp = client.post("/apps/admin/settings/run-jobs", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Scheduled jobs ran successfully" in resp.data
