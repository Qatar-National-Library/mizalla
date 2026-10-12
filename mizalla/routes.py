from collections.abc import Iterable
from datetime import date, datetime, time
from functools import wraps
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, send_from_directory, url_for
from flask_login import current_user, login_required
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import or_
from werkzeug.utils import secure_filename

from .access import ROLE_GRANTS, get_deletable_project, get_editable_project, get_visible_project, is_pm_membership, is_project_pm, is_project_team_member, visible_projects_query
from .extensions import db
from .jira import is_valid_jira_ticket_id
from .models import (
    ACTIVE_TASK_STATUSES,
    MEMBERSHIP_LABELS,
    MEMBERSHIP_ORDER,
    MEMBERSHIP_ROLES,
    TASK_STATUSES,
    TASK_SOURCE_TYPES,
    TASK_TYPES,
    BusinessDepartment,
    Meeting,
    Member,
    Project,
    ProjectAuditEvent,
    ProjectDocument,
    ProjectMembership,
    ProjectTechnology,
    Server,
    Task,
    TaskOwner,
    TaskStep,
)
from .highlights import apply_highlights, apply_milestone, at_risk_tasks, milestones_for, normalise_highlights
from .progress import current_step, project_completion
from .secure_links import generate_signed_document_token, verify_signed_document_token
from .role_meta import DELIVERY_ROLES, EXTERNAL_ROLES, MEMBERSHIP_NOTES, PM_TEAM_ROLE
from .settings import (
    can_edit_tasks,
    get_project_team_roles_list,
    get_task_highlight_types_list,
)
from .teams import create_teams_meeting_url
from .validators import bounded_int, optional_date, parse_email_list

bp = Blueprint("main", __name__, url_prefix="/apps/mizalla")
ALLOWED_DOCUMENT_EXTENSIONS = {"csv", "doc", "docx", "jpg", "jpeg", "pdf", "png", "ppt", "pptx", "txt", "xls", "xlsx"}


def _role_choices(roles: Iterable[str] = MEMBERSHIP_ORDER) -> list[tuple[str, str]]:
    """The given roles as ``(key, label)`` pairs, in the order given.

    Defaults to every role, which is what the role picker and the full role map
    want. The project screen also asks for the delivery and external subsets
    separately, so it can answer "who is building this?" and "who needs to see
    it?" without the template slicing the list.
    """
    return [(role, MEMBERSHIP_LABELS[role]) for role in roles if role in MEMBERSHIP_LABELS]



def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def _meeting_time_value(meeting_date, meeting_time, timezone_offset):
    if not meeting_time:
        return None, None
    if timezone_offset is None or not -840 <= timezone_offset <= 840:
        raise ValueError("The meeting's local timezone could not be determined. Refresh and try again.")
    try:
        parsed_time = time.fromisoformat(meeting_time)
    except ValueError:
        raise ValueError("Meeting time must be valid.") from None
    return datetime.combine(meeting_date, parsed_time), timezone_offset


def _member_options():
    """Members the current user may see in a picker.

    Scoped to memberships on the caller's visible projects so the full member
    directory is never disclosed to a user who can only see one project.
    """
    visible_project_ids = visible_projects_query().with_entities(Project.id)
    return (
        Member.query.join(ProjectMembership)
        .filter(ProjectMembership.project_id.in_(visible_project_ids))
        .distinct()
        .order_by(Member.name)
        .all()
    )


def _selected_member_ids(form, members):
    """Member ids ticked in an attendee/member checkbox group.

    An absent key means nothing was selected, so a freshly rendered checkbox
    group of pre-checked members still posts every id on submit.
    """
    return {int(value) for value in form.getlist("member_ids", type=int)} & {m.id for m in members}


def _external_attendee_emails(form):
    """Parse and validate the ad-hoc attendee address field."""
    valid, invalid = parse_email_list(form.get("external_attendee_emails", ""))
    if invalid:
        raise ValueError(f"These addresses are not valid email addresses: {', '.join(invalid)}.")
    return valid





def _project_detail(project):
    return redirect(url_for("main.project_detail", project_id=project.id))


def _new_task_marks(task) -> list[str]:
    """Audit fragments describing the highlights and milestone flag on a new task."""
    parts = []
    labels = task.get_highlight_list()
    if labels:
        parts.append(f"highlights {', '.join(labels)}")
    if task.is_milestone:
        parts.append("milestone")
    return parts


def _task_change_summary(prefix: str, task, changes=None) -> str:
    parts = [f"{prefix} task {task.name}", *(changes or [])]
    return "; ".join(parts)


def _record_project_change(project, action, entity_type, summary, entity_id=None):
    db.session.add(
        ProjectAuditEvent(
            project=project,
            project_key=project.project_id,
            project_name=project.name,
            actor_id=current_user.id,
            actor_name=current_user.name,
            actor_email=current_user.email,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary[:500],
        )
    )


def _excel_text(value):
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return f"'{value}"
    return value


def _format_export_sheet(worksheet):
    header_fill = PatternFill("solid", fgColor="103E30")
    stripe_fill = PatternFill("solid", fgColor="EFF3EE")
    for cell in worksheet[1]:
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    worksheet.row_dimensions[1].height = 30
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Arial", size=10, color="182A24")
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        if row[0].row % 2 == 0:
            for cell in row:
                cell.fill = stripe_fill
    for column in worksheet.columns:
        column_letter = column[0].column_letter
        content_width = max((len(str(cell.value or "")) for cell in column), default=0)
        worksheet.column_dimensions[column_letter].width = min(max(content_width + 2, 14), 48)
    worksheet.freeze_panes = "A2"
    if worksheet.max_row > 1:
        worksheet.auto_filter.ref = worksheet.dimensions


