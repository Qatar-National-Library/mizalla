from contextlib import nullcontext
from datetime import date, datetime, time
from flask import Flask, has_app_context
from mcp.server.fastmcp import FastMCP

from mizalla import create_app
from mizalla.extensions import db
from mizalla.highlights import apply_highlights, apply_milestone, milestones_for, normalise_highlights, at_risk_tasks
from mizalla.jira import is_valid_jira_ticket_id
from mizalla.models import (
    MEMBERSHIP_ROLES,
    TASK_SOURCE_TYPES,
    TASK_STATUSES,
    TASK_TYPES,
    Meeting,
    Member,
    Project,
    ProjectAuditEvent,
    ProjectDocument,
    ProjectMembership,
    Task,
    TaskStep,
)
from mizalla.progress import current_step, project_completion
from mizalla.secure_links import generate_signed_document_token
from mizalla.settings import get_project_team_roles_list, get_task_highlight_types_list
from mizalla.teams import create_teams_meeting_url

mcp = FastMCP("Mizalla MCP Service")

_default_app: Flask | None = None


def _get_default_app() -> Flask:
    """Build the fallback app on first use, never at import time.

    Creating the app here would run ``db.create_all()`` and ``upgrade_schema()`` against
    ``instance/mizalla.db``, so simply importing this module -- including from the
    test suite, which imports it to exercise the tools -- created and migrated the real
    database. The app is only needed when a tool runs outside an app context.
    """
    global _default_app
    if _default_app is None:
        _default_app = create_app()
    return _default_app


def _app_context():
    if has_app_context():
        return nullcontext()
    return _get_default_app().app_context()


def _get_member_by_email(email: str) -> Member | None:
    return db.session.scalar(db.select(Member).filter_by(email=email.strip().lower()))


def _verify_member_project_access(project_id: int, member_id: int) -> bool:
    return (
        ProjectMembership.query.filter_by(project_id=project_id, member_id=member_id)
        .filter(ProjectMembership.role.in_(MEMBERSHIP_ROLES))
        .first()
        is not None
    )


def _verify_member_editable_access(project_id: int, member_id: int) -> bool:
    return (
        ProjectMembership.query.filter_by(
            project_id=project_id, member_id=member_id, role="team"
        ).first()
        is not None
    )


def _record_audit(project: Project, member: Member, action: str, entity_type: str, summary: str, entity_id: int | None = None):
    db.session.add(
        ProjectAuditEvent(
            project=project,
            project_key=project.project_id,
            project_name=project.name,
            actor_id=member.id,
            actor_name=member.name,
            actor_email=member.email,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary[:500],
        )
    )


