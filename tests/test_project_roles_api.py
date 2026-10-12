"""REST API role assignment: PM-only, and able to grant several roles at once.

The web form has always been reserved for a PM of the project. The API was not -
it accepted any Project team member - so the same rule is applied here, and the
payload grew the ability to assign more than one role per call to match what the
web form can now do.
"""

import pytest

from mizalla.extensions import db
from mizalla.models import Member, Project, ProjectMembership
from mizalla.role_meta import PM_TEAM_ROLE

BASE = "/apps/mizalla/api/v1"


def make_member(name, email, password="a-long-enough-password", **flags):
    member = Member(name=name, email=email, **flags)
    member.set_password(password)
    return member


def make_project(code, name, *members):
    project = Project(project_id=code, name=name)
    for member, role, team_roles in members:
        row = ProjectMembership(member=member, role=role)
        if team_roles:
            row.set_team_roles_list(team_roles)
        project.memberships.append(row)
    return project


@pytest.fixture
def api_setup(app):
    """A PM, a developer, an outsider, a newcomer, and two projects."""
    with app.app_context():
        pm = make_member("PM", "pm@example.com")
        dev = make_member("Dev", "dev@example.com")
        outsider = make_member("PM Elsewhere", "elsewhere@example.com")
        newcomer = make_member("Newcomer", "new@example.com")
        mine = make_project("P1", "Mine", (pm, "delegated", None), (dev, "team", None))
        theirs = make_project("P2", "Theirs", (outsider, "delegated", None))
        db.session.add_all([pm, dev, outsider, newcomer, mine, theirs])
        db.session.commit()
        keys = {m.email: m.api_key or m.generate_api_key() for m in (pm, dev, outsider)}
        db.session.commit()
        return {
            "keys": keys,
            "mine": mine.id,
            "theirs": theirs.id,
            "newcomer": newcomer.id,
        }


def auth(email):
    return {"Authorization": f"Bearer {email}", "Content-Type": "application/json"}


def add_role(client, email, project_id, body):
    return client.post(f"{BASE}/projects/{project_id}/members", headers=auth(email), json=body)


def test_a_pm_can_assign_a_role(app, client, api_setup):
    response = add_role(
        client,
        api_setup["keys"]["pm@example.com"],
        api_setup["mine"],
        {"email": "new@example.com", "role": "team"},
    )
    assert response.status_code == 201
    assert response.get_json()["role"] == "team"

    with app.app_context():
        row = ProjectMembership.query.filter_by(
            project_id=api_setup["mine"],
            member_id=api_setup["newcomer"],
            role="team",
        ).one()
        assert row is not None


def test_a_plain_team_member_cannot_assign_roles(app, client, api_setup):
    """This is the tightening: a developer can edit the project but not staff it."""
    response = add_role(
        client,
        api_setup["keys"]["dev@example.com"],
        api_setup["mine"],
        {"email": "new@example.com", "role": "team"},
    )
    assert response.status_code == 403

    with app.app_context():
        assert ProjectMembership.query.filter_by(
            member_id=api_setup["newcomer"]
        ).count() == 0


def test_a_pm_of_another_project_cannot_assign_roles(app, client, api_setup):
    """They do not manage this project, and cannot even see it."""
    response = add_role(
        client,
        api_setup["keys"]["elsewhere@example.com"],
        api_setup["mine"],
        {"email": "new@example.com", "role": "team"},
    )
    assert response.status_code == 403


def test_a_delegated_backup_cannot_assign_roles(app, client, api_setup):
    """Developer-equivalent access does not include assigning roles."""
    with app.app_context():
        backup = make_member("Backup", "backup@example.com")
        project = db.session.get(Project, api_setup["mine"])
        project.memberships.append(ProjectMembership(member=backup, role="delegated_backup"))
        db.session.add(backup)
        db.session.commit()
        key = backup.generate_api_key()
        db.session.commit()

    response = add_role(
        client, key, api_setup["mine"], {"email": "new@example.com", "role": "team"}
    )
    assert response.status_code == 403


def test_several_roles_can_be_assigned_in_one_call(app, client, api_setup):
    response = add_role(
        client,
        api_setup["keys"]["pm@example.com"],
        api_setup["mine"],
        {"email": "new@example.com", "roles": ["team", "client", "delegated_backup"]},
    )
    assert response.status_code == 201
    body = response.get_json()
    assert set(body["roles"]) == {"team", "client", "delegated_backup"}

    with app.app_context():
        rows = ProjectMembership.query.filter_by(
            project_id=api_setup["mine"], member_id=api_setup["newcomer"]
        ).all()
        assert {row.role for row in rows} == {"team", "client", "delegated_backup"}


