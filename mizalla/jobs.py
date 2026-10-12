import logging
from datetime import date, datetime, timedelta, timezone

from .email import send_email
from .extensions import db
from .models import ACTIVE_TASK_STATUSES, JobRunLog, Meeting, Project, ProjectAuditEvent, Task
from .progress import project_completion
from .settings import get_setting

logger = logging.getLogger("mizalla.jobs")


def _get_app_base_url() -> str:
    url = get_setting("app_base_url", "http://127.0.0.1:5000/apps/mizalla").rstrip("/")
    return url


def send_pm_status_reports() -> int:
    """
    Email Project Status report to Project Managers for all active projects.
    Returns the number of email reports dispatched.
    """
    app_url = _get_app_base_url()
    min_interval_days = _int_setting("email_status_report_days", 7)

    projects = Project.query.all()
    count = 0

    for project in projects:
        # Only members explicitly holding the Project Manager team role receive
        # the digest. There is deliberately no fallback to the wider membership:
        # status digests stay with the project managers.
        pm_memberships = [
            m
            for m in project.memberships
            if "Project Manager" in m.get_team_roles_list() and m.member and m.member.email
        ]
        if not pm_memberships:
            continue

        recipients = {pm_m.member.email: pm_m.member.name for pm_m in pm_memberships}

        # Respect the configured interval so repeated runs do not re-send.
        if not _pm_report_due(project.id, min_interval_days):
            continue

        total_tasks = len(project.tasks)
        completed_tasks = sum(1 for t in project.tasks if t.status == "Completed")
        in_progress_tasks = sum(1 for t in project.tasks if t.status == "In Progress")
        blocked_tasks = sum(1 for t in project.tasks if t.status == "Blocked")
        # Match the figure shown on the project page: each step percentage
        # weighted by its parent task's weight.
        completion_pct = project_completion(project.tasks)

        project_link = f"{app_url}/projects/{project.id}"
        project_list_link = f"{app_url}/"

        subject = f"[Project Status Report] {project.name} ({project.project_id})"
        html_body = f"""
        <html>
        <body style="font-family: sans-serif; color: #333; line-height: 1.6;">
            <h2 style="color: #0f172a;">Project Status Digest: {project.name}</h2>
            <p><strong>Project Code:</strong> {project.project_id}</p>
            <p><strong>Overall Completion:</strong> <strong>{completion_pct}%</strong></p>
            <ul>
                <li>Total Tasks: {total_tasks}</li>
                <li>Completed: {completed_tasks}</li>
                <li>In Progress: {in_progress_tasks}</li>
                <li>Blocked: {blocked_tasks}</li>
            </ul>
            <p>
                <a href="{project_link}" style="background-color: #2563eb; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 6px; display: inline-block;">View Project Details</a>
                &nbsp;
                <a href="{project_list_link}" style="color: #2563eb; text-decoration: underline;">View All Projects</a>
            </p>
        </body>
        </html>
        """

        text_body = f"Project Status Digest: {project.name}\nCompletion: {completion_pct}%\nTasks: {total_tasks} total, {completed_tasks} completed.\nView: {project_link}"

        for email in recipients.keys():
            if send_email(to_email=email, subject=subject, html_body=html_body, text_body=text_body):
                count += 1
                db.session.add(JobRunLog(job_name="pm_status_reports", project_id=project.id))

    if count:
        db.session.commit()

    return count


def _int_setting(key: str, fallback: int) -> int:
    """Read a numeric hot setting, falling back when it is missing or invalid."""
    try:
        return int(get_setting(key, str(fallback)))
    except (TypeError, ValueError):
        return fallback


def _pm_report_due(project_id: int, interval_days: int) -> bool:
    """True when no PM status digest was sent for the project within the interval."""
    if interval_days <= 0:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(days=interval_days)
    last_sent = db.session.scalar(
        db.select(JobRunLog)
        .filter(JobRunLog.job_name == "pm_status_reports", JobRunLog.project_id == project_id)
        .order_by(JobRunLog.created_at.desc())
        .limit(1)
    )
    if last_sent is None:
        return True
    # SQLite drops the offset, so a stored value comes back naive. Treat it as
    # UTC rather than letting a naive/aware comparison raise.
    sent_at = last_sent.created_at
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return sent_at < cutoff