@mcp.tool()
def get_visible_projects(member_email: str) -> list[dict]:
    """
    List all projects visible to a specific team member by email address.
    Visibility is strictly scoped to projects where the member has a project role.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return [{"error": f"Member account with email '{member_email}' not found."}]

        projects = (
            Project.query.join(ProjectMembership)
            .filter(
                ProjectMembership.member_id == member.id,
                ProjectMembership.role.in_(MEMBERSHIP_ROLES),
            )
            .distinct()
            .order_by(Project.name)
            .all()
        )
        return [
            {
                "id": p.id,
                "project_id": p.project_id,
                "name": p.name,
                "description": p.description,
                "scope": p.scope,
                "completion_percentage": project_completion(p.tasks),
                "estimated_completion_date": p.estimated_completion_date.isoformat() if p.estimated_completion_date else None,
                "actual_start_date": p.actual_start_date.isoformat() if p.actual_start_date else None,
            }
            for p in projects
        ]


@mcp.tool()
def get_project_details(project_id: int, member_email: str) -> dict:
    """
    Retrieve full project details including tasks, meetings, and uploaded documents
    for a given member if they have visibility access.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        project = db.session.get(Project, project_id)
        if not project or not _verify_member_project_access(project.id, member.id):
            return {"error": "Project not found or member does not have access."}

        tasks = [
            {
                "id": t.id,
                "name": t.name,
                "task_type": t.task_type,
                "status": t.status,
                "priority": t.priority,
                "weight": t.weight,
                "jira_ticket_id": t.jira_ticket_id,
                "highlights": t.get_highlight_list(),
                "highlighted_on": t.highlighted_on.isoformat() if t.highlighted_on else None,
                "is_milestone": t.is_milestone,
                "milestone_achieved_on": t.milestone_achieved_on.isoformat() if t.milestone_achieved_on else None,
                "current_step": current_step(t).header if current_step(t) else None,
                "steps": [
                    {
                        "id": s.id,
                        "header": s.header,
                        "priority": s.priority,
                        "percentage_complete": s.percentage_complete,
                    }
                    for s in t.steps
                ],
            }
            for t in project.tasks
        ]
        all_tasks = project.tasks
        achieved_milestones, upcoming_milestones, open_milestones = milestones_for(all_tasks)
        return {
            "id": project.id,
            "project_id": project.project_id,
            "name": project.name,
            "description": project.description,
            "scope": project.scope,
            "completion": project_completion(all_tasks),
            "highlight_types": get_task_highlight_types_list(),
            "milestones": {
                "achieved": [
                    {
                        "id": m.id,
                        "name": m.name,
                        "milestone_achieved_on": m.milestone_achieved_on.isoformat() if m.milestone_achieved_on else None,
                        "due_date": m.due_date.isoformat() if m.due_date else None,
                    }
                    for m in achieved_milestones
                ],
                "upcoming": [
                    {
                        "id": m.id,
                        "name": m.name,
                        "due_date": m.due_date.isoformat() if m.due_date else None,
                    }
                    for m in upcoming_milestones
                ],
            },
            "at_risk_count": len(at_risk_tasks(all_tasks)),
            "members": [
                {"name": m.member.name, "email": m.member.email, "role": m.role}
                for m in project.memberships
            ],
            "documents": [
                {"id": doc.id, "filename": doc.filename, "description": doc.description}
                for doc in project.documents
            ],
            "tasks": tasks,
            "meetings": [
                {"id": m.id, "date": m.meeting_date.isoformat(), "agenda": m.agenda}
                for m in project.meetings
            ],
        }


@mcp.tool()
def get_highlight_types() -> dict:
    """
    Return the configured task highlight labels and the current milestone
    definitions for a project. Agents use this to know which labels are
    valid before applying highlights.
    """
    with _app_context():
        return {"highlight_types": get_task_highlight_types_list()}


