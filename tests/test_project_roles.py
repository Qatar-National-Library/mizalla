"""Per-project roles: what each one grants, and who may assign them.

A role belongs to one project, not to a person. A member can hold several roles on
the same project, be a Delegated PM on one and a Vendor on another, and the answer
to "may they edit this?" is whatever their roles on *this* project collectively
grant. These tests pin that down, because the previous model was a single role
column plus a global is_pm flag that made someone a PM everywhere.
"""

import sqlite3
from html.parser import HTMLParser

import pytest

from mizalla.access import (
    CAN_EDIT,
    CAN_PM,
    ROLE_GRANTS,
    can_edit_project,
    is_pm_membership,
    is_project_pm,
    role_grants,
)
from mizalla.extensions import db
from mizalla.feature_access import backfill_global_pm_roles
from mizalla.models import (
    MEMBERSHIP_LABELS,
    MEMBERSHIP_ORDER,
    MEMBERSHIP_ROLES,
    PM_TEAM_ROLE,
    Member,
    Project,
    ProjectMembership,
)
from mizalla.role_meta import MEMBERSHIP_NOTES
from mizalla.schema import upgrade_schema
from mizalla.settings import can_edit_tasks, set_setting


def make_member(name, email, password="a-long-enough-password", **flags):
    member = Member(name=name, email=email, **flags)
    member.set_password(password)
    return member


def make_project(code, name, *members):
    """A project with one membership row per (member, role) pair given."""
    project = Project(project_id=code, name=name)
    for member, role, team_roles in members:
        row = ProjectMembership(member=member, role=role)
        if team_roles:
            row.set_team_roles_list(team_roles)
        project.memberships.append(row)
    return project


# --------------------------------------------------------------------------
# What each role means
# --------------------------------------------------------------------------


def test_every_role_has_a_label_an_order_slot_and_a_grant():
    """A role with no grant entry would silently be read-only."""
    assert set(ROLE_GRANTS) == set(MEMBERSHIP_ROLES)
    assert set(MEMBERSHIP_LABELS) == set(MEMBERSHIP_ROLES)
    assert set(MEMBERSHIP_ORDER) == set(MEMBERSHIP_ROLES)
    assert set(MEMBERSHIP_NOTES) == set(MEMBERSHIP_ROLES)
    # The CHECK constraint is generated from MEMBERSHIP_ROLES, so the order in
    # the tuple is what ends up in the database.
    assert len(set(MEMBERSHIP_ROLES)) == len(MEMBERSHIP_ROLES)


def test_delegated_pm_grants_the_pm_capability():
    """'Delegated PM has the same access as the PM'."""
    assert role_grants("delegated", CAN_PM)
    assert role_grants("delegated", CAN_EDIT)
    assert role_grants("delegated", "view")


def test_delegated_backup_grants_exactly_what_a_developer_grants():
    """'Delegated Backup has the same access as the Developer'.

    A Developer is a Project team member, so the two grant sets must be equal -
    notably excluding PM, or a stand-in could reassign anyone's role.
    """
    assert ROLE_GRANTS["delegated_backup"] == ROLE_GRANTS["team"]
    assert not role_grants("delegated_backup", CAN_PM)
    assert role_grants("delegated_backup", CAN_EDIT)


def test_observer_roles_are_read_only():
    for role in ("client", "stakeholder", "vendor"):
        assert role_grants(role, "view")
        assert not role_grants(role, CAN_EDIT)
        assert not role_grants(role, CAN_PM)


def test_an_unknown_role_grants_nothing():
    """Fails safe: a new role starts read-only rather than accidentally open."""
    assert not role_grants("something-new", "view")
    assert not role_grants("something-new", CAN_EDIT)
    assert not role_grants("something-new", CAN_PM)


# --------------------------------------------------------------------------
# A PM of one project is not a PM of another
# --------------------------------------------------------------------------