def _portfolio_context(args):
    search = args.get("q", "").strip()[:120]
    task_status = args.get("status", "")
    if task_status not in TASK_STATUSES:
        task_status = ""
    owner_id = args.get("owner_id", type=int)
    due_from = args.get("due_from", "")
    due_to = args.get("due_to", "")
    try:
        due_from = optional_date(due_from, "Due from")
        due_to = optional_date(due_to, "Due to")
        if due_from and due_to and due_from > due_to:
            raise ValueError("Due from must be on or before Due to.")
    except ValueError as error:
        flash(str(error), "error")
        due_from = due_to = None

    projects_query = visible_projects_query()
    if search:
        project_text_match = or_(Project.name.ilike(f"%{search}%"), Project.project_id.ilike(f"%{search}%"))
        task_text_match = Project.tasks.any(
            or_(Task.name.ilike(f"%{search}%"), Task.jira_ticket_id.ilike(f"%{search}%"))
        )
        projects_query = projects_query.filter(or_(project_text_match, task_text_match))
    if task_status:
        projects_query = projects_query.filter(Project.tasks.any(Task.status == task_status))
    if owner_id:
        projects_query = projects_query.filter(Project.tasks.any(Task.owners.any(TaskOwner.member_id == owner_id)))
    if due_from:
        projects_query = projects_query.filter(Project.tasks.any(Task.due_date >= due_from))
    if due_to:
        projects_query = projects_query.filter(Project.tasks.any(Task.due_date <= due_to))

    projects = projects_query.order_by(Project.name).all()
    project_member_ids = visible_projects_query().with_entities(Project.id)
    portfolio_members = (
        Member.query.join(ProjectMembership)
        .filter(ProjectMembership.project_id.in_(project_member_ids))
        .distinct()
        .order_by(Member.name)
        .all()
    )
    summaries = []
    for project in projects:
        project_matches_search = bool(
            search and (search.casefold() in project.name.casefold() or search.casefold() in project.project_id.casefold())
        )
        matching_tasks = []
        for task in project.tasks:
            if task_status and task.status != task_status:
                continue
            if not task_status and task.status not in ACTIVE_TASK_STATUSES:
                continue
            if owner_id and not any(owner.member_id == owner_id for owner in task.owners):
                continue
            if due_from and (task.due_date is None or task.due_date < due_from):
                continue
            if due_to and (task.due_date is None or task.due_date > due_to):
                continue
            if search and not project_matches_search:
                needle = search.casefold()
                if needle not in task.name.casefold() and needle not in (task.jira_ticket_id or "").casefold():
                    continue
            matching_tasks.append(task)
        summaries.append(
            {
                "project": project,
                "completion": project_completion(project.tasks),
                "tasks": [(task, current_step(task)) for task in matching_tasks],
            }
        )

    return {
        "summaries": summaries,
        "portfolio_members": portfolio_members,
        "filters": {
            "q": search,
            "status": task_status,
            "owner_id": owner_id,
            "due_from": due_from.isoformat() if due_from else "",
            "due_to": due_to.isoformat() if due_to else "",
        },
        "has_filters": bool(search or task_status or owner_id or due_from or due_to),
        "task_statuses": TASK_STATUSES,
    }


@bp.get("/about")
def about():
    return render_template("about.html")


@bp.get("/user-guide")
def user_guide():
    return render_template("user_guide.html")


@bp.get("/")
@login_required
def project_list():
    return render_template("projects.html", **_portfolio_context(request.args))


@bp.get("/projects/export.xlsx")
@login_required
def project_export():
    summaries = _portfolio_context(request.args)["summaries"]
    workbook = Workbook()
    projects_sheet = workbook.active
    projects_sheet.title = "Projects"
    projects_sheet.append(
        [
            "Project ID",
            "Project Name",
            "Description",
            "Scope",
            "Estimated Completion",
            "Actual Start",
            "Actual Completion",
            "Completion %",
            "Active / Matching Tasks",
        ]
    )

    tasks_sheet = workbook.create_sheet("Tasks")
    tasks_sheet.append(
        [
            "Project ID",
            "Project Name",
            "Task Name",
            "Task Type",
            "Status",
            "Due Date",
            "Priority",
            "Weight",
            "Owners",
            "Current Step",
            "Source",
            "JIRA Ticket ID",
        ]
    )

    for item in summaries:
        project = item["project"]
        task_labels = []
        for task, step in item["tasks"]:
            owner_names = ", ".join(owner.member.name for owner in task.owners)
            task_label = task.name
            if step:
                task_label += f" — {step.header}"
            task_label += f" ({owner_names or 'Unassigned'})"
            task_labels.append(task_label)
            tasks_sheet.append(
                [
                    project.project_id,
                    project.name,
                    task.name,
                    task.task_type,
                    task.status,
                    task.due_date,
                    task.priority,
                    task.weight,
                    owner_names,
                    step.header if step else None,
                    task.source_type,
                    task.jira_ticket_id,
                ]
            )

        projects_sheet.append(
            [
                project.project_id,
                _excel_text(project.name),
                _excel_text(project.description),
                _excel_text(project.scope),
                project.estimated_completion_date,
                project.actual_start_date,
                project.actual_completion_date,
                item["completion"],
                _excel_text("; ".join(task_labels)),
            ]
        )

    for worksheet in (projects_sheet, tasks_sheet):
        _format_export_sheet(worksheet)
    for cell in projects_sheet["H"][1:]:
        cell.number_format = '0"%"'

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"mizalla-export-{date.today().isoformat()}.xlsx",
    )


