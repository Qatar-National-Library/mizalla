from functools import wraps
from flask import Blueprint, jsonify, request

from .extensions import db
from .jira import is_valid_jira_ticket_id
from .access import is_pm_membership
from .models import (
    MEMBERSHIP_ORDER,
    MEMBERSHIP_ROLES,
    PM_TEAM_ROLE,
    TASK_STATUSES,
    TASK_TYPES,
    Member,
    Project,
    ProjectAuditEvent,
    ProjectMembership,
    Task,
    TaskStep,
)
from .highlights import apply_highlights, apply_milestone, normalise_highlights
from .progress import project_completion
from .settings import get_project_team_roles_list, get_task_highlight_types_list
from .validators import bounded_int, optional_date

bp = Blueprint("api", __name__, url_prefix="/apps/mizalla/api/v1")


def _task_payload(task: Task) -> dict:
    """Serialise a task, including the highlight labels and milestone state."""
    return {
        "id": task.id,
        "name": task.name,
        "task_type": task.task_type,
        "status": task.status,
        "priority": task.priority,
        "weight": task.weight,
        "jira_ticket_id": task.jira_ticket_id,
        "highlights": task.get_highlight_list(),
        "highlighted_on": task.highlighted_on.isoformat() if task.highlighted_on else None,
        "is_milestone": task.is_milestone,
        "milestone_achieved_on": task.milestone_achieved_on.isoformat() if task.milestone_achieved_on else None,
        "percentage_complete": task.task_completion,
    }


def api_auth_required(f):
    """Authenticate an API caller by API key only.

    Session cookies are deliberately not accepted. The api blueprint is CSRF
    exempt, so honouring ``current_user`` here would let a logged-in member be
    made to issue state-changing requests from another site.
    """

    @wraps(f)
    def decorated(*args, **kwargs):
        member = None
        auth_header = request.headers.get("Authorization", "")
        api_key_header = request.headers.get("X-API-Key", "")

        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            member = db.session.scalar(db.select(Member).filter_by(api_key=token)) if token else None
        elif api_key_header:
            token = api_key_header.strip()
            member = db.session.scalar(db.select(Member).filter_by(api_key=token)) if token else None

        if member is None:
            return jsonify({"error": "Authentication required. Provide a valid Bearer token or API key."}), 401

        request.api_member = member
        return f(*args, **kwargs)

    return decorated


def _editable_project_for(project_id: int) -> Project | None:
    """Return the project when the API member holds the team role, else None."""
    project = db.session.get(Project, project_id)
    if not project:
        return None
    if not ProjectMembership.query.filter_by(
        project_id=project.id, member_id=request.api_member.id, role="team"
    ).first():
        return None
    return project


def _pm_project_for(project_id: int) -> Project | None:
    """Return the project when the API member manages it, else None.

    Assigning roles is reserved for a PM of that project. A Delegated PM role
    counts, and so does a Project team membership holding the "Project Manager"
    team role - the member's global is_pm flag is not consulted.
    """
    from .access import is_project_pm

    project = db.session.get(Project, project_id)
    if not project:
        return None
    return project if is_project_pm(project, request.api_member) else None


def _editable_task_for(task_id: int) -> Task | None:
    task = db.session.get(Task, task_id)
    if not task or not _editable_project_for(task.project_id):
        return None
    return task


def _editable_step_for(step_id: int) -> TaskStep | None:
    step = db.session.get(TaskStep, step_id)
    if not step or not _editable_project_for(step.task.project_id):
        return None
    return step


def _record_api_change(project: Project, action: str, entity_type: str, summary: str, entity_id: int | None = None):
    actor = request.api_member
    db.session.add(
        ProjectAuditEvent(
            project=project,
            project_key=project.project_id,
            project_name=project.name,
            actor_id=actor.id,
            actor_name=actor.name,
            actor_email=actor.email,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary[:500],
        )
    )