def test_a_role_already_held_is_skipped_not_duplicated(app, client, api_setup):
    response = add_role(
        client,
        api_setup["keys"]["pm@example.com"],
        api_setup["mine"],
        {"email": "dev@example.com", "roles": ["team", "vendor"]},
    )
    assert response.status_code == 201
    body = response.get_json()
    # The new role came back; the one already held is reported separately.
    assert body["roles"] == ["vendor"]
    assert body["already_held"] == ["team"]

    with app.app_context():
        dev = Member.query.filter_by(email="dev@example.com").one()
        rows = ProjectMembership.query.filter_by(
            project_id=api_setup["mine"], member_id=dev.id
        ).all()
        assert {row.role for row in rows} == {"team", "vendor"}


def test_assigning_only_roles_already_held_is_rejected(app, client, api_setup):
    response = add_role(
        client,
        api_setup["keys"]["pm@example.com"],
        api_setup["mine"],
        {"email": "dev@example.com", "role": "team"},
    )
    assert response.status_code == 400
    assert "already" in response.get_json()["error"].lower()


def test_an_invalid_role_is_rejected(app, client, api_setup):
    response = add_role(
        client,
        api_setup["keys"]["pm@example.com"],
        api_setup["mine"],
        {"email": "new@example.com", "roles": ["team", "superuser"]},
    )
    assert response.status_code == 400

    with app.app_context():
        assert ProjectMembership.query.filter_by(
            member_id=api_setup["newcomer"]
        ).count() == 0


def test_creating_a_project_over_the_api_makes_the_caller_its_pm(app, client):
    """So their key can go on to staff the project they just created."""
    with app.app_context():
        founder = make_member("Founder", "founder@example.com")
        db.session.add(founder)
        db.session.commit()
        key = founder.generate_api_key()
        db.session.commit()

    created = client.post(
        f"{BASE}/projects",
        headers=auth(key),
        json={"project_id": "API-1", "name": "Via API"},
    )
    assert created.status_code == 201
    project_id = created.get_json()["id"]

    response = add_role(
        client, key, project_id, {"email": "founder@example.com", "role": "client"}
    )
    # They manage it, so this is allowed (and refused only as a duplicate of
    # nothing - the client role is new to them).
    assert response.status_code == 201


def test_the_project_payload_reports_every_role_a_member_holds(app, client, api_setup):
    with app.app_context():
        dev = Member.query.filter_by(email="dev@example.com").one()
        project = db.session.get(Project, api_setup["mine"])
        project.memberships.append(ProjectMembership(member=dev, role="vendor"))
        db.session.commit()

    response = client.get(
        f"{BASE}/projects/{api_setup['mine']}", headers=auth(api_setup["keys"]["dev@example.com"])
    )
    assert response.status_code == 200
    members = response.get_json()["members"]

    dev_entries = [m for m in members if m["email"] == "dev@example.com"]
    # One entry per membership row, so filtering on "role" still finds everyone.
    assert len(dev_entries) == 2
    for entry in dev_entries:
        # Each entry also carries the member's complete set of roles.
        assert set(entry["roles"]) == {"team", "vendor"}
    assert {m["role"] for m in dev_entries} == {"team", "vendor"}

    # The PM row is marked as such, and the developer row is not.
    pm_entries = [m for m in members if m["email"] == "pm@example.com"]
    assert len(pm_entries) == 1
    assert pm_entries[0]["is_project_manager"] is True
    assert all(
        m["is_project_manager"] is False for m in dev_entries
    )


def test_a_team_member_with_the_pm_team_role_counts_as_a_pm(app, client, api_setup):
    with app.app_context():
        backup = make_member("Team PM", "teampm@example.com")
        project = db.session.get(Project, api_setup["mine"])
        project.memberships.append(
            ProjectMembership(member=backup, role="team", team_roles=None)
        )
        # Attach the PM team role the long way round, the way the UI does.
        row = project.memberships[-1]
        row.set_team_roles_list([PM_TEAM_ROLE])
        db.session.add(backup)
        db.session.commit()
        key = backup.generate_api_key()
        db.session.commit()

    response = add_role(
        client, key, api_setup["mine"], {"email": "new@example.com", "role": "team"}
    )
    assert response.status_code == 201


def test_an_admin_can_assign_roles_on_any_project(app, client, api_setup):
    with app.app_context():
        boss = make_member("Admin", "admin@example.com", is_admin=True)
        db.session.add(boss)
        db.session.commit()
        key = boss.generate_api_key()
        db.session.commit()

    response = add_role(
        client, key, api_setup["theirs"], {"email": "new@example.com", "role": "team"}
    )
    assert response.status_code == 201


def test_the_global_pm_flag_does_not_open_the_api_either(app, client, api_setup):
    with app.app_context():
        flagged = make_member("Flagged", "flagged@example.com", is_pm=True)
        db.session.add(flagged)
        db.session.commit()
        key = flagged.generate_api_key()
        db.session.commit()

    # They manage no project, so even reading one is a 404.
    assert client.get(
        f"{BASE}/projects/{api_setup['mine']}", headers=auth(key)
    ).status_code == 404