@bp.route("/projects/new", methods=["GET", "POST"])
@login_required
def project_new():
    # Creating a project is a directory-level capability, not a per-project one -
    # there is no project yet to hold a role on - so it stays with admins and
    # globally flagged PMs. Authority over an *existing* project comes from the
    # roles on that project; see access.py.
    if not current_user.is_admin and not current_user.is_pm:
        abort(403)
    if request.method == "POST":
        project_id = request.form.get("project_id", "").strip()
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        scope = request.form.get("scope", "").strip()
        if not project_id or not name:
            flash("Project ID and project name are required.", "error")
        elif Project.query.filter_by(project_id=project_id).first():
            flash("That project ID is already in use.", "error")
        else:
            try:
                estimated_date = optional_date(request.form.get("estimated_completion_date"), "Estimated completion date")
                start_date = optional_date(request.form.get("actual_start_date"), "Actual start date")
                completion_date = optional_date(request.form.get("actual_completion_date"), "Actual completion date")
                if start_date and completion_date and completion_date < start_date:
                    raise ValueError("Actual completion date cannot be before actual start date.")
                project = Project(
                    project_id=project_id,
                    name=name,
                    description=description,
                    scope=scope,
                    estimated_completion_date=estimated_date,
                    actual_start_date=start_date,
                    actual_completion_date=completion_date,
                )
                # The creator runs the project, so they get PM authority on it.
                # Without it they would own a project they cannot staff, since
                # assigning roles is reserved for that project's PM.
                owner = ProjectMembership(member=current_user, role="team")
                owner.set_team_roles_list([PM_TEAM_ROLE])
                project.memberships.append(owner)
                db.session.add(project)
                db.session.flush()
                _record_project_change(project, "CREATE", "Project", "Created project record", project.id)
                db.session.commit()
                flash(
                    "Project created. You have been added to its project team as Project Manager.",
                    "success",
                )
                return _project_detail(project)
            except ValueError as error:
                flash(str(error), "error")
    return render_template("project_form.html", project=None)


@bp.route("/projects/<int:project_id>", methods=["GET"])
@login_required
def project_detail(project_id):
    project = get_visible_project(project_id)
    active_tasks = [task for task in project.tasks if task.status in ACTIVE_TASK_STATUSES]
    achieved_milestones, upcoming_milestones, _open_milestones = milestones_for(project.tasks)
    return render_template(
        "project_detail.html",
        project=project,
        active_tasks=active_tasks,
        all_tasks=project.tasks,
        achieved_milestones=achieved_milestones,
        upcoming_milestones=upcoming_milestones,
        at_risk_tasks=at_risk_tasks(project.tasks),
        task_types=TASK_TYPES,
        task_statuses=TASK_STATUSES,
        # Every role, for the picker and the full role map; and the two
        # groupings the tabbed screen splits the same rows into.
        roles=_role_choices(),
        delivery_roles=_role_choices(DELIVERY_ROLES),
        external_roles=_role_choices(EXTERNAL_ROLES),
        task_source_types=TASK_SOURCE_TYPES,
        meetings=project.meetings,
        can_edit=is_project_team_member(project),
        can_delete=current_user.is_admin and is_project_team_member(project),
        can_edit_tasks=can_edit_tasks(project, current_user),
        can_manage_members=is_project_pm(project),
        members=_member_options(),
        servers=Server.query.order_by(Server.name).all(),
        project_team_roles=get_project_team_roles_list(),
        role_notes=MEMBERSHIP_NOTES,
        # The Roles tab is a reference: what each role is and what it grants, so a
        # PM can answer "who can edit this?" without leaving the project.
        role_grants=ROLE_GRANTS,
        # One membership row per role, so the raw count would double-count anyone
        # holding more than one role on this project.
        people_count=len({row.member_id for row in project.memberships}),
        highlight_types=get_task_highlight_types_list(),
        completion=project_completion(project.tasks),
        audit_events=ProjectAuditEvent.query.filter_by(project_id=project.id).order_by(ProjectAuditEvent.created_at.desc()).limit(30).all(),
    )


@bp.get("/projects/<int:project_id>/summary")
@login_required
def project_summary(project_id):
    project = get_visible_project(project_id)
    tasks = project.tasks
    achieved_milestones, upcoming_milestones, open_milestones = milestones_for(tasks)

    # Configured labels come first, then any label still recorded on a task that an
    # administrator has since removed from the setting, so nothing silently vanishes
    # from the summary.
    configured = get_task_highlight_types_list()
    known = {label.casefold() for label in configured}
    unconfigured = sorted(
        {label for task in tasks for label in task.get_highlight_list() if label.casefold() not in known},
        key=str.casefold,
    )
    highlight_labels = [*configured, *unconfigured]
    highlights_by_label = {label: [] for label in highlight_labels}
    for task in tasks:
        for label in task.get_highlight_list():
            match = next((item for item in highlight_labels if item.casefold() == label.casefold()), None)
            if match is not None:
                highlights_by_label[match].append(task)
    for label in highlight_labels:
        highlights_by_label[label].sort(key=lambda task: (task.highlighted_on or date.min, task.id), reverse=True)

    today = date.today()
    next_meeting = next((meeting for meeting in project.meetings if meeting.meeting_date >= today), None)
    last_audit = ProjectAuditEvent.query.filter_by(project_id=project.id).order_by(ProjectAuditEvent.created_at.desc()).first()
    meeting_teams_urls = {
        m.id: create_teams_meeting_url(project, m)
        for m in project.meetings
    }
    return render_template(
        "project_summary.html",
        project=project,
        completion=project_completion(tasks),
        active_tasks=[task for task in tasks if task.status in ACTIVE_TASK_STATUSES],
        at_risk_tasks=at_risk_tasks(tasks),
        achieved_milestones=achieved_milestones,
        upcoming_milestones=upcoming_milestones,
        next_milestone=upcoming_milestones[0] if upcoming_milestones else None,
        open_milestone_count=len(open_milestones),
        highlight_labels=highlight_labels,
        highlights_by_label=highlights_by_label,
        highlight_total=sum(len(items) for items in highlights_by_label.values()),
        next_meeting=next_meeting,
        next_meeting_date=next_meeting.meeting_date if next_meeting else today,
        past_meetings=sorted((meeting for meeting in project.meetings if meeting.meeting_date < today), key=lambda m: m.meeting_date, reverse=True),
        last_updated=last_audit.created_at if last_audit else project.created_at,
        roles=_role_choices(),
        current_step=current_step,
        can_edit=is_project_team_member(project),
        highlight_types=configured,
        pm_memberships=[m for m in project.memberships if is_pm_membership(m)],
        meeting_teams_urls=meeting_teams_urls,
    )


@bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
@login_required
def project_edit(project_id):
    project = get_editable_project(project_id)
    if request.method == "POST":
        project_id_value = request.form.get("project_id", "").strip()
        name = request.form.get("name", "").strip()
        duplicate = Project.query.filter(Project.project_id == project_id_value, Project.id != project.id).first()
        if not project_id_value or not name:
            flash("Project ID and project name are required.", "error")
        elif duplicate:
            flash("That project ID is already in use.", "error")
        else:
            try:
                estimated_date = optional_date(request.form.get("estimated_completion_date"), "Estimated completion date")
                start_date = optional_date(request.form.get("actual_start_date"), "Actual start date")
                completion_date = optional_date(request.form.get("actual_completion_date"), "Actual completion date")
                if start_date and completion_date and completion_date < start_date:
                    raise ValueError("Actual completion date cannot be before actual start date.")
                project.project_id = project_id_value
                project.name = name
                project.description = request.form.get("description", "").strip()
                project.scope = request.form.get("scope", "").strip()
                project.estimated_completion_date = estimated_date
                project.actual_start_date = start_date
                project.actual_completion_date = completion_date
                _record_project_change(project, "UPDATE", "Project", "Updated project details", project.id)
                db.session.commit()
                flash("Project details updated.", "success")
                return _project_detail(project)
            except ValueError as error:
                flash(str(error), "error")
    return render_template("project_form.html", project=project)


@bp.post("/projects/<int:project_id>/delete")
@login_required
def project_delete(project_id):
    project = get_deletable_project(project_id)
    _record_project_change(project, "DELETE", "Project", f"Deleted project {project.project_id}", project.id)
    for document in project.documents:
        (Path(current_app.config["UPLOAD_FOLDER"]) / document.stored_name).unlink(missing_ok=True)
    db.session.delete(project)
    db.session.commit()
    flash("Project deleted.", "success")
    return redirect(url_for("main.project_list"))


@bp.post("/projects/<int:project_id>/memberships")
@login_required
def membership_add(project_id):
    project = get_visible_project(project_id)
    # Only a PM of this project may assign its roles. A Delegated PM counts.
    if not is_project_pm(project):
        abort(403)
    member = db.session.get(Member, request.form.get("member_id", type=int))
    # A member may hold several roles on one project, so this is a multi-select
    # and each choice becomes its own membership row.
    roles = request.form.getlist("role")
    responsibility = request.form.get("responsibility", "").strip()
    team_roles_input = request.form.getlist("team_roles")
    allowed_team_roles = set(get_project_team_roles_list())
    valid_team_roles = [r for r in team_roles_input if r in allowed_team_roles]

    unknown = [r for r in roles if r not in MEMBERSHIP_ROLES]
    if member is None or not roles:
        flash("Choose a member and at least one project role.", "error")
    elif unknown:
        flash("One of the chosen roles is not valid.", "error")
    else:
        # De-duplicate the submission, keeping the order the roles are listed in.
        chosen = [role for role in MEMBERSHIP_ORDER if role in set(roles)]
        held = {
            row.role
            for row in ProjectMembership.query.filter(
                ProjectMembership.project_id == project.id,
                ProjectMembership.member_id == member.id,
                ProjectMembership.role.in_(chosen),
            ).all()
        }
        new_roles = [role for role in chosen if role not in held]
        if not new_roles:
            flash(f"{member.name} already holds every role you selected.", "error")
        else:
            for role in new_roles:
                membership = ProjectMembership(
                    project=project, member=member, role=role, responsibility=responsibility
                )
                if role == "team" and valid_team_roles:
                    membership.set_team_roles_list(valid_team_roles)
                db.session.add(membership)
            db.session.flush()
            labels = ", ".join(MEMBERSHIP_LABELS[role] for role in new_roles)
            _record_project_change(
                project, "CREATE", "Membership", f"Added {member.name} as {labels}", member.id
            )
            db.session.commit()
            skipped = [role for role in chosen if role in held]
            suffix = (
                f" Already held: {', '.join(MEMBERSHIP_LABELS[r] for r in skipped)}."
                if skipped
                else ""
            )
            flash(f"{member.name} added as {labels}.{suffix}", "success")
    return _project_detail(project)


@bp.post("/projects/<int:project_id>/memberships/<int:membership_id>/delete")
@login_required
def membership_delete(project_id, membership_id):
    project = get_visible_project(project_id)
    if not is_project_pm(project):
        abort(403)
    membership = ProjectMembership.query.filter_by(id=membership_id, project_id=project.id).first_or_404()
    name, label = membership.member.name, membership.role_label
    _record_project_change(
        project, "DELETE", "Membership", f"Removed {name}'s {label} role", membership.id
    )
    db.session.delete(membership)
    db.session.commit()
    # Someone can hold several roles, so taking one away may leave them on the
    # project rather than off it entirely.
    remaining = ProjectMembership.query.filter_by(
        project_id=project.id, member_id=membership.member_id
    ).count()
    flash(
        f"{name} is no longer {label.lower()} on this project."
        if remaining
        else f"{name} removed from this project.",
        "success",
    )
    return _project_detail(project) if visible_projects_query().filter_by(id=project.id).first() else redirect(url_for("main.project_list"))