@mcp.tool()
def get_milestones(project_id: int, member_email: str) -> dict:
    """
    Return the milestone breakdown for a project: achieved milestones with
    their achievement date and upcoming milestones ordered by due date.
    Requires visibility access to the project.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}
        project = db.session.get(Project, project_id)
        if not project or not _verify_member_project_access(project.id, member.id):
            return {"error": "Project not found or member does not have access."}
        achieved, upcoming, _ = milestones_for(project.tasks)
        return {
            "project_id": project.id,
            "achieved": [
                {
                    "id": m.id,
                    "name": m.name,
                    "milestone_achieved_on": m.milestone_achieved_on.isoformat() if m.milestone_achieved_on else None,
                    "due_date": m.due_date.isoformat() if m.due_date else None,
                }
                for m in achieved
            ],
            "upcoming": [
                {
                    "id": m.id,
                    "name": m.name,
                    "due_date": m.due_date.isoformat() if m.due_date else None,
                }
                for m in upcoming
            ],
        }


@mcp.tool()
def get_project_summary(project_id: int, member_email: str) -> dict:
    """
    Return the Project Summary screen data for an agent: the always-visible
    four-tile pulse, the content behind each accordion, and the team roster
    with responsibilities. Requires any project-team, client, stakeholder,
    or vendor role; returns 404-style error for outsiders.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}
        project = db.session.get(Project, project_id)
        if not project or not _verify_member_project_access(project.id, member.id):
            return {"error": "Project not found or member does not have access."}

        all_tasks = project.tasks
        achieved_milestones, upcoming_milestones, open_milestones = milestones_for(all_tasks)
        at_risk = at_risk_tasks(all_tasks)
        next_meeting = next((m for m in project.meetings if m.meeting_date >= date.today()), None)
        past_meetings = sorted(
            (m for m in project.meetings if m.meeting_date < date.today()), key=lambda m: m.meeting_date, reverse=True,
        )
        configured = get_task_highlight_types_list()
        known = {label.casefold() for label in configured}
        unconfigured = sorted(
            {label for task in all_tasks for label in task.get_highlight_list() if label.casefold() not in known},
            key=str.casefold,
        )
        highlight_labels = [*configured, *unconfigured]
        highlights_by_label = {label: [] for label in highlight_labels}
        for task in all_tasks:
            for label in task.get_highlight_list():
                match = next((item for item in highlight_labels if item.casefold() == label.casefold()), None)
                if match is not None:
                    highlights_by_label[match].append(task)
        for label in highlight_labels:
            highlights_by_label[label].sort(key=lambda task: (task.highlighted_on or date.min, task.id), reverse=True)

        return {
            "project": {
                "id": project.id,
                "project_id": project.project_id,
                "name": project.name,
                "description": project.description,
                "scope": project.scope,
            },
            "pulse": {
                "completion": project_completion(all_tasks),
                "next_milestone": upcoming_milestones[0].name if upcoming_milestones else None,
                "needs_attention": len(at_risk),
                "next_meeting": next_meeting.meeting_date.isoformat() if next_meeting else None,
                "next_meeting_teams_url": create_teams_meeting_url(project, next_meeting) if next_meeting else None,
            },
            "milestones": {
                "achieved": [
                    {
                        "id": m.id,
                        "name": m.name,
                        "milestone_achieved_on": m.milestone_achieved_on.isoformat() if m.milestone_achieved_on else None,
                        "due_date": m.due_date.isoformat() if m.due_date else None,
                        "task_link": f"/projects/{project.id}/tasks/{m.id}",
                    }
                    for m in achieved_milestones
                ],
                "upcoming": [
                    {
                        "id": m.id,
                        "name": m.name,
                        "due_date": m.due_date.isoformat() if m.due_date else None,
                        "task_link": f"/projects/{project.id}/tasks/{m.id}",
                    }
                    for m in upcoming_milestones
                ],
            },
            "highlights": {
                "labels": highlight_labels,
                "total": sum(len(items) for items in highlights_by_label.values()),
                "by_label": {
                    label: [
                        {
                            "id": t.id,
                            "name": t.name,
                            "highlighted_on": t.highlighted_on.isoformat() if t.highlighted_on else None,
                            "task_link": f"/projects/{project.id}/tasks/{t.id}",
                        }
                        for t in tasks
                    ]
                    for label, tasks in highlights_by_label.items()
                },
            },
            "documents": [
                {"id": d.id, "filename": d.filename, "description": d.description}
                for d in project.documents
            ],
            "tech_stack": [t.technology for t in project.technologies],
            "team": [
                {
                    "name": m.member.name,
                    "email": m.member.email,
                    "role": m.role,
                    "responsibility": m.responsibility or "",
                    "team_roles": m.get_team_roles_list(),
                    "mailto": m.member.email,
                }
                for m in project.memberships
            ],
            "meetings": {
                "upcoming": [
                    {"id": m.id, "date": m.meeting_date.isoformat(), "agenda": m.agenda, "teams_url": create_teams_meeting_url(project, m)}
                    for m in project.meetings if m.meeting_date >= date.today()
                ],
                "past": [
                    {"id": m.id, "date": m.meeting_date.isoformat(), "agenda": m.agenda, "teams_url": None}
                    for m in past_meetings
                ],
            },
            "tasks": {
                "active": [
                    {
                        "id": t.id,
                        "name": t.name,
                        "status": t.status,
                        "percentage_complete": project_completion([t]),
                        "current_step": current_step(t).header if current_step(t) else None,
                        "responsible": t.owners[0].member.name if t.owners else None,
                        "is_milestone": t.is_milestone,
                        "highlights": t.get_highlight_list(),
                    }
                    for t in all_tasks if t.status in ("Not Started", "In Progress", "Blocked")
                ],
                "at_risk": [
                    {
                        "id": t.id,
                        "name": t.name,
                        "status": t.status,
                        "due_date": t.due_date.isoformat() if t.due_date else None,
                        "highlights": t.get_highlight_list(),
                    }
                    for t in at_risk
                ],
            },
        }