@bp.get("/projects")
@api_auth_required
def list_projects():
    member = request.api_member
    projects = (
        Project.query.join(ProjectMembership)
        .filter(ProjectMembership.member_id == member.id, ProjectMembership.role.in_(MEMBERSHIP_ROLES))
        .distinct()
        .order_by(Project.name)
        .all()
    )
    return jsonify(
        [
            {
                "id": p.id,
                "project_id": p.project_id,
                "name": p.name,
                "description": p.description,
                "scope": p.scope,
                "completion_percentage": project_completion(p.tasks),
                "estimated_completion_date": p.estimated_completion_date.isoformat() if p.estimated_completion_date else None,
            }
            for p in projects
        ]
    )


@bp.post("/projects")
@api_auth_required
def create_project():
    data = request.get_json() or {}
    project_id = data.get("project_id", "").strip()
    name = data.get("name", "").strip()
    if not project_id or not name:
        return jsonify({"error": "project_id and name are required."}), 400
    if Project.query.filter_by(project_id=project_id).first():
        return jsonify({"error": "That project_id is already in use."}), 400

    try:
        est_date = optional_date(data.get("estimated_completion_date"), "estimated_completion_date")
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    project = Project(
        project_id=project_id,
        name=name,
        description=data.get("description", "").strip(),
        scope=data.get("scope", "").strip(),
        estimated_completion_date=est_date,
    )
    # The creator runs the project, so they get PM authority on it. Without it
    # their key could create the project but not assign any role on it.
    owner = ProjectMembership(member=request.api_member, role="team")
    owner.set_team_roles_list([PM_TEAM_ROLE])
    project.memberships.append(owner)
    db.session.add(project)
    db.session.flush()
    _record_api_change(project, "CREATE", "Project", f"Created project {project.project_id} via API", project.id)
    db.session.commit()
    return jsonify({"id": project.id, "project_id": project.project_id, "name": project.name}), 201