@bp.post("/projects/<int:project_id>/departments")
@login_required
def department_add(project_id):
    project = get_editable_project(project_id)
    name = request.form.get("department_name", "").strip()
    responsibility = request.form.get("responsibility", "").strip()
    if not name:
        flash("Department name is required.", "error")
    else:
        department = BusinessDepartment(project=project, department_name=name, responsibility=responsibility)
        db.session.add(department)
        db.session.flush()
        _record_project_change(project, "CREATE", "Department", f"Added owner department {name}", department.id)
        db.session.commit()
        flash("Business owner department added.", "success")
    return _project_detail(project)


@bp.post("/projects/<int:project_id>/departments/<int:department_id>/delete")
@login_required
def department_delete(project_id, department_id):
    project = get_editable_project(project_id)
    department = BusinessDepartment.query.filter_by(id=department_id, project_id=project.id).first_or_404()
    _record_project_change(project, "DELETE", "Department", f"Removed owner department {department.department_name}", department.id)
    db.session.delete(department)
    db.session.commit()
    flash("Department removed.", "success")
    return _project_detail(project)


@bp.post("/projects/<int:project_id>/technologies")
@login_required
def technology_add(project_id):
    project = get_editable_project(project_id)
    technology = request.form.get("technology", "").strip()
    if not technology:
        flash("Technology is required.", "error")
    else:
        item = ProjectTechnology(project=project, technology=technology)
        db.session.add(item)
        db.session.flush()
        _record_project_change(project, "CREATE", "Technology", f"Added technology {technology}", item.id)
        db.session.commit()
        flash("Technology added.", "success")
    return _project_detail(project)


@bp.post("/projects/<int:project_id>/technologies/<int:technology_id>/delete")
@login_required
def technology_delete(project_id, technology_id):
    project = get_editable_project(project_id)
    technology = ProjectTechnology.query.filter_by(id=technology_id, project_id=project.id).first_or_404()
    _record_project_change(project, "DELETE", "Technology", f"Removed technology {technology.technology}", technology.id)
    db.session.delete(technology)
    db.session.commit()
    flash("Technology removed.", "success")
    return _project_detail(project)


@bp.post("/projects/<int:project_id>/servers")
@login_required
def project_servers(project_id):
    project = get_editable_project(project_id)
    test_id = request.form.get("test_server_id", type=int)
    production_id = request.form.get("production_server_id", type=int)
    test_server = db.session.get(Server, test_id) if test_id else None
    production_server = db.session.get(Server, production_id) if production_id else None
    if (test_id and test_server is None) or (production_id and production_server is None):
        flash("Choose a valid test and production server.", "error")
        return _project_detail(project)
    project.test_server = test_server
    project.production_server = production_server
    _record_project_change(project, "UPDATE", "Environment", "Updated test and production server assignments", project.id)
    db.session.commit()
    flash("Project server assignments updated.", "success")
    return _project_detail(project)


@bp.post("/projects/<int:project_id>/meetings")
@login_required
def meeting_add(project_id):
    project = get_editable_project(project_id)
    try:
        meeting_date = optional_date(request.form.get("meeting_date"), "Meeting date")
        if meeting_date is None:
            raise ValueError("Meeting date is required.")
        meeting_start_at, meeting_utc_offset = _meeting_time_value(
            meeting_date,
            request.form.get("meeting_time", ""),
            request.form.get("meeting_utc_offset_minutes", type=int),
        )
        external_emails = _external_attendee_emails(request.form)
        attendee_ids = set(request.form.getlist("attendee_ids", type=int))
        attendees = Member.query.filter(Member.id.in_(attendee_ids)).all() if attendee_ids else []
        if len(attendees) != len(attendee_ids):
            raise ValueError("Choose valid meeting attendees.")
        meeting = Meeting(
            project=project,
            meeting_date=meeting_date,
            meeting_start_at=meeting_start_at,
            meeting_utc_offset_minutes=meeting_utc_offset,
            agenda=request.form.get("agenda", "").strip(),
            location=request.form.get("location", "").strip(),
            notes=request.form.get("notes", "").strip(),
            attendees=attendees,
        )
        meeting.set_external_attendee_list(external_emails)
        db.session.add(meeting)
        db.session.flush()
        _record_project_change(project, "CREATE", "Meeting", f"Added meeting on {meeting.meeting_date.isoformat()}", meeting.id)
        db.session.commit()
        flash("Meeting added.", "success")
        teams_url = create_teams_meeting_url(project, meeting)
        if teams_url:
            return redirect(teams_url)
    except ValueError as error:
        db.session.rollback()
        flash(str(error), "error")
    return _project_detail(project)