@mcp.tool()
def generate_secure_document_link(document_id: int, member_email: str, base_url: str = "http://127.0.0.1:5000") -> dict:
    """
    Generate a cryptographically signed, time-limited download URL for a project document.
    Ensures the requesting member has visibility access to the document's parent project.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        document = db.session.get(ProjectDocument, document_id)
        if not document:
            return {"error": f"Document ID {document_id} not found."}

        if not _verify_member_project_access(document.project_id, member.id):
            return {"error": "Access denied: member does not have access to this document's project."}

        token = generate_signed_document_token(document.id, member.id)
        signed_url = f"{base_url.rstrip('/')}/documents/signed/{token}"
        return {
            "document_id": document.id,
            "filename": document.filename,
            "signed_url": signed_url,
            "expires_in_seconds": 3600,
            "description": "Cryptographically signed 1-hour secure download link.",
        }


@mcp.tool()
def generate_teams_meeting_link(meeting_id: int, member_email: str) -> dict:
    """
    Generate a prefilled Microsoft Teams compose link for a scheduled future project meeting.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        meeting = db.session.get(Meeting, meeting_id)
        if not meeting:
            return {"error": f"Meeting ID {meeting_id} not found."}

        if not _verify_member_project_access(meeting.project_id, member.id):
            return {"error": "Access denied: member does not have access to this meeting's project."}

        teams_url = create_teams_meeting_url(meeting.project, meeting)
        if not teams_url:
            return {"error": "Meeting is not scheduled in the future or lacks start time."}

        return {
            "meeting_id": meeting.id,
            "subject": f"{meeting.project.name} meeting",
            "teams_url": teams_url,
        }


@mcp.tool()
def create_project(
    member_email: str,
    project_id: str,
    name: str,
    description: str = "",
    scope: str = "",
    estimated_completion_date: str | None = None,
) -> dict:
    """
    Create a new project. The creating member is automatically assigned to the project team.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        project_id_clean = project_id.strip()
        name_clean = name.strip()
        if not project_id_clean or not name_clean:
            return {"error": "Project ID and project name are required."}

        if Project.query.filter_by(project_id=project_id_clean).first():
            return {"error": f"Project ID '{project_id_clean}' is already in use."}

        est_date = date.fromisoformat(estimated_completion_date) if estimated_completion_date else None

        project = Project(
            project_id=project_id_clean,
            name=name_clean,
            description=description.strip(),
            scope=scope.strip(),
            estimated_completion_date=est_date,
        )
        project.memberships.append(ProjectMembership(member=member, role="team"))
        db.session.add(project)
        db.session.flush()
        _record_audit(project, member, "CREATE", "Project", f"Created project {project.project_id} via MCP", project.id)
        db.session.commit()

        return {
            "id": project.id,
            "project_id": project.project_id,
            "name": project.name,
            "message": "Project created successfully. You have been added to the project team.",
        }


@mcp.tool()
def update_project(
    project_id: int,
    member_email: str,
    name: str | None = None,
    description: str | None = None,
    scope: str | None = None,
    estimated_completion_date: str | None = None,
) -> dict:
    """
    Update details of an existing project. Requires 'team' role on the project.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        project = db.session.get(Project, project_id)
        if not project or not _verify_member_editable_access(project.id, member.id):
            return {"error": "Project not found or member lacks Project Team edit permissions."}

        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                return {"error": "Project name cannot be empty."}
            project.name = clean_name
        if description is not None:
            project.description = description.strip()
        if scope is not None:
            project.scope = scope.strip()
        if estimated_completion_date is not None:
            project.estimated_completion_date = date.fromisoformat(estimated_completion_date) if estimated_completion_date else None

        _record_audit(project, member, "UPDATE", "Project", "Updated project details via MCP", project.id)
        db.session.commit()

        return {"id": project.id, "project_id": project.project_id, "name": project.name, "message": "Project updated successfully."}