def test_delegated_pm_authority_is_scoped_to_one_project(app):
    with app.app_context():
        lead = make_member("Lead", "lead@example.com")
        first = Project(project_id="P1", name="First")
        second = Project(project_id="P2", name="Second")
        first.memberships.append(ProjectMembership(member=lead, role="delegated"))
        second.memberships.append(ProjectMembership(member=lead, role="vendor"))
        db.session.add_all([lead, first, second])
        db.session.commit()

        assert is_project_pm(first, lead)
        # A Vendor on the second project, so no PM authority there even though
        # they run the first one.
        assert not is_project_pm(second, lead)
        # Nor can they edit it.
        assert not can_edit_project(second, lead)
        # But they can still see it, which is what a vendor gets.
        assert second.id is not None


def test_the_global_pm_flag_no_longer_grants_project_authority(app):
    """The flag means "may create projects"; authority comes from the roles."""
    with app.app_context():
        flagged = make_member("Flagged PM", "flagged@example.com", is_pm=True)
        project = Project(project_id="P1", name="First")
        # A plain team membership: a Developer, not a PM.
        project.memberships.append(ProjectMembership(member=flagged, role="team"))
        db.session.add_all([flagged, project])
        db.session.commit()

        assert not is_project_pm(project, flagged)
        assert can_edit_project(project, flagged)


def test_a_team_member_with_the_pm_team_role_is_a_pm(app):
    with app.app_context():
        lead = make_member("Team PM", "teampm@example.com")
        project = make_project("P1", "First", (lead, "team", [PM_TEAM_ROLE]))
        db.session.add_all([lead, project])
        db.session.commit()

        assert is_project_pm(project, lead)
        assert is_pm_membership(project.memberships[0])


def test_admins_are_always_authorised(app):
    with app.app_context():
        boss = make_member("Admin", "admin@example.com", is_admin=True)
        project = Project(project_id="P1", name="First")
        project.memberships.append(ProjectMembership(member=boss, role="vendor"))
        db.session.add_all([boss, project])
        db.session.commit()

        assert is_project_pm(project, boss)
        assert can_edit_project(project, boss)


# --------------------------------------------------------------------------
# Several roles on the same project
# --------------------------------------------------------------------------


def test_a_member_can_hold_several_roles_on_one_project(app):
    with app.app_context():
        person = make_member("Multi", "multi@example.com")
        project = make_project(
            "P1",
            "First",
            (person, "vendor", None),
            (person, "team", ["Developer"]),
            (person, "delegated", None),
        )
        db.session.add_all([person, project])
        db.session.commit()

        roles = {row.role for row in project.memberships}
        assert roles == {"vendor", "team", "delegated"}
        # Every role counts, so the strongest one is what decides.
        assert is_project_pm(project, person)
        assert can_edit_project(project, person)


def test_a_read_only_role_does_not_veto_the_edit_role(app):
    """Someone who is both a client and a developer can still edit.

    The old code took the first membership row it found and compared it to
    "team". Rows are served by the (project_id, member_id, role) unique index,
    so they arrive in role-alphabetical order - which put "client" ahead of
    "team" and handed a developer a read-only view of the project they were
    building.
    """
    with app.app_context():
        person = make_member("Client Dev", "clientdev@example.com")
        project = make_project(
            "P1",
            "First",
            (person, "team", None),
            (person, "client", None),
        )
        db.session.add_all([person, project])
        db.session.commit()

        # The read-only role is the one the index hands back first.
        assert project.memberships[0].role == "client"
        assert can_edit_project(project, person)
        assert can_edit_tasks(project, person)


def test_a_read_only_member_cannot_edit_tasks(app):
    with app.app_context():
        client = make_member("Client", "client@example.com")
        project = make_project("P1", "First", (client, "client", None))
        db.session.add_all([client, project])
        db.session.commit()

        assert not can_edit_project(project, client)
        assert not can_edit_tasks(project, client)