@bp.route("/projects/<int:project_id>/meetings/<int:meeting_id>/edit", methods=["GET", "POST"])
@login_required
def meeting_edit(project_id, meeting_id):
    project = get_editable_project(project_id)
    meeting = Meeting.query.filter_by(id=meeting_id, project_id=project.id).first_or_404()
    if request.method == "POST":
        try:
            meeting_date = optional_date(request.form.get("meeting_date"), "Meeting date")
            if meeting_date is None:
                raise ValueError("Meeting date is required.")
            meeting_start_at, meeting_utc_offset = _meeting_time_value(
                meeting_date,
                request.form.get("meeting_time", ""),
                request.form.get("meeting_utc_offset_minutes", type=int),
            )
            external_emails = _external_attendee_emails(request.form)
            attendee_ids = set(request.form.getlist("attendee_ids", type=int))
            attendees = Member.query.filter(Member.id.in_(attendee_ids)).all() if attendee_ids else []
            if len(attendees) != len(attendee_ids):
                raise ValueError("Choose valid meeting attendees.")
            meeting.meeting_date = meeting_date
            meeting.meeting_start_at = meeting_start_at
            meeting.meeting_utc_offset_minutes = meeting_utc_offset
            meeting.agenda = request.form.get("agenda", "").strip()
            meeting.location = request.form.get("location", "").strip()
            meeting.notes = request.form.get("notes", "").strip()
            meeting.attendees = attendees
            meeting.set_external_attendee_list(external_emails)
            _record_project_change(project, "UPDATE", "Meeting", f"Updated meeting on {meeting.meeting_date.isoformat()}", meeting.id)
            db.session.commit()
            flash("Meeting updated.", "success")
            teams_url = create_teams_meeting_url(project, meeting)
            if teams_url:
                return redirect(teams_url)
            return _project_detail(project)
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "error")
    return render_template("meeting_form.html", project=project, meeting=meeting, members=_member_options())


@bp.get("/projects/<int:project_id>/meetings/<int:meeting_id>/open-teams")
@login_required
def meeting_open_teams(project_id, meeting_id):
    project = get_editable_project(project_id)
    meeting = Meeting.query.filter_by(id=meeting_id, project_id=project.id).first_or_404()
    teams_url = create_teams_meeting_url(project, meeting)
    if teams_url is None:
        flash("Only scheduled future meetings can be opened in Teams.", "error")
        return _project_detail(project)
    return redirect(teams_url)


@bp.post("/projects/<int:project_id>/meetings/<int:meeting_id>/delete")
@login_required
def meeting_delete(project_id, meeting_id):
    project = get_editable_project(project_id)
    meeting = Meeting.query.filter_by(id=meeting_id, project_id=project.id).first_or_404()
    if Task.query.filter_by(meeting_id=meeting.id).first():
        flash("Reassign tasks linked to this meeting before deleting it.", "error")
    else:
        _record_project_change(project, "DELETE", "Meeting", f"Deleted meeting on {meeting.meeting_date.isoformat()}", meeting.id)
        db.session.delete(meeting)
        db.session.commit()
        flash("Meeting deleted.", "success")
    return _project_detail(project)


@bp.post("/projects/<int:project_id>/tasks")
@login_required
def task_add(project_id):
    project = get_editable_project(project_id)
    name = request.form.get("name", "").strip()
    task_type = request.form.get("task_type", "")
    status = request.form.get("status", "Not Started")
    source_type = request.form.get("source_type", "Project Plan")
    jira_ticket_id = request.form.get("jira_ticket_id", "").strip() or None
    try:
        priority = bounded_int(request.form.get("priority"), "Task priority", 1, 10)
        weight = bounded_int(request.form.get("weight"), "Task weight", 1, 10)
        due_value = request.form.get("due_date", "")
        due_date = optional_date(due_value, "Due date")
        if not name or task_type not in TASK_TYPES or status not in TASK_STATUSES or source_type not in TASK_SOURCE_TYPES:
            raise ValueError("Enter a task name and choose valid task type and status values.")
        meeting_id = request.form.get("meeting_id", type=int)
        source_meeting = None
        if source_type == "Meeting":
            source_meeting = Meeting.query.filter_by(id=meeting_id, project_id=project.id).first()
            if source_meeting is None:
                raise ValueError("Choose a meeting from this project as the task source.")
        if jira_ticket_id and (len(jira_ticket_id) > 80 or not is_valid_jira_ticket_id(jira_ticket_id)):
            raise ValueError("Enter a valid JIRA ticket ID, such as APP-123.")
        highlight_types = get_task_highlight_types_list()
        highlights = normalise_highlights(request.form.getlist("highlights"), highlight_types)
        marked_on = optional_date(request.form.get("highlighted_on", ""), "Highlight date")
        is_milestone = bool(request.form.get("is_milestone"))
        task = Task(
            project=project,
            name=name,
            task_type=task_type,
            status=status,
            due_date=due_date,
            priority=priority,
            weight=weight,
            source_type=source_type,
            source_meeting=source_meeting,
            jira_ticket_id=jira_ticket_id,
        )
        apply_highlights(task, highlights, marked_on)
        apply_milestone(task, is_milestone, status, None)
        db.session.add(task)
        db.session.flush()
        owner_id = request.form.get("owner_id", type=int)
        if owner_id:
            owner = db.session.get(Member, owner_id)
            if owner is None:
                raise ValueError("Choose a valid task owner.")
            db.session.add(TaskOwner(task=task, member=owner, responsibility=request.form.get("owner_responsibility", "").strip()))
        _record_project_change(project, "CREATE", "Task", _task_change_summary("Created", task, _new_task_marks(task)), task.id)
        db.session.commit()
        flash("Task created.", "success")
    except ValueError as error:
        db.session.rollback()
        flash(str(error), "error")
    return _project_detail(project)