def send_task_due_reminders() -> int:
    """
    Email Task Owners a reminder when task end dates are approaching.
    """
    days_ahead = int(get_setting("email_due_reminder_days", "2"))
    today = date.today()
    target_date = today + timedelta(days=days_ahead)
    app_url = _get_app_base_url()

    tasks = Task.query.filter(
        Task.status.in_(ACTIVE_TASK_STATUSES),
        Task.due_date.isnot(None),
        Task.due_date <= target_date,
        Task.due_date >= today,
    ).all()

    count = 0
    for task in tasks:
        task_link = f"{app_url}/projects/{task.project.id}#task-{task.id}"
        project_list_link = f"{app_url}/"

        subject = f"[Reminder] Task Due Soon: '{task.name}' ({task.project.name})"

        for owner_assoc in task.owners:
            member = owner_assoc.member
            if not member or not member.email:
                continue

            html_body = f"""
            <html>
            <body style="font-family: sans-serif; color: #333; line-height: 1.6;">
                <p>Hello <strong>{member.name}</strong>,</p>
                <p>This is a scheduled reminder that your assigned task <strong>"{task.name}"</strong> in project <em>{task.project.name}</em> is due on <strong>{task.due_date}</strong>.</p>
                <p><strong>Status:</strong> {task.status} | <strong>Priority:</strong> {task.priority}/10</p>
                <p>
                    <a href="{task_link}" style="background-color: #0284c7; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 6px; display: inline-block;">Open Task #{task.id}</a>
                    &nbsp;
                    <a href="{project_list_link}" style="color: #0284c7;">Go to Project List</a>
                </p>
            </body>
            </html>
            """
            text_body = f"Reminder: Task '{task.name}' is due on {task.due_date}.\nOpen: {task_link}"

            if send_email(to_email=member.email, subject=subject, html_body=html_body, text_body=text_body):
                count += 1

    return count


def send_stale_task_nudges() -> int:
    """
    Send a gentle morale-boosting nudge to Task Owners if active tasks haven't had recent activity.
    """
    stale_days = int(get_setting("email_stale_task_nudge_days", "5"))
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=stale_days)
    app_url = _get_app_base_url()

    active_tasks = Task.query.filter(Task.status.in_(ACTIVE_TASK_STATUSES)).all()
    count = 0

    for task in active_tasks:
        # Check audit events for this specific task
        recent_event = ProjectAuditEvent.query.filter(
            ProjectAuditEvent.entity_type == "Task",
            ProjectAuditEvent.entity_id == task.id,
            ProjectAuditEvent.created_at >= cutoff_dt,
        ).first()

        if recent_event:
            continue

        task_link = f"{app_url}/projects/{task.project.id}#task-{task.id}"
        project_list_link = f"{app_url}/"

        subject = f"✨ Quick check-in on '{task.name}' — You've got this!"

        for owner_assoc in task.owners:
            member = owner_assoc.member
            if not member or not member.email:
                continue

            html_body = f"""
            <html>
            <body style="font-family: sans-serif; color: #333; line-height: 1.6;">
                <p>Hi <strong>{member.name}</strong> 👋,</p>
                <p>We noticed that <strong>"{task.name}"</strong> in <em>{task.project.name}</em> hasn't had updates over the past {stale_days} days.</p>
                <p>Great progress happens step by step! If you're blocked or need a hand from the team, reach out on the project board or post an update.</p>
                <div style="margin: 20px 0;">
                    <a href="{task_link}" style="background-color: #10b981; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 6px; font-weight: bold;">Update Task Progress</a>
                    &nbsp;
                    <a href="{project_list_link}" style="color: #10b981;">View All Projects</a>
                </div>
                <p>Keep up the great work!</p>
            </body>
            </html>
            """
            text_body = f"Hi {member.name}! Gentle check-in on '{task.name}'. You've got this! Update task: {task_link}"

            if send_email(to_email=member.email, subject=subject, html_body=html_body, text_body=text_body):
                count += 1

    return count