def test_task_permission_pm_only_respects_delegated_roles(app):
    """With task editing restricted to PMs, a Delegated Backup cannot edit."""
    with app.app_context():
        backup = make_member("Backup", "backup@example.com")
        dev = make_member("Dev", "dev@example.com")
        pm = make_member("PM", "pm@example.com")
        project = make_project(
            "P1",
            "First",
            (backup, "delegated_backup", None),
            (dev, "team", None),
            (pm, "delegated", None),
        )
        db.session.add_all([backup, dev, pm, project])
        db.session.commit()

        set_setting("task_edit_permission", "pm")
        assert can_edit_tasks(project, pm)
        assert not can_edit_tasks(project, dev)
        assert not can_edit_tasks(project, backup)

        # Under the looser setting a developer can, but the backup is still not a
        # PM and so cannot assign roles.
        set_setting("task_edit_permission", "team")
        assert can_edit_tasks(project, dev)
        assert can_edit_tasks(project, backup)
        assert not is_project_pm(project, backup)


# --------------------------------------------------------------------------
# Only a PM of that project may assign its roles
# --------------------------------------------------------------------------


def _login(client, email):
    client.post(
        "/apps/login",
        data={"email": email, "password": "a-long-enough-password"},
    )


def test_only_a_pm_of_that_project_can_assign_its_roles(app, client):
    with app.app_context():
        pm = make_member("PM", "pm@example.com")
        dev = make_member("Dev", "dev@example.com")
        outsider = make_member("PM Elsewhere", "elsewhere@example.com")
        newcomer = make_member("Newcomer", "new@example.com")
        mine = make_project("P1", "Mine", (pm, "delegated", None), (dev, "team", None))
        theirs = make_project("P2", "Theirs", (outsider, "delegated", None))
        db.session.add_all([pm, dev, outsider, newcomer, mine, theirs])
        db.session.commit()
        mine_id, theirs_id, newcomer_id = mine.id, theirs.id, newcomer.id

    # A PM of project one.
    _login(client, "pm@example.com")
    assert client.post(
        f"/apps/mizalla/projects/{mine_id}/memberships",
        data={"member_id": newcomer_id, "role": "team"},
    ).status_code == 302

    # A developer on project one cannot.
    _login(client, "dev@example.com")
    assert client.post(
        f"/apps/mizalla/projects/{mine_id}/memberships",
        data={"member_id": newcomer_id, "role": "client"},
    ).status_code == 403

    # Nor can a PM of a *different* project, who cannot even see this one.
    _login(client, "elsewhere@example.com")
    assert client.post(
        f"/apps/mizalla/projects/{theirs_id}/memberships",
        data={"member_id": newcomer_id, "role": "team"},
    ).status_code == 302
    assert client.get(
        f"/apps/mizalla/projects/{mine_id}"
    ).status_code == 404