@bp.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
@login_required
def task_edit(task_id):
    task = db.get_or_404(Task, task_id)
    project = get_editable_project(task.project_id)
    if request.method == "POST":
        try:
            priority = bounded_int(request.form.get("priority"), "Task priority", 1, 10)
            weight = bounded_int(request.form.get("weight"), "Task weight", 1, 10)
            task_type = request.form.get("task_type", "")
            status = request.form.get("status", "")
            source_type = request.form.get("source_type", "Project Plan")
            name = request.form.get("name", "").strip()
            due_value = request.form.get("due_date", "")
            if not name or task_type not in TASK_TYPES or status not in TASK_STATUSES or source_type not in TASK_SOURCE_TYPES:
                raise ValueError("Enter a task name and choose valid task type and status values.")
            meeting_id = request.form.get("meeting_id", type=int)
            source_meeting = None
            if source_type == "Meeting":
                source_meeting = Meeting.query.filter_by(id=meeting_id, project_id=project.id).first()
                if source_meeting is None:
                    raise ValueError("Choose a meeting from this project as the task source.")
            jira_ticket_id = request.form.get("jira_ticket_id", "").strip() or None
            if jira_ticket_id and (len(jira_ticket_id) > 80 or not is_valid_jira_ticket_id(jira_ticket_id)):
                raise ValueError("Enter a valid JIRA ticket ID, such as APP-123.")
            previous_status = task.status
            previous_highlights = task.get_highlight_list()
            previous_milestone = task.is_milestone
            task.name = name
            task.task_type = task_type
            task.status = status
            task.priority = priority
            task.weight = weight
            task.due_date = optional_date(due_value, "Due date")
            task.source_type = source_type
            task.source_meeting = source_meeting
            task.jira_ticket_id = jira_ticket_id
            apply_highlights(
                task,
                normalise_highlights(request.form.getlist("highlights"), get_task_highlight_types_list()),
                optional_date(request.form.get("highlighted_on", ""), "Highlight date"),
            )
            apply_milestone(
                task,
                bool(request.form.get("is_milestone")),
                status,
                optional_date(request.form.get("milestone_achieved_on", ""), "Milestone achieved date"),
            )
            changes = []
            if previous_status != status:
                changes.append(f"status {previous_status} to {status}")
            if previous_highlights != task.get_highlight_list():
                changes.append("highlights " + (", ".join(task.get_highlight_list()) or "cleared"))
            if previous_milestone != task.is_milestone:
                changes.append("milestone on" if task.is_milestone else "milestone off")
            _record_project_change(project, "UPDATE", "Task", _task_change_summary("Updated", task, changes), task.id)
            db.session.commit()
            flash("Task updated.", "success")
            return _project_detail(project)
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "error")
    return render_template(
        "task_form.html",
        project=project,
        task=task,
        task_types=TASK_TYPES,
        task_statuses=TASK_STATUSES,
        task_source_types=TASK_SOURCE_TYPES,
        meetings=project.meetings,
        highlight_types=get_task_highlight_types_list(),
    )


@bp.post("/tasks/<int:task_id>/delete")
@login_required
def task_delete(task_id):
    task = db.get_or_404(Task, task_id)
    project = get_editable_project(task.project_id)
    _record_project_change(project, "DELETE", "Task", f"Deleted task {task.name}", task.id)
    db.session.delete(task)
    db.session.commit()
    flash("Task deleted.", "success")
    return _project_detail(project)


@bp.post("/tasks/<int:task_id>/owners")
@login_required
def task_owner_add(task_id):
    task = db.get_or_404(Task, task_id)
    project = get_editable_project(task.project_id)
    member = db.session.get(Member, request.form.get("member_id", type=int))
    if member is None:
        flash("Choose a valid task owner.", "error")
    elif TaskOwner.query.filter_by(task_id=task.id, member_id=member.id).first():
        flash("That member already owns this task.", "error")
    else:
        assignment = TaskOwner(task=task, member=member, responsibility=request.form.get("responsibility", "").strip())
        db.session.add(assignment)
        db.session.flush()
        _record_project_change(project, "CREATE", "Task owner", f"Assigned {member.name} to {task.name}", assignment.id)
        db.session.commit()
        flash("Task owner added.", "success")
    return _project_detail(project)


@bp.post("/tasks/<int:task_id>/owners/<int:owner_id>/delete")
@login_required
def task_owner_delete(task_id, owner_id):
    task = db.get_or_404(Task, task_id)
    project = get_editable_project(task.project_id)
    owner = TaskOwner.query.filter_by(id=owner_id, task_id=task.id).first_or_404()
    _record_project_change(project, "DELETE", "Task owner", f"Removed {owner.member.name} from {task.name}", owner.id)
    db.session.delete(owner)
    db.session.commit()
    flash("Task owner removed.", "success")
    return _project_detail(project)


@bp.post("/tasks/<int:task_id>/steps")
@login_required
def step_add(task_id):
    task = db.get_or_404(Task, task_id)
    project = get_editable_project(task.project_id)
    header = request.form.get("header", "").strip()
    try:
        priority = bounded_int(request.form.get("priority"), "Step priority", 1, 10000)
        percentage = bounded_int(request.form.get("percentage_complete", "0"), "Step completion", 0, 100)
        if not header:
            raise ValueError("Step header is required.")
        step = TaskStep(
                task=task,
                priority=priority,
                header=header,
                description=request.form.get("description", "").strip(),
                percentage_complete=percentage,
            )
        db.session.add(step)
        db.session.flush()
        _record_project_change(project, "CREATE", "Task step", f"Added step {header} to {task.name}", step.id)
        db.session.commit()
        flash("Task step added.", "success")
    except ValueError as error:
        db.session.rollback()
        flash(str(error), "error")
    return _project_detail(project)


@bp.route("/steps/<int:step_id>/edit", methods=["GET", "POST"])
@login_required
def step_edit(step_id):
    step = db.get_or_404(TaskStep, step_id)
    project = get_editable_project(step.task.project_id)
    if request.method == "POST":
        try:
            previous_percentage = step.percentage_complete
            step.priority = bounded_int(request.form.get("priority"), "Step priority", 1, 10000)
            step.percentage_complete = bounded_int(request.form.get("percentage_complete"), "Step completion", 0, 100)
            step.header = request.form.get("header", "").strip()
            step.description = request.form.get("description", "").strip()
            if not step.header:
                raise ValueError("Step header is required.")
            summary = f"Updated step {step.header}"
            if previous_percentage != step.percentage_complete:
                summary += f"; completion {previous_percentage}% to {step.percentage_complete}%"
            _record_project_change(project, "UPDATE", "Task step", summary, step.id)
            db.session.commit()
            flash("Task step updated.", "success")
            return _project_detail(project)
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "error")
    return render_template("step_form.html", project=project, step=step)