@mcp.tool()
def create_task(
    project_id: int,
    member_email: str,
    name: str,
    task_type: str,
    status: str = "Not Started",
    priority: int = 5,
    weight: int = 5,
    due_date: str | None = None,
    jira_ticket_id: str | None = None,
    source_type: str = "Project Plan",
    meeting_id: int | None = None,
    highlights: list[str] | None = None,
    highlighted_on: str | None = None,
    is_milestone: bool = False,
) -> dict:
    """
    Create a new task within a project. Requires Project Team membership.

    ``highlights`` must use the labels an administrator configured in the
    ``task_highlight_types`` setting (for example Key Amendment, Key Finding,
    Key Decision). ``is_milestone`` flags a scheduled checkpoint, which is
    tracked separately from highlights and ordered by ``due_date``.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        project = db.session.get(Project, project_id)
        if not project or not _verify_member_editable_access(project.id, member.id):
            return {"error": "Project not found or member lacks Project Team edit permissions."}

        clean_name = name.strip()
        if not clean_name or task_type not in TASK_TYPES or status not in TASK_STATUSES or source_type not in TASK_SOURCE_TYPES:
            return {"error": "Provide a valid task name, type, status, and source type."}

        if not 1 <= priority <= 10 or not 1 <= weight <= 10:
            return {"error": "Task priority and weight must be between 1 and 10."}

        jira_id_clean = jira_ticket_id.strip() if jira_ticket_id else None
        if jira_id_clean and not is_valid_jira_ticket_id(jira_id_clean):
            return {"error": f"Invalid JIRA ticket ID format '{jira_id_clean}'."}

        source_meeting = None
        if source_type == "Meeting":
            if not meeting_id:
                return {"error": "Meeting ID is required when task source is 'Meeting'."}
            source_meeting = Meeting.query.filter_by(id=meeting_id, project_id=project.id).first()
            if not source_meeting:
                return {"error": "Selected meeting does not belong to this project."}

        due = date.fromisoformat(due_date) if due_date else None
        try:
            highlight_labels = normalise_highlights(highlights or [], get_task_highlight_types_list())
            marked_on = date.fromisoformat(highlighted_on) if highlighted_on else None
        except (ValueError, TypeError):
            return {"error": "Invalid highlight label or highlighted_on date."}

        task = Task(
            project=project,
            name=clean_name,
            task_type=task_type,
            status=status,
            priority=priority,
            weight=weight,
            due_date=due,
            jira_ticket_id=jira_id_clean,
            source_type=source_type,
            source_meeting=source_meeting,
        )
        apply_highlights(task, highlight_labels, marked_on)
        apply_milestone(task, is_milestone, status, None)
        db.session.add(task)
        db.session.flush()
        _record_audit(project, member, "CREATE", "Task", f"Created task {task.name} via MCP", task.id)
        db.session.commit()

        return {
            "id": task.id,
            "name": task.name,
            "task_type": task.task_type,
            "status": task.status,
            "highlights": task.get_highlight_list(),
            "highlighted_on": task.highlighted_on.isoformat() if task.highlighted_on else None,
            "is_milestone": task.is_milestone,
            "message": "Task created.",
        }


@mcp.tool()
def update_task(
    task_id: int,
    member_email: str,
    name: str | None = None,
    task_type: str | None = None,
    status: str | None = None,
    priority: int | None = None,
    weight: int | None = None,
    due_date: str | None = None,
    jira_ticket_id: str | None = None,
    highlights: list[str] | None = None,
    highlighted_on: str | None = None,
    is_milestone: bool | None = None,
) -> dict:
    """
    Update an existing task's properties. Requires Project Team membership on the task's project.

    ``highlights`` replaces the whole set and must use configured labels. Marking a
    task Completed for the first time stamps ``milestone_achieved_on`` automatically
    when the task is a milestone; pass ``is_milestone`` to flag or unflag one.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        task = db.session.get(Task, task_id)
        if not task or not _verify_member_editable_access(task.project_id, member.id):
            return {"error": "Task not found or member lacks Project Team edit permissions."}

        if name is not None:
            task.name = name.strip()
        if task_type is not None:
            if task_type not in TASK_TYPES:
                return {"error": "Invalid task type."}
            task.task_type = task_type
        if status is not None:
            if status not in TASK_STATUSES:
                return {"error": "Invalid task status."}
            task.status = status
        if priority is not None:
            if not 1 <= priority <= 10:
                return {"error": "Priority must be between 1 and 10."}
            task.priority = priority
        if weight is not None:
            if not 1 <= weight <= 10:
                return {"error": "Weight must be between 1 and 10."}
            task.weight = weight
        if due_date is not None:
            task.due_date = date.fromisoformat(due_date) if due_date else None
        if jira_ticket_id is not None:
            clean_jira = jira_ticket_id.strip() if jira_ticket_id else None
            if clean_jira and not is_valid_jira_ticket_id(clean_jira):
                return {"error": "Invalid JIRA ticket ID format."}
            task.jira_ticket_id = clean_jira
        if highlights is not None:
            try:
                highlight_labels = normalise_highlights(highlights, get_task_highlight_types_list())
            except ValueError as error:
                return {"error": str(error)}
            try:
                marked_on = date.fromisoformat(highlighted_on) if highlighted_on else task.highlighted_on
            except (ValueError, TypeError):
                return {"error": "highlighted_on must be an ISO date."}
            apply_highlights(task, highlight_labels, marked_on)
        if is_milestone is not None:
            apply_milestone(task, is_milestone, task.status, None)

        _record_audit(task.project, member, "UPDATE", "Task", f"Updated task {task.name} via MCP", task.id)
        db.session.commit()

        return {
            "id": task.id,
            "name": task.name,
            "status": task.status,
            "highlights": task.get_highlight_list(),
            "highlighted_on": task.highlighted_on.isoformat() if task.highlighted_on else None,
            "is_milestone": task.is_milestone,
            "milestone_achieved_on": task.milestone_achieved_on.isoformat() if task.milestone_achieved_on else None,
            "message": "Task updated.",
        }


