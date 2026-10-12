"""Tests for task highlights, milestones, and the project summary screen.

The existing suite asserts on status codes and page text but never rendered most
templates, which let a template that referenced an unimported Jinja macro pass
73 green tests. ``test_every_screen_renders_with_a_breadcrumb`` walks the whole
URL map to stop that recurring.
"""

import re
from datetime import date, timedelta

import pytest

from mizalla.extensions import db
from mizalla.highlights import apply_milestone, milestones_for, normalise_highlights
from mizalla.models import Meeting, Member, Project, ProjectMembership, ProjectTechnology, Server, Task, TaskStep, TaskOwner
from mizalla.settings import get_task_highlight_types_list, set_setting

PASSWORD = "a-long-enough-password"


def make_admin(name="Root Admin", email="admin@example.com"):
    admin = Member(name=name, email=email, is_admin=True)
    admin.set_password(PASSWORD)
    db.session.add(admin)
    return admin


def make_member(name, email, password=PASSWORD):
    member = Member(name=name, email=email)
    member.set_password(password)
    db.session.add(member)
    return member


@pytest.fixture
def seeded(app):
    """An admin who is also on the project team, with a task, step, and meeting."""
    with app.app_context():
        admin = make_admin()
        team = make_member("Team Dev", "dev@example.com")
        client = make_member("Client Rep", "client@example.com")
        stakeholder = make_member("Stakeholder", "stake@example.com")
        vendor = make_member("Vendor", "vendor@example.com")
        outsider = make_member("Outsider", "out@example.com")

        project = Project(project_id="SUM-1", name="Summary Project", description="A described project.")
        project.scope = "The agreed scope text."
        project.estimated_completion_date = date(2026, 12, 1)
        db.session.add(project)
        db.session.flush()

        admin_pm_roles = "Project Manager"
        project.memberships.append(ProjectMembership(member=admin, role="team", team_roles=admin_pm_roles, responsibility="Oversee delivery"))
        project.memberships.append(ProjectMembership(member=team, role="team", team_roles="Developer/Implementer", responsibility="Build it"))
        project.memberships.append(ProjectMembership(member=client, role="client", responsibility="Sign off"))
        project.memberships.append(ProjectMembership(member=stakeholder, role="stakeholder", responsibility="Oversee"))
        project.memberships.append(ProjectMembership(member=vendor, role="vendor", responsibility="Supply"))
        project.technologies.append(ProjectTechnology(technology="Flask"))
        db.session.add(Meeting(project=project, meeting_date=date(2026, 1, 5), agenda="Kickoff"))
        db.session.add(Server(name="srv-1", ip="10.0.0.1", operating_system="Ubuntu"))
        db.session.commit()

        task = Task(project=project, name="Build the thing", task_type="Development", status="In Progress", due_date=date(2026, 10, 1))
        db.session.add(task)
        db.session.flush()
        db.session.add(TaskStep(task=task, header="Design", percentage_complete=50))
        db.session.add(TaskOwner(task=task, member=team, responsibility="Implementation"))
        db.session.commit()

        ids = {
            "admin": admin.id,
            "team": team.id,
            "client": client.id,
            "stakeholder": stakeholder.id,
            "vendor": vendor.id,
            "outsider": outsider.id,
            "project": project.id,
            "task": task.id,
            "step": task.steps[0].id,
            "meeting": project.meetings[0].id,
            "server": Server.query.first().id,
        }
    return ids


# --------------------------------------------------------------------------
# Highlights
# --------------------------------------------------------------------------


def test_normalise_highlights_orders_by_setting_and_deduplicates():
    allowed = ["Key Amendment", "Key Finding", "Key Decision"]
    assert normalise_highlights(["Key Decision", "Key Finding"], allowed) == ["Key Finding", "Key Decision"]
    assert normalise_highlights(["key decision", "KEY DECISION"], allowed) == ["Key Decision"]
    assert normalise_highlights("Key Finding, Key Amendment", allowed) == ["Key Amendment", "Key Finding"]
    assert normalise_highlights([], allowed) == []
    assert normalise_highlights(None, allowed) == []


def test_normalise_highlights_rejects_labels_the_admin_did_not_configure():
    with pytest.raises(ValueError, match="not a configured highlight type"):
        normalise_highlights(["Key Rumour"], ["Key Finding"])