@bp.post("/steps/<int:step_id>/delete")
@login_required
def step_delete(step_id):
    step = db.get_or_404(TaskStep, step_id)
    project = get_editable_project(step.task.project_id)
    _record_project_change(project, "DELETE", "Task step", f"Removed step {step.header} from {step.task.name}", step.id)
    db.session.delete(step)
    db.session.commit()
    flash("Task step deleted.", "success")
    return _project_detail(project)


@bp.post("/projects/<int:project_id>/documents")
@login_required
def document_add(project_id):
    project = get_editable_project(project_id)
    upload = request.files.get("attachment")
    if upload is None or not upload.filename:
        flash("Choose a document to upload.", "error")
        return _project_detail(project)
    original_name = secure_filename(upload.filename)
    suffix = original_name.rpartition(".")[2].lower()
    if not original_name or suffix not in ALLOWED_DOCUMENT_EXTENSIONS:
        flash("That file type is not allowed.", "error")
        return _project_detail(project)
    stored_name = f"{uuid4().hex}.{suffix}"
    upload.save(Path(current_app.config["UPLOAD_FOLDER"]) / stored_name)
    document = ProjectDocument(
        project=project,
        filename=original_name,
        stored_name=stored_name,
        description=request.form.get("description", "").strip(),
    )
    db.session.add(document)
    db.session.flush()
    _record_project_change(project, "CREATE", "Document", f"Uploaded document {original_name}", document.id)
    db.session.commit()
    flash("Project document uploaded.", "success")
    return _project_detail(project)


@bp.get("/documents/<int:document_id>/download")
@login_required
def document_download(document_id):
    document = db.get_or_404(ProjectDocument, document_id)
    get_visible_project(document.project_id)
    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"],
        document.stored_name,
        as_attachment=True,
        download_name=document.filename,
    )


@bp.get("/documents/<int:document_id>/secure-link")
@login_required
def document_secure_link(document_id):
    document = db.get_or_404(ProjectDocument, document_id)
    get_visible_project(document.project_id)
    token = generate_signed_document_token(document.id, current_user.id)
    signed_url = url_for("main.signed_document_download", token=token, _external=True)
    if request.headers.get("Accept") == "application/json" or request.args.get("json"):
        return {"document_id": document.id, "filename": document.filename, "signed_url": signed_url, "expires_in_seconds": 3600}
    flash("Secure download link generated.", "info")
    return _project_detail(document.project)


@bp.get("/documents/signed/<token>")
def signed_document_download(token):
    payload = verify_signed_document_token(token, max_age=3600)
    if not payload:
        abort(403)
    document = db.session.get(ProjectDocument, payload["doc_id"])
    if not document:
        abort(404)
    has_access = ProjectMembership.query.filter_by(
        project_id=document.project_id,
        member_id=payload["mem_id"],
    ).first()
    if not has_access:
        abort(403)
    return send_from_directory(
        current_app.config["UPLOAD_FOLDER"],
        document.stored_name,
        as_attachment=True,
        download_name=document.filename,
    )


@bp.post("/documents/<int:document_id>/delete")
@login_required
def document_delete(document_id):
    document = db.get_or_404(ProjectDocument, document_id)
    project = get_editable_project(document.project_id)
    _record_project_change(project, "DELETE", "Document", f"Deleted document {document.filename}", document.id)
    (Path(current_app.config["UPLOAD_FOLDER"]) / document.stored_name).unlink(missing_ok=True)
    db.session.delete(document)
    db.session.commit()
    flash("Project document deleted.", "success")
    return _project_detail(project)


@bp.post("/projects/<int:project_id>/tasks/reorder")
@login_required
def task_reorder(project_id):
    """Reorder tasks via drag-and-drop. Accepts ordered list of task IDs."""
    project = get_editable_project(project_id)
    if not can_edit_tasks(project, current_user):
        abort(403)

    task_ids = request.form.getlist("task_ids", type=int)
    if not task_ids:
        flash("No tasks to reorder.", "error")
        return _project_detail(project)

    # Update task_order for each task based on its position in the list
    for order, task_id in enumerate(task_ids):
        task = db.session.get(Task, task_id)
        if task and task.project_id == project.id:
            task.task_order = order

    db.session.commit()
    _record_project_change(project, "UPDATE", "Task", "Reordered tasks", None)
    flash("Task order updated.", "success")
    return _project_detail(project)


@bp.post("/tasks/<int:task_id>/steps/reorder")
@login_required
def step_reorder(task_id):
    """Reorder steps within a task via drag-and-drop."""
    task = db.get_or_404(Task, task_id)
    project = get_editable_project(task.project_id)
    if not can_edit_tasks(project, current_user):
        abort(403)

    step_ids = request.form.getlist("step_ids", type=int)
    if not step_ids:
        flash("No steps to reorder.", "error")
        return _project_detail(project)

    # Update step priority based on position
    for priority, step_id in enumerate(step_ids, start=1):
        step = db.session.get(TaskStep, step_id)
        if step and step.task_id == task.id:
            step.priority = priority

    db.session.commit()
    _record_project_change(project, "UPDATE", "Task step", f"Reordered steps in task '{task.name}'", task.id)
    flash("Step order updated.", "success")
    return _project_detail(project)