@mcp.tool()
def add_task_step(
    task_id: int,
    member_email: str,
    header: str,
    priority: int = 1,
    percentage_complete: int = 0,
    description: str = "",
) -> dict:
    """
    Add a progress step to a task. Priority orders the step; percentage_complete (0-100) shapes project progress.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        task = db.session.get(Task, task_id)
        if not task or not _verify_member_editable_access(task.project_id, member.id):
            return {"error": "Task not found or member lacks Project Team edit permissions."}

        clean_header = header.strip()
        if not clean_header:
            return {"error": "Step header is required."}

        if not 0 <= percentage_complete <= 100:
            return {"error": "Percentage complete must be between 0 and 100."}

        step = TaskStep(
            task=task,
            priority=priority,
            header=clean_header,
            description=description.strip(),
            percentage_complete=percentage_complete,
        )
        db.session.add(step)
        db.session.flush()
        _record_audit(task.project, member, "CREATE", "Task step", f"Added step '{step.header}' to task '{task.name}' via MCP", step.id)
        db.session.commit()

        return {"id": step.id, "header": step.header, "percentage_complete": step.percentage_complete, "message": "Task step added."}


@mcp.tool()
def update_task_step(
    step_id: int,
    member_email: str,
    header: str | None = None,
    priority: int | None = None,
    percentage_complete: int | None = None,
    description: str | None = None,
) -> dict:
    """
    Update a task step's completion percentage, priority, or header text.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        step = db.session.get(TaskStep, step_id)
        if not step or not _verify_member_editable_access(step.task.project_id, member.id):
            return {"error": "Task step not found or member lacks Project Team edit permissions."}

        if header is not None:
            clean_hdr = header.strip()
            if not clean_hdr:
                return {"error": "Step header cannot be empty."}
            step.header = clean_hdr
        if priority is not None:
            step.priority = max(1, priority)
        if percentage_complete is not None:
            if not 0 <= percentage_complete <= 100:
                return {"error": "Percentage complete must be between 0 and 100."}
            step.percentage_complete = percentage_complete
        if description is not None:
            step.description = description.strip()

        _record_audit(step.task.project, member, "UPDATE", "Task step", f"Updated step '{step.header}' via MCP", step.id)
        db.session.commit()

        return {"id": step.id, "header": step.header, "percentage_complete": step.percentage_complete, "message": "Task step updated."}


