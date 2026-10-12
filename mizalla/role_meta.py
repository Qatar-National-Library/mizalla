"""Per-project role metadata: the single source of truth for what a role means.

A role says nothing about a member in general - it is scoped to one project, so
the same person can be a Delegated PM on one project, a Developer on the next,
and a Vendor on a third. A member may hold several roles on the *same* project,
which is why ``ProjectMembership`` is one row per (project, member, role).

``MEMBERSHIP_ROLES`` feeds the column's CHECK constraint, so the two cannot
drift. ``MEMBERSHIP_LABELS`` and ``MEMBERSHIP_ORDER`` are what the screens show.
Authorisation is not here: :mod:`mizalla.access` maps each role onto the
capabilities it grants.
"""

#: Every role a project membership may hold. Order is the order the database
#: constraint is generated in, so it must be a superset of what access.py knows.
MEMBERSHIP_ROLES = (
    "team",
    "client",
    "stakeholder",
    "vendor",
    "delegated",
    "delegated_backup",
)

#: The team role that promotes a Project team member to Project Manager. A
#: Delegated PM holds PM authority through its membership role instead.
PM_TEAM_ROLE = "Project Manager"

MEMBERSHIP_LABELS = {
    "team": "Project team",
    "client": "Client",
    "stakeholder": "Stakeholder",
    "vendor": "Vendor",
    "delegated": "Delegated PM",
    "delegated_backup": "Delegated Backup",
}

#: One line of help under each role in the "assign roles" picker, so the
#: difference between a Developer and a Delegated PM is stated where the choice
#: is made rather than only in the documentation.
MEMBERSHIP_NOTES = {
    "team": "Builds the project: can add and edit tasks, steps, meetings, and documents.",
    "client": "Read-only. Sees the project and downloads its documents.",
    "stakeholder": "Read-only. Sees the project and downloads its documents.",
    "vendor": "Read-only. Sees the project and downloads its documents.",
    "delegated": "Acts for the Project Manager on this project, including assigning roles.",
    "delegated_backup": "Stands in for a Delegated PM's day-to-day work: same access as a Developer.",
}

#: Display order for the "Project roles" sections. Delegated roles lead because
#: they carry the authority; the read-only roles trail.
MEMBERSHIP_ORDER = (
    "delegated",
    "team",
    "delegated_backup",
    "client",
    "stakeholder",
    "vendor",
)

#: The roles that put someone on the delivery side of a project: they can work on
#: it, and two of them carry authority. The project screen lists these under
#: "Member management", because they are the roles a PM staffs the project with.
DELIVERY_ROLES = ("delegated", "team", "delegated_backup")

#: The roles that sit outside the delivery team: people who need to see the
#: project and are not working on it. The project screen lists these under
#: "Business contacts" alongside the owner departments, because that answers
#: "who needs to see this?" as opposed to "who is building it?".
EXTERNAL_ROLES = ("client", "stakeholder", "vendor")