def test_creating_a_task_records_highlights_and_defaults_the_date_to_today(app, client, seeded):
    login_admin(client)
    with app.app_context():
        response = client.post(
            f"/apps/mizalla/projects/{seeded['project']}/tasks",
            data={
                "name": "Signed off the change",
                "task_type": "Development",
                "status": "Completed",
                "priority": "5",
                "weight": "5",
                "highlights": ["Key Amendment", "Key Decision"],
            },
        )
        assert response.status_code == 302
        task = Task.query.filter_by(name="Signed off the change").one()
        assert task.get_highlight_list() == ["Key Amendment", "Key Decision"]
        assert task.highlighted_on == date.today()


def test_clearing_all_highlights_clears_the_date(app, client, seeded):
    login_admin(client)
    with app.app_context():
        task = Task.query.get(seeded["task"])
        task.set_highlight_list(["Key Finding"])
        task.highlighted_on = date(2026, 3, 3)
        db.session.commit()

        response = client.post(
            f"/apps/mizalla/tasks/{seeded['task']}/edit",
            data={
                "name": task.name,
                "task_type": task.task_type,
                "status": task.status,
                "priority": task.priority,
                "weight": task.weight,
            },
        )
        assert response.status_code == 302
        task = Task.query.get(seeded["task"])
        assert task.get_highlight_list() == []
        assert task.highlighted_on is None


def test_admin_renaming_the_label_list_changes_the_task_form_options(app, client, seeded):
    with app.app_context():
        set_setting("task_highlight_types", "Gate Review, Blocker")
        db.session.commit()
    login(client, "admin@example.com")
    page = client.get(f"/apps/mizalla/tasks/{seeded['task']}/edit")
    body = page.data.decode()
    assert page.status_code == 200
    assert "Gate Review" in body and "Blocker" in body
    assert "Key Amendment" not in body


def test_task_form_rejects_a_highlight_that_is_not_configured(app, client, seeded):
    login_admin(client)
    with app.app_context():
        response = client.post(
            f"/apps/mizalla/tasks/{seeded['task']}/edit",
            data={
                "name": "Renamed",
                "task_type": "Development",
                "status": "In Progress",
                "priority": 5,
                "weight": 5,
                "highlights": ["Key Fabrication"],
            },
            follow_redirects=True,
        )
        assert "not a configured highlight type" in response.data.decode()
        assert Task.query.get(seeded["task"]).name == "Build the thing"


# --------------------------------------------------------------------------
# Milestones
# --------------------------------------------------------------------------


def test_milestone_flag_auto_stamps_the_achieved_date_once(app, client, seeded):
    login_admin(client)
    with app.app_context():
        response = client.post(
            f"/apps/mizalla/projects/{seeded['project']}/tasks",
            data={"name": "Go live", "task_type": "Development", "status": "Completed", "priority": "5", "weight": "5", "is_milestone": "1"},
        )
        assert response.status_code == 302
        task = Task.query.filter_by(name="Go live").one()
        assert task.is_milestone is True
        assert task.milestone_achieved_on == date.today()

        # Re-saving the same milestone must not rewrite the achieved date.
        earlier = date(2026, 1, 2)
        client.post(
            f"/apps/mizalla/tasks/{task.id}/edit",
            data={
                "name": task.name,
                "task_type": task.task_type,
                "status": "Completed",
                "priority": 5,
                "weight": 5,
                "is_milestone": "1",
                "milestone_achieved_on": earlier.isoformat(),
            },
        )
        assert Task.query.get(task.id).milestone_achieved_on == earlier


def test_apply_milestone_clears_achieved_date_when_unflagged(app):
    with app.app_context():
        project = Project(project_id="M-1", name="M")
        db.session.add(project)
        task = Task(project=project, name="T", task_type="Development", status="Completed", is_milestone=True, milestone_achieved_on=date(2026, 5, 5))
        db.session.add(task)
        db.session.commit()
        apply_milestone(task, False, "Completed", None)
        assert task.is_milestone is False
        assert task.milestone_achieved_on is None


