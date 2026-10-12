"""Who may see and change what, resolved from per-project roles.

A role is scoped to a single project, and a member may hold several roles on that
same project. So every question here is answered by asking what the member's roles
on *this* project collectively grant, never by looking at one membership row and
assuming it is the only one.

The global ``is_pm`` flag on a member is deliberately not a grant: it used to make
someone a Project Manager on every project, which cannot be reconciled with a PM
of one project being a Developer on another. It now only means "may create new
projects", and project-level authority comes from the roles below.
"""

from flask import abort
from flask_login import current_user

from .extensions import db
from .models import MEMBERSHIP_ROLES, PM_TEAM_ROLE, Project, ProjectMembership

#: Capabilities a project role can grant.
CAN_VIEW = "view"
CAN_EDIT = "edit"
CAN_PM = "pm"

#: What each role grants. Read the three groups as:
#:
#: - ``delegated`` is a Project Manager by another name, so it edits and it
#:   assigns roles.
#: - ``team`` is what a Developer holds, and ``delegated_backup`` deliberately
#:   holds exactly the same, so a stand-in for a Delegated PM can do the day-to-day
#:   work without also being able to reassign anyone's role.
#: - the rest observe.
#:
#: A role missing from this table grants nothing, so a new role added to
#: ``MEMBERSHIP_ROLES`` starts read-only rather than accidentally open.
ROLE_GRANTS = {
    "team": frozenset({CAN_VIEW, CAN_EDIT}),
    "delegated_backup": frozenset({CAN_VIEW, CAN_EDIT}),
    "delegated": frozenset({CAN_VIEW, CAN_EDIT, CAN_PM}),
    "client": frozenset({CAN_VIEW}),
    "stakeholder": frozenset({CAN_VIEW}),
    "vendor": frozenset({CAN_VIEW}),
}


def role_grants(role: str, capability: str) -> bool:
    """True when a single ``role`` grants ``capability``."""
    return capability in ROLE_GRANTS.get(role, frozenset())


def project_memberships(project: Project, member) -> list[ProjectMembership]:
    """Every role ``member`` holds on ``project``, in insertion order."""
    return ProjectMembership.query.filter_by(
        project_id=project.id, member_id=member.id
    ).all()


def project_roles(project: Project, member) -> set[str]:
    """The set of roles ``member`` holds on ``project``."""
    return {row.role for row in project_memberships(project, member)}


def _member_for(member):
    return member if member is not None else current_user


def can_view_project(project: Project, member=None) -> bool:
    """True when the member holds any role at all on the project, or is an admin."""
    member = _member_for(member)
    return member.is_admin or bool(project_memberships(project, member))


def can_edit_project(project: Project, member=None) -> bool:
    """True when one of the member's roles lets them change project data.

    This is what used to be ``role == "team"``. Delegated PM and Delegated Backup
    are team-equivalent, so they pass too.
    """
    member = _member_for(member)
    if member.is_admin:
        return True
    return any(row.grants(CAN_EDIT) for row in project_memberships(project, member))


def is_project_pm(project: Project, member=None) -> bool:
    """True when the member manages this project.

    A PM is a Delegated PM on the project, or a Project team member holding the
    "Project Manager" team role. Administrators pass regardless. The member's
    global ``is_pm`` flag is not consulted - see the module docstring.
    """
    member = _member_for(member)
    if member.is_admin:
        return True
    for row in project_memberships(project, member):
        if row.grants(CAN_PM):
            return True
        if row.role == "team" and row.has_team_role(PM_TEAM_ROLE):
            return True
    return False


def is_pm_membership(membership) -> bool:
    """True when a single membership row carries Project Manager authority.

    A Delegated PM role does so on its own; a Project team membership needs the
    "Project Manager" team role alongside it. Used to mark the PMs in a project's
    people lists, where the question is about the row rather than the member.
    """
    return membership.grants(CAN_PM) or (
        membership.role == "team" and membership.has_team_role(PM_TEAM_ROLE)
    )


def visible_projects_query():
    return (
        Project.query.join(ProjectMembership)
        .filter(
            ProjectMembership.member_id == current_user.id,
            ProjectMembership.role.in_(MEMBERSHIP_ROLES),
        )
        .distinct()
    )


def get_visible_project(project_id: int) -> Project:
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)
    if not can_view_project(project):
        abort(404)
    return project


def is_project_team_member(project: Project) -> bool:
    """Backwards-compatible alias for :func:`can_edit_project`."""
    return can_edit_project(project)


def get_editable_project(project_id: int) -> Project:
    project = get_visible_project(project_id)
    if not can_edit_project(project):
        abort(403)
    return project


def get_deletable_project(project_id: int) -> Project:
    project = get_editable_project(project_id)
    if not current_user.is_admin:
        abort(403)
    return project