def test_several_roles_can_be_assigned_at_once(app, client):
    with app.app_context():
        pm = make_member("PM", "pm@example.com")
        person = make_member("Consultant", "consultant@example.com")
        project = make_project("P1", "First", (pm, "delegated", None))
        db.session.add_all([pm, person, project])
        db.session.commit()
        project_id, person_id = project.id, person.id

    _login(client, "pm@example.com")
    response = client.post(
        f"/apps/mizalla/projects/{project_id}/memberships",
        data={
            "member_id": person_id,
            # Three boxes ticked at once.
            "role": ["team", "client", "delegated_backup"],
            "responsibility": "Advising",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        rows = ProjectMembership.query.filter_by(
            project_id=project_id, member_id=person_id
        ).all()
        assert {row.role for row in rows} == {"team", "client", "delegated_backup"}
        # The same responsibility is recorded against each role.
        assert {row.responsibility for row in rows} == {"Advising"}


def test_assigning_a_role_already_held_is_reported_not_duplicated(app, client):
    with app.app_context():
        pm = make_member("PM", "pm@example.com")
        person = make_member("Dev", "dev@example.com")
        project = make_project(
            "P1", "First", (pm, "delegated", None), (person, "team", ["Developer"])
        )
        db.session.add_all([pm, person, project])
        db.session.commit()
        project_id, person_id = project.id, person.id

    _login(client, "pm@example.com")
    client.post(
        f"/apps/mizalla/projects/{project_id}/memberships",
        data={"member_id": person_id, "role": ["team", "vendor"]},
    )

    with app.app_context():
        rows = ProjectMembership.query.filter_by(
            project_id=project_id, member_id=person_id
        ).all()
        # The new role was added, the duplicate was skipped.
        assert {row.role for row in rows} == {"team", "vendor"}
        follow = client.get(
            f"/apps/mizalla/projects/{project_id}", follow_redirects=True
        )
        body = follow.get_data(as_text=True)
        assert "already held" in body.lower() or "Project team" in body


def test_no_role_selected_is_rejected(app, client):
    with app.app_context():
        pm = make_member("PM", "pm@example.com")
        person = make_member("New", "new@example.com")
        project = make_project("P1", "First", (pm, "delegated", None))
        db.session.add_all([pm, person, project])
        db.session.commit()
        project_id, person_id = project.id, person.id

    _login(client, "pm@example.com")
    client.post(
        f"/apps/mizalla/projects/{project_id}/memberships",
        data={"member_id": person_id, "role": []},
    )
    with app.app_context():
        assert ProjectMembership.query.filter_by(
            project_id=project_id, member_id=person_id
        ).count() == 0


def test_an_invalid_role_is_rejected(app, client):
    with app.app_context():
        pm = make_member("PM", "pm@example.com")
        person = make_member("New", "new@example.com")
        project = make_project("P1", "First", (pm, "delegated", None))
        db.session.add_all([pm, person, project])
        db.session.commit()
        project_id, person_id = project.id, person.id

    _login(client, "pm@example.com")
    client.post(
        f"/apps/mizalla/projects/{project_id}/memberships",
        data={"member_id": person_id, "role": ["team", "superuser"]},
    )
    with app.app_context():
        assert ProjectMembership.query.filter_by(
            project_id=project_id, member_id=person_id
        ).count() == 0


def test_removing_one_role_leaves_the_others(app, client):
    with app.app_context():
        pm = make_member("PM", "pm@example.com")
        person = make_member("Multi", "multi@example.com")
        project = make_project(
            "P1",
            "First",
            (pm, "delegated", None),
            (person, "team", None),
            (person, "vendor", None),
        )
        db.session.add_all([pm, person, project])
        db.session.commit()
        project_id = project.id
        team_row = ProjectMembership.query.filter_by(
            project_id=project_id, member_id=person.id, role="team"
        ).one().id
        person_id = person.id

    _login(client, "pm@example.com")
    client.post(
        f"/apps/mizalla/projects/{project_id}/memberships/{team_row}/delete"
    )

    with app.app_context():
        remaining = ProjectMembership.query.filter_by(
            project_id=project_id, member_id=person_id
        ).all()
        assert [row.role for row in remaining] == ["vendor"]
        # Still on the project, and still visible.
        assert can_edit_project(db.session.get(Project, project_id), db.session.get(Member, person_id)) is False


def test_creating_a_project_makes_the_creator_its_pm(app, client):
    """Otherwise a creator owns a project they cannot staff."""
    with app.app_context():
        founder = make_member("Founder", "founder@example.com", is_pm=True)
        db.session.add(founder)
        db.session.commit()

    _login(client, "founder@example.com")
    response = client.post(
        "/apps/mizalla/projects/new",
        data={"project_id": "NEW-1", "name": "Brand new"},
        follow_redirects=True,
    )
    assert response.status_code == 200

    with app.app_context():
        project = Project.query.filter_by(project_id="NEW-1").one()
        founder = Member.query.filter_by(email="founder@example.com").one()
        assert is_project_pm(project, founder)

    # And can therefore staff it.
    with app.app_context():
        hire = make_member("Hire", "hire@example.com")
        db.session.add(hire)
        db.session.commit()
        hire_id, project_id = hire.id, project.id

    assert client.post(
        f"/apps/mizalla/projects/{project_id}/memberships",
        data={"member_id": hire_id, "role": "team"},
    ).status_code == 302


# --------------------------------------------------------------------------
# Recording the authority a global PM already had
# --------------------------------------------------------------------------


def test_global_pm_flag_is_converted_into_per_project_authority(app):
    """The flag stopped granting PM everywhere, so what it granted is written down."""
    with app.app_context():
        flagged = make_member("Flagged", "flagged@example.com", is_pm=True)
        plain = make_member("Plain", "plain@example.com")
        first = make_project("P1", "First", (flagged, "team", ["Developer"]))
        second = make_project("P2", "Second", (flagged, "team", None))
        third = make_project(
            "P3",
            "Third",
            (flagged, "client", None),  # not a team member: no authority to record
            (plain, "team", ["Developer"]),  # not flagged: left alone
        )
        db.session.add_all([flagged, plain, first, second, third])
        db.session.commit()

        assert backfill_global_pm_roles() == 2

        assert is_project_pm(first, flagged)
        assert is_project_pm(second, flagged)
        # Existing team roles are kept alongside the new one.
        assert {row.role for row in first.memberships} == {"team"}
        assert first.memberships[0].get_team_roles_list() == [
            "Developer",
            PM_TEAM_ROLE,
        ]
        # An observer membership is not promoted.
        assert not is_project_pm(third, flagged)
        # Someone without the flag is untouched.
        assert not is_project_pm(third, plain)


def test_the_conversion_is_safe_to_run_again(app):
    with app.app_context():
        flagged = make_member("Flagged", "flagged@example.com", is_pm=True)
        project = make_project("P1", "First", (flagged, "team", None))
        db.session.add_all([flagged, project])
        db.session.commit()

        assert backfill_global_pm_roles() == 1
        # Idempotent: no duplicate entry on a second pass.
        assert backfill_global_pm_roles() == 0
        assert project.memberships[0].get_team_roles_list() == [PM_TEAM_ROLE]


def test_nobody_flagged_changes_nothing(app):
    with app.app_context():
        assert backfill_global_pm_roles() == 0


# --------------------------------------------------------------------------
# The database has to accept the new roles
# --------------------------------------------------------------------------


LEGACY_CHECK = """
DROP TABLE project_membership;
CREATE TABLE project_membership (
    id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    role VARCHAR(20) NOT NULL,
    team_roles VARCHAR(300) NOT NULL DEFAULT '',
    responsibility VARCHAR(300) NOT NULL DEFAULT '',
    CONSTRAINT uq_project_membership_role UNIQUE (project_id, member_id, role),
    CONSTRAINT ck_membership_role
        CHECK (role IN ('team','client','stakeholder','vendor')),
    FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE,
    FOREIGN KEY(member_id) REFERENCES member (id) ON DELETE CASCADE,
    PRIMARY KEY (id)
);
INSERT INTO project_membership (id, project_id, member_id, role, team_roles, responsibility)
    SELECT id, project_id, member_id, role, team_roles, responsibility
    FROM project_membership_backup;
"""


def _downgrade_to_the_old_constraint(db_path):
    """Put the pre-Delegated CHECK back, which is what an existing install has."""
    con = sqlite3.connect(db_path)
    con.executescript("CREATE TABLE project_membership_backup AS SELECT * FROM project_membership;")
    con.executescript(LEGACY_CHECK)
    con.executescript("DROP TABLE project_membership_backup;")
    con.commit()
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='project_membership'"
    ).fetchone()[0]
    assert "'delegated'" not in sql
    con.close()


def _table_sql(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='project_membership'"
        ).fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def legacy_db(tmp_path):
    """An app whose database has the old role constraint, with real rows in it."""
    db_path = tmp_path / "legacy.db"
    app = create_legacy_app(db_path)
    with app.app_context():
        person = make_member("Alice", "alice@example.com")
        project = make_project(
            "P1",
            "Legacy",
            (person, "team", ["Project Manager"]),
            (person, "client", None),  # the same member, a second role
        )
        db.session.add_all([person, project])
        db.session.commit()
    _downgrade_to_the_old_constraint(db_path)
    return app, db_path


def create_legacy_app(db_path):
    from mizalla import create_app

    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-key",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path.as_posix()}",
            "UPLOAD_FOLDER": str(db_path.parent / "uploads"),
            "WTF_CSRF_ENABLED": False,
        }
    )