def send_overdue_task_alerts() -> int:
    """
    Send an overdue alert to Task Owners and Project Managers for tasks past their due date.
    """
    today = date.today()
    app_url = _get_app_base_url()

    overdue_tasks = Task.query.filter(
        Task.status.in_(ACTIVE_TASK_STATUSES),
        Task.due_date.isnot(None),
        Task.due_date < today,
    ).all()

    count = 0
    for task in overdue_tasks:
        task_link = f"{app_url}/projects/{task.project.id}#task-{task.id}"
        project_list_link = f"{app_url}/"
        subject = f"⚠️ [Overdue Alert] Task '{task.name}' ({task.project.name})"

        recipients = set()
        for owner_assoc in task.owners:
            if owner_assoc.member and owner_assoc.member.email:
                recipients.add(owner_assoc.member.email)

        for pm_m in task.project.memberships:
            if "Project Manager" in pm_m.get_team_roles_list() and pm_m.member and pm_m.member.email:
                recipients.add(pm_m.member.email)

        for email in recipients:
            html_body = f"""
            <html>
            <body style="font-family: sans-serif; color: #333; line-height: 1.6;">
                <h3 style="color: #dc2626;">⚠️ Overdue Task Alert</h3>
                <p>Task <strong>"{task.name}"</strong> in project <em>{task.project.name}</em> was due on <strong>{task.due_date}</strong> and remains in <strong>{task.status}</strong> status.</p>
                <p>
                    <a href="{task_link}" style="background-color: #dc2626; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 6px; display: inline-block;">Resolve Task Now</a>
                    &nbsp;
                    <a href="{project_list_link}" style="color: #dc2626;">View All Projects</a>
                </p>
            </body>
            </html>
            """
            text_body = f"Overdue Alert: Task '{task.name}' was due on {task.due_date}.\nResolve: {task_link}"

            if send_email(to_email=email, subject=subject, html_body=html_body, text_body=text_body):
                count += 1

    return count


def send_upcoming_meeting_reminders() -> int:
    """
    Send meeting reminders to attendees for meetings happening soon.
    """
    hours_ahead = int(get_setting("email_upcoming_meeting_hours", "24"))
    today = date.today()
    target_date = today + timedelta(days=max(1, hours_ahead // 24))
    app_url = _get_app_base_url()

    meetings = Meeting.query.filter(
        Meeting.meeting_date >= today,
        Meeting.meeting_date <= target_date,
    ).all()

    count = 0
    for meeting in meetings:
        project_link = f"{app_url}/projects/{meeting.project.id}"
        project_list_link = f"{app_url}/"
        subject = f"📅 [Meeting Reminder] {meeting.project.name} - {meeting.meeting_date}"

        # One reminder per invitee: member attendees keep their name in the
        # greeting, ad-hoc invitees are addressed by address.
        for recipient_name, recipient_email in _meeting_reminder_recipients(meeting):
            html_body = f"""
            <html>
            <body style="font-family: sans-serif; color: #333; line-height: 1.6;">
                <h3 style="color: #4f46e5;">📅 Upcoming Meeting Reminder</h3>
                <p>Hello <strong>{recipient_name}</strong>,</p>
                <p>You have an upcoming meeting for project <em>{meeting.project.name}</em> on <strong>{meeting.meeting_date}</strong>.</p>
                <p><strong>Location/Link:</strong> {meeting.location or 'Not specified'}</p>
                <p><strong>Agenda:</strong> {meeting.agenda or 'No agenda specified.'}</p>
                <p>
                    <a href="{project_link}" style="background-color: #4f46e5; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 6px; display: inline-block;">View Project & Agenda</a>
                    &nbsp;
                    <a href="{project_list_link}" style="color: #4f46e5;">Project List</a>
                </p>
            </body>
            </html>
            """
            text_body = f"Meeting Reminder: {meeting.project.name} on {meeting.meeting_date}.\nAgenda: {meeting.agenda}\nView: {project_link}"

            if send_email(to_email=recipient_email, subject=subject, html_body=html_body, text_body=text_body):
                count += 1

    return count


def _meeting_reminder_recipients(meeting) -> list[tuple[str, str]]:
    """``(greeting_name, email)`` pairs for a meeting, external invitees included."""
    recipients: list[tuple[str, str]] = []
    seen: set[str] = set()
    for member in meeting.attendees:
        email = (member.email or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            recipients.append((member.name or email, email))
    for email in meeting.get_external_attendee_list():
        if email and email not in seen:
            seen.add(email)
            recipients.append((email, email))
    return recipients


def run_all_scheduled_jobs() -> dict[str, int]:
    """
    Dispatcher function to execute all scheduled jobs.
    Returns a dictionary of execution summary counts.
    """
    logger.info("Executing scheduled email jobs...")
    results = {
        "pm_status_reports": send_pm_status_reports(),
        "task_due_reminders": send_task_due_reminders(),
        "stale_task_nudges": send_stale_task_nudges(),
        "overdue_task_alerts": send_overdue_task_alerts(),
        "upcoming_meeting_reminders": send_upcoming_meeting_reminders(),
    }
    logger.info("Scheduled jobs completed: %s", results)
    return results