@bp.get("/projects/<int:project_id>")
@api_auth_required
def get_project(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        return jsonify({"error": "Project not found."}), 404
    visible = ProjectMembership.query.filter_by(project_id=project.id, member_id=request.api_member.id).first()
    if not visible:
        return jsonify({"error": "Project not found."}), 404

    return jsonify(
        {
            "id": project.id,
            "project_id": project.project_id,
            "name": project.name,
            "description": project.description,
            "scope": project.scope,
            "completion": project_completion(project.tasks),
            # One entry per membership row, so filtering on "role" still finds
            # everyone holding that role. "roles" carries the member's full set,
            # which is what a caller needs when someone holds several.
            "members": [
                {
                    "name": m.member.name,
                    "email": m.member.email,
                    "role": m.role,
                    "roles": sorted(
                        {r.role for r in project.memberships if r.member_id == m.member_id}
                    ),
                    "is_project_manager": is_pm_membership(m),
                    "team_roles": m.get_team_roles_list(),
                    "responsibility": m.responsibility,
                }
                for m in project.memberships
            ],
            "tasks": [
                {
                    **_task_payload(t),
                    "steps": [{"id": s.id, "header": s.header, "percentage_complete": s.percentage_complete} for s in t.steps],
                }
                for t in project.tasks
            ],
            "meetings": [{"id": m.id, "date": m.meeting_date.isoformat(), "agenda": m.agenda} for m in project.meetings],
        }
    )


@bp.post("/projects/<int:project_id>/members")
@api_auth_required
def add_member(project_id):
    # Only a PM of the project may assign its roles. A plain team member - even
    # one who can edit the project's tasks - cannot.
    project = _pm_project_for(project_id)
    if not project:
        return jsonify({"error": "Project not found or you do not manage it."}), 403

    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    team_roles = data.get("team_roles", [])
    # "role" stays a plain string for existing callers; "roles" takes a list for
    # assigning several at once, which a member is allowed to hold.
    requested = data.get("roles")
    if requested is None:
        requested = [data.get("role", "")]
    if not isinstance(requested, list) or not all(isinstance(r, str) for r in requested):
        return jsonify({"error": "roles must be a list of role names."}), 400
    if not requested or any(r not in MEMBERSHIP_ROLES for r in requested):
        return jsonify({"error": "Invalid role."}), 400

    target = db.session.scalar(db.select(Member).filter_by(email=email))
    if not target:
        return jsonify({"error": "Target member email not found."}), 404

    chosen = [role for role in MEMBERSHIP_ORDER if role in set(requested)]
    held = {
        row.role
        for row in ProjectMembership.query.filter(
            ProjectMembership.project_id == project.id,
            ProjectMembership.member_id == target.id,
            ProjectMembership.role.in_(chosen),
        ).all()
    }
    new_roles = [role for role in chosen if role not in held]
    if not new_roles:
        return jsonify({"error": "Member already has this project role."}), 400

    created = []
    for role in new_roles:
        membership = ProjectMembership(
            project=project, member=target, role=role,
            responsibility=data.get("responsibility", "").strip(),
        )
        if role == "team" and team_roles:
            allowed = set(get_project_team_roles_list())
            membership.set_team_roles_list([r for r in team_roles if r in allowed])
        db.session.add(membership)
        created.append(membership)

    db.session.flush()
    _record_api_change(
        project, "CREATE", "Membership",
        f"Added {target.name} as {', '.join(new_roles)} via API", created[0].id,
    )
    db.session.commit()
    return jsonify(
        {
            "project_id": project.id,
            "email": target.email,
            "role": new_roles[0],
            "roles": new_roles,
            "already_held": [role for role in chosen if role in held],
            "team_roles": created[0].get_team_roles_list() if created[0].role == "team" else [],
        }
    ), 201


@bp.patch("/projects/<int:project_id>")
@api_auth_required
def update_project(project_id):
    project = _editable_project_for(project_id)
    if not project:
        return jsonify({"error": "Project not found or edit forbidden."}), 403

    data = request.get_json() or {}
    if "name" in data:
        name = data["name"].strip()
        if not name:
            return jsonify({"error": "name cannot be empty."}), 400
        project.name = name
    if "description" in data:
        project.description = data["description"].strip()
    if "scope" in data:
        project.scope = data["scope"].strip()
    if "estimated_completion_date" in data:
        try:
            project.estimated_completion_date = optional_date(
                data["estimated_completion_date"], "estimated_completion_date"
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    _record_api_change(project, "UPDATE", "Project", "Updated project details via API", project.id)
    db.session.commit()
    return jsonify({"id": project.id, "project_id": project.project_id, "name": project.name})


@bp.post("/projects/<int:project_id>/tasks")
@api_auth_required
def create_task(project_id):
    project = _editable_project_for(project_id)
    if not project:
        return jsonify({"error": "Project not found or edit forbidden."}), 403

    data = request.get_json() or {}
    name = data.get("name", "").strip()
    task_type = data.get("task_type", "")
    status = data.get("status", "Not Started")
    if not name or task_type not in TASK_TYPES or status not in TASK_STATUSES:
        return jsonify({"error": "Valid name, task_type, and status are required."}), 400

    jira = data.get("jira_ticket_id", "").strip() or None
    if jira and (len(jira) > 80 or not is_valid_jira_ticket_id(jira)):
        return jsonify({"error": "Invalid JIRA ticket ID format."}), 400

    try:
        priority = bounded_int(data.get("priority", 5), "priority", 1, 10)
        weight = bounded_int(data.get("weight", 5), "weight", 1, 10)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    try:
        highlights = normalise_highlights(data.get("highlights", []), get_task_highlight_types_list())
        marked_on = optional_date(data.get("highlighted_on"), "highlighted_on")
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    task = Task(
        project=project,
        name=name,
        task_type=task_type,
        status=status,
        priority=priority,
        weight=weight,
        jira_ticket_id=jira,
    )
    apply_highlights(task, highlights, marked_on)
    apply_milestone(task, bool(data.get("is_milestone", False)), status, optional_date(data.get("milestone_achieved_on"), "milestone_achieved_on"))
    db.session.add(task)
    db.session.flush()
    _record_api_change(project, "CREATE", "Task", f"Created task {task.name} via API", task.id)
    db.session.commit()
    return jsonify(_task_payload(task)), 201


@bp.patch("/tasks/<int:task_id>")
@api_auth_required
def update_task(task_id):
    task = _editable_task_for(task_id)
    if not task:
        return jsonify({"error": "Task not found or edit forbidden."}), 403

    data = request.get_json() or {}
    if "name" in data:
        name = data["name"].strip()
        if not name:
            return jsonify({"error": "name cannot be empty."}), 400
        task.name = name
    if "status" in data:
        if data["status"] not in TASK_STATUSES:
            return jsonify({"error": "Invalid task status."}), 400
        task.status = data["status"]
    if "priority" in data:
        try:
            task.priority = bounded_int(data["priority"], "priority", 1, 10)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
    if "weight" in data:
        try:
            task.weight = bounded_int(data["weight"], "weight", 1, 10)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
    if "highlights" in data:
        try:
            labels = normalise_highlights(data["highlights"], get_task_highlight_types_list())
            marked_on = optional_date(data["highlighted_on"], "highlighted_on") if "highlighted_on" in data else task.highlighted_on
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        apply_highlights(task, labels, marked_on)
    elif "highlighted_on" in data:
        try:
            apply_highlights(task, task.get_highlight_list(), optional_date(data["highlighted_on"], "highlighted_on"))
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
    if "is_milestone" in data:
        try:
            apply_milestone(
                task,
                bool(data["is_milestone"]),
                task.status,
                optional_date(data["milestone_achieved_on"], "milestone_achieved_on") if "milestone_achieved_on" in data else None,
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
    elif "milestone_achieved_on" in data:
        try:
            apply_milestone(task, task.is_milestone, task.status, optional_date(data["milestone_achieved_on"], "milestone_achieved_on"))
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    _record_api_change(task.project, "UPDATE", "Task", f"Updated task {task.name} via API", task.id)
    db.session.commit()
    return jsonify(_task_payload(task))


@bp.post("/tasks/<int:task_id>/steps")
@api_auth_required
def add_step(task_id):
    task = _editable_task_for(task_id)
    if not task:
        return jsonify({"error": "Task not found or edit forbidden."}), 403

    data = request.get_json() or {}
    header = data.get("header", "").strip()
    if not header:
        return jsonify({"error": "header is required."}), 400

    try:
        priority = bounded_int(data.get("priority", 1), "priority", 1, 10000)
        percentage = bounded_int(data.get("percentage_complete", 0), "percentage_complete", 0, 100)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    step = TaskStep(
        task=task,
        priority=priority,
        header=header,
        percentage_complete=percentage,
        description=data.get("description", "").strip(),
    )
    db.session.add(step)
    db.session.flush()
    _record_api_change(task.project, "CREATE", "Task step", f"Added step '{step.header}' via API", step.id)
    db.session.commit()
    return jsonify({"id": step.id, "header": step.header, "percentage_complete": step.percentage_complete}), 201


@bp.patch("/steps/<int:step_id>")
@api_auth_required
def update_step(step_id):
    step = _editable_step_for(step_id)
    if not step:
        return jsonify({"error": "Step not found or edit forbidden."}), 403

    data = request.get_json() or {}
    if "header" in data:
        header = data["header"].strip()
        if not header:
            return jsonify({"error": "header cannot be empty."}), 400
        step.header = header
    if "percentage_complete" in data:
        try:
            step.percentage_complete = bounded_int(data["percentage_complete"], "percentage_complete", 0, 100)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
    if "priority" in data:
        try:
            step.priority = bounded_int(data["priority"], "priority", 1, 10000)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    _record_api_change(step.task.project, "UPDATE", "Task step", f"Updated step '{step.header}' via API", step.id)
    db.session.commit()
    return jsonify({"id": step.id, "header": step.header, "percentage_complete": step.percentage_complete})