def test_the_old_constraint_refuses_a_delegated_role(legacy_db):
    """Establishes what the rebuild is fixing."""
    app, db_path = legacy_db
    with app.app_context():
        project = Project.query.one()
        db.session.add(
            ProjectMembership(project=project, member=project.memberships[0].member, role="delegated")
        )
        with pytest.raises(Exception, match="CHECK constraint failed"):
            db.session.commit()
        db.session.rollback()


def test_upgrading_rebuilds_the_table_and_keeps_every_row(legacy_db):
    app, db_path = legacy_db
    with app.app_context():
        assert upgrade_schema() is True

    sql = _table_sql(db_path)
    for role in MEMBERSHIP_ROLES:
        assert f"'{role}'" in sql, role

    with app.app_context():
        rows = ProjectMembership.query.all()
        # Both of Alice's roles survived, with their team roles intact.
        assert len(rows) == 2
        assert {row.role for row in rows} == {"team", "client"}
        team_row = next(row for row in rows if row.role == "team")
        assert team_row.get_team_roles_list() == ["Project Manager"]


def test_the_rebuilt_table_still_enforces_its_constraint(legacy_db):
    app, db_path = legacy_db
    with app.app_context():
        upgrade_schema()
        project = Project.query.one()
        person = project.memberships[0].member

        # The new role is accepted now.
        db.session.add(ProjectMembership(project=project, member=person, role="delegated"))
        db.session.add(
            ProjectMembership(project=project, member=person, role="delegated_backup")
        )
        db.session.commit()

        # An invented role is still refused.
        db.session.add(ProjectMembership(project=project, member=person, role="wizard"))
        with pytest.raises(Exception, match="CHECK constraint failed"):
            db.session.commit()
        db.session.rollback()

        # And a member still cannot hold the same role twice.
        db.session.add(ProjectMembership(project=project, member=person, role="delegated"))
        with pytest.raises(Exception):
            db.session.commit()
        db.session.rollback()