def test_milestones_for_ranks_upcoming_by_due_date_with_undated_last(app):
    with app.app_context():
        project = Project(project_id="M-2", name="M")
        db.session.add(project)
        undated = Task(project=project, name="Undated", task_type="Development", status="Not Started", is_milestone=True)
        late = Task(project=project, name="Late", task_type="Development", status="Not Started", is_milestone=True, due_date=date(2026, 11, 1))
        soon = Task(project=project, name="Soon", task_type="Development", status="In Progress", is_milestone=True, due_date=date(2026, 9, 1))
        done = Task(project=project, name="Done", task_type="Development", status="Completed", is_milestone=True, milestone_achieved_on=date(2026, 2, 2))
        plain = Task(project=project, name="Plain", task_type="Development", status="In Progress")
        db.session.add_all([undated, late, soon, done, plain])
        db.session.commit()

        achieved, upcoming, open_milestones = milestones_for(project.tasks)
        assert [t.name for t in upcoming] == ["Soon", "Late", "Undated"]
        assert [t.name for t in achieved] == ["Done"]
        assert len(open_milestones) == 3
        assert "Plain" not in [t.name for t in open_milestones]


def test_a_task_with_all_steps_complete_counts_as_achieved(app):
    with app.app_context():
        project = Project(project_id="M-3", name="M")
        db.session.add(project)
        task = Task(project=project, name="Stepped", task_type="Development", status="In Progress", is_milestone=True)
        db.session.add(task)
        db.session.flush()
        db.session.add(TaskStep(task=task, header="Only", percentage_complete=100))
        db.session.commit()
        assert task.is_achieved is True
        assert task.task_completion == 100


# --------------------------------------------------------------------------
# Summary screen
# --------------------------------------------------------------------------


def login(client, email, password=PASSWORD):
    return client.post("/apps/login", data={"email": email, "password": password})


def accordion_body(body, heading):
    """Return the inner HTML of the summary accordion carrying the given heading.

    A page-wide ``in body`` check is not enough: the badge macro also prints a task's
    highlight labels on the Current tasks list, so a whole-page assertion passes even
    when the Key highlights section has dropped the label entirely.
    """
    pattern = re.compile(
        r'<summary class="accordion-head">.*?accordion-heading">\s*'
        + re.escape(heading)
        + r'\s*</span>.*?</summary>\s*<div class="accordion-body">(.*?)</div>\s*</details>',
        re.S,
    )
    match = pattern.search(body)
    assert match, f"no accordion headed {heading!r}"
    return match.group(1)


ROLE_LOGINS = {
    "admin": "admin@example.com",
    "team": "dev@example.com",
    "client": "client@example.com",
    "stakeholder": "stake@example.com",
    "vendor": "vendor@example.com",
}


@pytest.mark.parametrize("role", sorted(ROLE_LOGINS))
def test_every_project_role_can_open_the_summary(app, client, seeded, role):
    assert login(client, ROLE_LOGINS[role]).status_code == 302
    response = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary")
    assert response.status_code == 200
    assert b"Summary Project" in response.data


def test_a_non_member_cannot_open_the_summary(app, client, seeded):
    assert login(client, "out@example.com").status_code == 302
    assert client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").status_code == 404


def test_summary_is_read_only_for_non_team_members(app, client, seeded):
    login(client, "client@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    assert "read-only access" in body
    assert "Add task" not in body


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("get", "/apps/mizalla/projects/{project}/edit", None),
        ("get", "/apps/admin/members", None),
        ("get", "/apps/admin/settings", None),
    ],
)
def test_a_forbidden_request_gets_the_styled_page_not_flask_default(
    app, client, seeded, method, path, data
):
    """access.py raises 403 constantly; it must not fall through to Flask's bare page."""
    login(client, "client@example.com")
    response = getattr(client, method)(path.format(project=seeded["project"]), data=data)
    assert response.status_code == 403
    body = response.data.decode()
    assert "Not permitted" in body
    assert 'class="button button-primary"' in body, "error page must offer a way back"
    assert "Traceback" not in body


def test_a_hidden_project_404_hides_its_existence_behind_the_styled_page(app, client, seeded):
    login(client, "out@example.com")
    response = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary")
    assert response.status_code == 404
    body = response.data.decode()
    assert "Not found" in body
    # The message must not confirm whether the project itself exists.
    assert "Summary Project" not in body