@mcp.tool()
def create_meeting(
    project_id: int,
    member_email: str,
    meeting_date: str,
    meeting_time: str | None = None,
    timezone_offset_minutes: int | None = None,
    agenda: str = "",
    location: str = "",
    notes: str = "",
    attendee_emails: list[str] | None = None,
) -> dict:
    """
    Record a project meeting with optional local start time and attendees.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        project = db.session.get(Project, project_id)
        if not project or not _verify_member_editable_access(project.id, member.id):
            return {"error": "Project not found or member lacks Project Team edit permissions."}

        m_date = date.fromisoformat(meeting_date)
        m_start = None
        if meeting_time:
            p_time = time.fromisoformat(meeting_time)
            m_start = datetime.combine(m_date, p_time)

        attendees = []
        if attendee_emails:
            for email in attendee_emails:
                att = _get_member_by_email(email)
                if att:
                    attendees.append(att)

        meeting = Meeting(
            project=project,
            meeting_date=m_date,
            meeting_start_at=m_start,
            meeting_utc_offset_minutes=timezone_offset_minutes,
            agenda=agenda.strip(),
            location=location.strip(),
            notes=notes.strip(),
            attendees=attendees,
        )
        db.session.add(meeting)
        db.session.flush()
        _record_audit(project, member, "CREATE", "Meeting", f"Added meeting on {meeting.meeting_date.isoformat()} via MCP", meeting.id)
        db.session.commit()

        return {"id": meeting.id, "meeting_date": meeting.meeting_date.isoformat(), "agenda": meeting.agenda, "message": "Meeting created."}


@mcp.tool()
def add_project_member(
    project_id: int,
    member_email: str,
    new_member_email: str,
    role: str,
    responsibility: str = "",
    team_roles: list[str] | None = None,
) -> dict:
    """
    Assign a project role ('team', 'client', 'stakeholder', 'vendor') to a member account.
    For 'team' role, optional specific team_roles (e.g. ['Project Manager', 'Developer/Implementer']) can be assigned.
    """
    with _app_context():
        member = _get_member_by_email(member_email)
        if not member:
            return {"error": f"Member account '{member_email}' not found."}

        project = db.session.get(Project, project_id)
        if not project or not _verify_member_editable_access(project.id, member.id):
            return {"error": "Project not found or member lacks Project Team edit permissions."}

        if role not in MEMBERSHIP_ROLES:
            return {"error": f"Invalid role '{role}'. Must be one of: {', '.join(MEMBERSHIP_ROLES)}."}

        target_member = _get_member_by_email(new_member_email)
        if not target_member:
            return {"error": f"Target member '{new_member_email}' not found."}

        if ProjectMembership.query.filter_by(project_id=project.id, member_id=target_member.id, role=role).first():
            return {"error": f"Member already has role '{role}' on this project."}

        membership = ProjectMembership(project=project, member=target_member, role=role, responsibility=responsibility.strip())
        if role == "team" and team_roles:
            allowed = set(get_project_team_roles_list())
            valid = [r for r in team_roles if r in allowed]
            membership.set_team_roles_list(valid)

        db.session.add(membership)
        db.session.flush()
        _record_audit(project, member, "CREATE", "Membership", f"Added {target_member.name} as {role} via MCP", membership.id)
        db.session.commit()

        return {
            "project_id": project.id,
            "member": target_member.email,
            "role": role,
            "team_roles": membership.get_team_roles_list(),
            "message": "Project role assigned.",
        }


@mcp.tool()
def get_project_team_roles() -> list[str]:
    """
    Get the list of active configurable Project Team roles.
    """
    with _app_context():
        return get_project_team_roles_list()


if __name__ == "__main__":
    mcp.run()