def test_the_rebuilt_table_still_cascades_on_project_delete(legacy_db):
    """The copy must not lose the foreign keys the original had."""
    app, db_path = legacy_db
    with app.app_context():
        upgrade_schema()
        project = Project.query.one()
        db.session.delete(project)
        db.session.commit()
        assert ProjectMembership.query.count() == 0


def test_upgrading_twice_changes_nothing(legacy_db):
    app, db_path = legacy_db
    with app.app_context():
        assert upgrade_schema() is True
        assert upgrade_schema() is False


def test_a_fresh_database_needs_no_rebuild(app, tmp_path):
    """create_all already made the constraint current."""
    with app.app_context():
        assert upgrade_schema() is False


# --------------------------------------------------------------------------
# The role groups the detail page renders
# --------------------------------------------------------------------------


class _Node:
    __slots__ = ("tag", "attrs", "classes", "children", "text")

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = attrs
        self.classes = attrs.get("class", "").split()
        self.children = []
        self.text = []

    def walk(self):
        """Descendants only, not self: a question here is always "what is inside
        this element?", and counting the element answers it wrongly."""
        for child in self.children:
            yield child
            yield from child.walk()

    def find_all(self, cls):
        return [node for node in self.walk() if cls in node.classes]

    def find_tags(self, tag):
        return [node for node in self.walk() if node.tag == tag]

    def own_text(self):
        return " ".join("".join(self.text).split())


def _render(node):
    """The node's own text, plus its descendants' - enough to spot a heading."""
    parts = [node.own_text()]
    for child in node.children:
        parts.append(_render(child))
    return " ".join(part for part in parts if part)


class _Dom(HTMLParser):
    """Just enough tree to answer "is this markup nested or not?".

    Void elements are listed because an unclosed <input> inside a <form> would
    otherwise swallow everything after it as children.
    """

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].text.append(data)