def test_summary_shows_every_required_section(app, client, seeded):
    login(client, "admin@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    for heading in (
        "Scope and timeline",
        "Milestones",
        "Key highlights",
        "Current tasks",
        "Blocked, overdue and at risk",
        "Meetings",
        "Documents",
        "Project team and responsibilities",
    ):
        assert heading in body, f"missing summary section: {heading}"


def test_summary_sections_start_collapsed_so_the_screen_is_not_verbose(app, client, seeded):
    login(client, "admin@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    accordions = re.findall(r'<details class="accordion[^"]*"[^>]*>', body)
    assert accordions, "summary should use native disclosure accordions"
    assert all("open" not in tag for tag in accordions), "summary sections must start collapsed"


def test_summary_carries_the_always_visible_pulse_row(app, client, seeded):
    login(client, "admin@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    for label in ("Completion", "Next milestone", "Needs attention", "Next meeting"):
        assert label in body
    assert "Full project details" in body
    assert f"/apps/mizalla/projects/{seeded['project']}" in body


def test_summary_lists_team_with_mailto_and_the_project_manager(app, client, seeded):
    login(client, "admin@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    assert "mailto:admin@example.com" in body
    assert "mailto:vendor@example.com" in body
    assert "Project Managers" in body
    assert "Client" in body and "Stakeholder" in body and "Vendor" in body


def test_summary_records_highlights_with_a_date_and_a_task_link(app, client, seeded):
    with app.app_context():
        task = Task.query.get(seeded["task"])
        task.set_highlight_list(["Key Finding"])
        task.highlighted_on = date(2026, 4, 4)
        db.session.commit()
    login(client, "admin@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    assert "Key Finding" in body
    assert "Apr 04, 2026" in body
    assert f"#task-{seeded['task']}" in body


def test_summary_keeps_showing_a_label_removed_from_the_setting(app, client, seeded):
    with app.app_context():
        task = Task.query.get(seeded["task"])
        # Complete the task so it also drops out of the Current tasks accordion, which
        # would otherwise print the raw labels and mask a regression here.
        task.status = "Completed"
        task.set_highlight_list(["Key Finding", "Retired Label"])
        task.highlighted_on = date(2026, 4, 4)
        db.session.commit()
        set_setting("task_highlight_types", "Key Amendment, Key Finding, Key Decision")
        db.session.commit()
    login(client, "admin@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    assert "Retired Label" in accordion_body(body, "Key highlights")


def test_summary_reports_blocked_and_overdue_work(app, client, seeded):
    with app.app_context():
        project = Project.query.get(seeded["project"])
        db.session.add(Task(project=project, name="Stuck item", task_type="Development", status="Blocked"))
        db.session.add(Task(project=project, name="Late item", task_type="Development", status="In Progress", due_date=date.today() - timedelta(days=3)))
        db.session.add(Task(project=project, name="Fine item", task_type="Development", status="In Progress", due_date=date.today() + timedelta(days=3)))
        db.session.commit()
    login(client, "admin@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    at_risk = accordion_body(body, "Blocked, overdue and at risk")
    assert "Stuck item" in at_risk
    assert "Late item" in at_risk
    assert "Fine item" not in at_risk


def test_summary_shows_next_and_achieved_milestones(app, client, seeded):
    with app.app_context():
        project = Project.query.get(seeded["project"])
        db.session.add(Task(project=project, name="Next gate", task_type="Development", status="In Progress", is_milestone=True, due_date=date.today() + timedelta(days=7)))
        db.session.add(Task(project=project, name="Past gate", task_type="Development", status="Completed", is_milestone=True, milestone_achieved_on=date(2026, 1, 15)))
        db.session.commit()
    login(client, "admin@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    assert "Next gate" in body
    assert "Past gate" in body
    assert "Jan 15, 2026" in body


def test_detail_screen_gains_a_milestone_section_and_a_summary_link(app, client, seeded):
    login(client, "admin@example.com")
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}").data.decode()
    assert "Milestones" in body
    assert f"/apps/mizalla/projects/{seeded['project']}/summary" in body
    assert "flagged as milestones" in body


# --------------------------------------------------------------------------
# Breadcrumbs and template rendering
# --------------------------------------------------------------------------


def login_admin(client):
    return login(client, "admin@example.com")


def test_every_screen_renders(app, client, seeded):
    """Walk every screen so an unimported Jinja macro cannot pass unnoticed again.

    The suite was green for 73 tests while a template referenced a macro it never
    imported, which only blew up on request.
    """
    login_admin(client)
    paths = [
        "/apps/mizalla/",
        "/apps/mizalla/about",
        "/apps/mizalla/user-guide",
        "/apps/mizalla/projects/new",
        f"/apps/mizalla/projects/{seeded['project']}",
        f"/apps/mizalla/projects/{seeded['project']}/summary",
        f"/apps/mizalla/projects/{seeded['project']}/edit",
        f"/apps/mizalla/tasks/{seeded['task']}/edit",
        f"/apps/mizalla/steps/{seeded['step']}/edit",
        f"/apps/mizalla/projects/{seeded['project']}/meetings/{seeded['meeting']}/edit",
        "/apps/admin/members",
        f"/apps/admin/members/{seeded['admin']}/edit",
        "/apps/admin/servers",
        f"/apps/admin/servers/{seeded['server']}/edit",
        "/apps/admin/settings",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        assert b"</html>" in response.data, f"{path} rendered a truncated document"


def test_error_page_renders(app, client, seeded):
    login_admin(client)
    response = client.get("/apps/mizalla/projects/999999")
    assert response.status_code == 404
    assert b"</html>" in response.data


def test_hierarchical_screens_carry_a_breadcrumb(app, client, seeded):
    login_admin(client)
    paths = [
        f"/apps/mizalla/projects/{seeded['project']}/summary",
        f"/apps/mizalla/projects/{seeded['project']}/edit",
        f"/apps/mizalla/tasks/{seeded['task']}/edit",
        f"/apps/mizalla/steps/{seeded['step']}/edit",
        f"/apps/mizalla/projects/{seeded['project']}/meetings/{seeded['meeting']}/edit",
        "/apps/admin/members",
        f"/apps/admin/members/{seeded['admin']}/edit",
        "/apps/admin/servers",
        f"/apps/admin/servers/{seeded['server']}/edit",
        "/apps/admin/settings",
        "/apps/mizalla/user-guide",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert 'aria-label="Breadcrumb"' in response.data.decode(), f"{path} has no breadcrumb"


def test_top_level_screens_omit_the_pointless_single_item_trail(client):
    """A one-item trail offers no way back, and the top bar already links home."""
    for path in ("/apps/mizalla/", "/apps/mizalla/about", "/apps/login"):
        body = client.get(path).data.decode()
        assert 'aria-label="Breadcrumb"' not in body, f"{path} should not render a one-item breadcrumb"


def test_breadcrumb_macro_suppresses_trails_of_one_or_fewer_items(app):
    """Unit-test the guard itself, which no current screen exercises."""
    from flask import render_template_string

    template = '{% from "macros.html" import breadcrumb %}{{ breadcrumb(items) }}'
    with app.app_context():
        none = render_template_string(template, items=[])
        one = render_template_string(template, items=[("Projects", "/apps/mizalla/")])
        two = render_template_string(template, items=[("Projects", "/apps/mizalla/"), ("Settings", None)])

    assert 'aria-label="Breadcrumb"' not in none
    assert 'aria-label="Breadcrumb"' not in one
    assert 'aria-label="Breadcrumb"' in two
    assert 'aria-current="page">Settings<' in two


def test_breadcrumb_marks_the_final_page_as_current(app, client, seeded):
    login_admin(client)
    body = client.get(f"/apps/mizalla/projects/{seeded['project']}/summary").data.decode()
    summary_row = re.search(r'<nav class="crumb"[^>]*>(.*?)</nav>', body, re.S)
    assert summary_row is not None
    assert 'aria-current="page">Summary<' in summary_row.group(1)


# --------------------------------------------------------------------------
# Admin settings
# --------------------------------------------------------------------------


def test_settings_rejects_a_blank_label_list_and_saves_nothing(app, client, seeded):
    login_admin(client)
    before = client.get("/apps/admin/settings").data.decode()
    assert "task_highlight_types" in before

    response = client.post(
        "/apps/admin/settings",
        data={
            "task_highlight_types": "  ,  ,",
            "project_team_roles": "Project Manager, Analyst",
            "email_status_report_days": "3",
        },
        follow_redirects=True,
    )
    body = response.data.decode()
    assert "At least one label is required" in body
    # Nothing may have been written, including the unrelated valid field.
    with app.app_context():
        from mizalla.settings import get_setting

        assert get_setting("email_status_report_days") != "3"
        assert get_task_highlight_types_list() == ["Key Amendment", "Key Finding", "Key Decision"]


def test_settings_rejects_a_duplicate_label(app, client, seeded):
    login_admin(client)
    body = client.post(
        "/apps/admin/settings",
        data={"task_highlight_types": "Key Finding, key finding"},
        follow_redirects=True,
    ).data.decode()
    assert "listed more than once" in body


def test_settings_saves_a_valid_label_list(app, client, seeded):
    login_admin(client)
    client.post(
        "/apps/admin/settings",
        data={"task_highlight_types": "Key Amendment, Key Decision, Blocker"},
        follow_redirects=True,
    )
    with app.app_context():
        assert get_task_highlight_types_list() == ["Key Amendment", "Key Decision", "Blocker"]


def test_settings_can_reset_a_single_setting_to_its_default(app, client, seeded):
    login_admin(client)
    client.post("/apps/admin/settings", data={"task_highlight_types": "Only One Label"}, follow_redirects=True)
    body = client.post("/apps/admin/settings", data={"reset": "task_highlight_types"}, follow_redirects=True).data.decode()
    assert "restored to its default value" in body
    with app.app_context():
        assert get_task_highlight_types_list() == ["Key Amendment", "Key Finding", "Key Decision"]


def test_settings_reset_ignores_an_unknown_key(app, client, seeded):
    login_admin(client)
    body = client.post("/apps/admin/settings", data={"reset": "not_a_setting"}, follow_redirects=True).data.decode()
    assert "not a known setting" in body


# --------------------------------------------------------------------------
# REST API
# --------------------------------------------------------------------------


@pytest.fixture
def api(app, client, seeded):
    with app.app_context():
        admin = Member.query.get(seeded["admin"])
        key = admin.api_key or admin.generate_api_key()
        db.session.commit()
        return key


def test_api_creates_a_task_with_highlights_and_a_milestone(app, client, seeded, api):
    response = client.post(
        f"/apps/mizalla/api/v1/projects/{seeded['project']}/tasks",
        json={"name": "API milestone", "task_type": "Development", "status": "Completed", "highlights": ["Key Decision"], "is_milestone": True},
        headers={"X-API-Key": api},
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["highlights"] == ["Key Decision"]
    assert payload["is_milestone"] is True
    assert payload["milestone_achieved_on"] == date.today().isoformat()


def test_api_rejects_an_unconfigured_highlight(app, client, seeded, api):
    response = client.post(
        f"/apps/mizalla/api/v1/projects/{seeded['project']}/tasks",
        json={"name": "Bad", "task_type": "Development", "highlights": ["Key Fabrication"]},
        headers={"X-API-Key": api},
    )
    assert response.status_code == 400
    assert "not a configured highlight type" in response.get_json()["error"]


def test_api_patch_replaces_the_whole_highlight_set(app, client, seeded, api):
    with app.app_context():
        task = Task.query.get(seeded["task"])
        task.set_highlight_list(["Key Finding", "Key Decision"])
        task.highlighted_on = date(2026, 6, 6)
        db.session.commit()

    response = client.patch(
        f"/apps/mizalla/api/v1/tasks/{seeded['task']}",
        json={"highlights": ["Key Amendment"]},
        headers={"X-API-Key": api},
    )
    assert response.status_code == 200
    assert response.get_json()["highlights"] == ["Key Amendment"]


def test_api_project_payload_exposes_highlights_and_milestones(app, client, seeded, api):
    response = client.get(f"/apps/mizalla/api/v1/projects/{seeded['project']}", headers={"X-API-Key": api})
    assert response.status_code == 200
    task = response.get_json()["tasks"][0]
    for key in ("highlights", "highlighted_on", "is_milestone", "milestone_achieved_on", "percentage_complete"):
        assert key in task