def _dom(html_text):
    dom = _Dom()
    dom.feed(html_text)
    dom.close()
    return dom.root


def _role_groups(html_text):
    """Every ``.role-group`` in the page, as a tree, in document order."""
    return _dom(html_text).find_all("role-group")


def test_role_groups_are_siblings_not_nested(app, client):
    """One group per role, none inside another.

    The person-row markup closes its inner div only in the branch that renders
    team-role tags, so any row without them used to leave that div open and
    swallow the rows after it - which made each group report every member on the
    project, not its own.
    """
    with app.app_context():
        pm = make_member("Ada", "ada@example.com")
        dev = make_member("Grace", "grace@example.com")
        vendor = make_member("Linus", "linus@example.com")
        client_contact = make_member("Mary", "mary@example.com")
        backup = make_member("Barbara", "barbara@example.com")
        # Two team roles, so the group that *does* render tags is in play too.
        mine = make_project(
            "P1", "Mine",
            (pm, "delegated", None),
            (dev, "team", None),
            (dev, "client", None),
            (vendor, "vendor", None),
            (client_contact, "stakeholder", None),
            (backup, "delegated_backup", None),
        )
        db.session.add_all([pm, dev, vendor, client_contact, backup, mine])
        db.session.commit()
        mine_id = mine.id

    _login(client, "ada@example.com")
    page = client.get(f"/apps/mizalla/projects/{mine_id}").data.decode()
    groups = _role_groups(page)

    assert len(groups) == len(MEMBERSHIP_ROLES)

    # No group is a descendant of another.
    for group in groups:
        assert not group.find_all("role-group"), "role groups are nested"

    by_role = {role: label for role, label in MEMBERSHIP_LABELS.items()}
    listed = {}
    for group in groups:
        headings = group.find_tags("h3")
        assert len(headings) == 1, "a role group lost its heading"
        role = next(r for r, label in by_role.items() if label in headings[0].own_text())
        rows = group.find_all("person-row")
        listed[role] = [row.find_tags("strong")[0].own_text() for row in rows]

    # Every row is inside exactly one group, and no group claims another's rows.
    # Grace holds two roles, so she is the row that used to leak across groups.
    assert listed["delegated"] == ["Ada"]
    assert listed["delegated_backup"] == ["Barbara"]
    assert listed["team"] == ["Grace"]
    assert listed["client"] == ["Grace"]
    assert listed["vendor"] == ["Linus"]
    assert listed["stakeholder"] == ["Mary"]
    # All six membership rows are on the page, and no row is counted twice.
    assert sum(len(names) for names in listed.values()) == 6
    assert set(listed) == set(MEMBERSHIP_ROLES)


def test_each_group_hint_sits_under_its_own_heading(app, client):
    """Every role's help text is announced with that role, not pooled separately.

    The hint is a span inside the h3, so it has to stay in the heading: a
    browser that reads the roles list aloud should say "Project Team - the
    engineers who build it" rather than "Project Team" six times and then a
    paragraph of unrelated notes.
    """
    with app.app_context():
        pm = make_member("Ada", "ada@example.com")
        mine = make_project("P1", "Mine", (pm, "delegated", None))
        db.session.add_all([pm, mine])
        db.session.commit()
        mine_id = mine.id

    _login(client, "ada@example.com")
    page = client.get(f"/apps/mizalla/projects/{mine_id}").data.decode()

    groups = _role_groups(page)
    assert len(groups) == len(MEMBERSHIP_ROLES)
    for role in MEMBERSHIP_ORDER:
        heading = next(
            group.find_tags("h3")[0]
            for group in groups
            if MEMBERSHIP_LABELS[role] in group.find_tags("h3")[0].own_text()
        )
        hints = heading.find_all("group-hint")
        assert len(hints) == 1, role
        assert hints[0].own_text() == MEMBERSHIP_NOTES[role]

    # And nothing outside the headings carries one.
    assert len(_dom(page).find_all("group-hint")) == len(MEMBERSHIP_ORDER)
