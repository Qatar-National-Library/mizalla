import json
import secrets
from datetime import date, datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .role_meta import MEMBERSHIP_LABELS, MEMBERSHIP_ORDER, MEMBERSHIP_ROLES, PM_TEAM_ROLE


class Member(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    designation = db.Column(db.String(120), nullable=False, default="")
    department_organization = db.Column(db.String(160), nullable=False, default="")
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_pm = db.Column(db.Boolean, nullable=False, default=False)
    api_key = db.Column(db.String(64), unique=True, nullable=True, index=True)

    # Flag indicating whether this member has access to the bulk email feature.
    can_bulk_email = db.Column(db.Boolean, nullable=False, default=False)
    can_add_command_items = db.Column(db.Boolean, nullable=False, default=False)
    can_add_command_groups = db.Column(db.Boolean, nullable=False, default=False)

    memberships = db.relationship("ProjectMembership", back_populates="member", cascade="all, delete-orphan")
    task_assignments = db.relationship("TaskOwner", back_populates="member", cascade="all, delete-orphan")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def generate_api_key(self) -> str:
        self.api_key = secrets.token_hex(32)
        return self.api_key


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    scope = db.Column(db.Text, nullable=False, default="")
    estimated_completion_date = db.Column(db.Date, nullable=True)
    actual_start_date = db.Column(db.Date, nullable=True)
    actual_completion_date = db.Column(db.Date, nullable=True)
    test_server_id = db.Column(db.Integer, db.ForeignKey("server.id"), nullable=True)
    production_server_id = db.Column(db.Integer, db.ForeignKey("server.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    memberships = db.relationship("ProjectMembership", back_populates="project", cascade="all, delete-orphan")
    departments = db.relationship("BusinessDepartment", back_populates="project", cascade="all, delete-orphan")
    documents = db.relationship("ProjectDocument", back_populates="project", cascade="all, delete-orphan")
    technologies = db.relationship("ProjectTechnology", back_populates="project", cascade="all, delete-orphan")
    tasks = db.relationship("Task", back_populates="project", cascade="all, delete-orphan", order_by="Task.priority")
    meetings = db.relationship("Meeting", back_populates="project", cascade="all, delete-orphan", order_by="Meeting.meeting_date")
    audit_events = db.relationship("ProjectAuditEvent", back_populates="project", order_by="ProjectAuditEvent.created_at.desc()")
    test_server = db.relationship("Server", foreign_keys=[test_server_id], back_populates="test_projects")
    production_server = db.relationship("Server", foreign_keys=[production_server_id], back_populates="production_projects")


meeting_attendees = db.Table(
    "meeting_attendees",
    db.Column("meeting_id", db.Integer, db.ForeignKey("meeting.id", ondelete="CASCADE"), primary_key=True),
    db.Column("member_id", db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), primary_key=True),
)


class Meeting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    meeting_date = db.Column(db.Date, nullable=False)
    meeting_start_at = db.Column(db.DateTime, nullable=True)
    meeting_utc_offset_minutes = db.Column(db.Integer, nullable=True)
    agenda = db.Column(db.Text, nullable=False, default="")
    location = db.Column(db.String(240), nullable=False, default="")
    notes = db.Column(db.Text, nullable=False, default="")
    # Ad-hoc invitees who are not member accounts. They receive Teams compose
    # links and reminders but gain no project access.
    external_attendees = db.Column(db.Text, nullable=False, default="")
    project = db.relationship("Project", back_populates="meetings")
    attendees = db.relationship("Member", secondary=meeting_attendees, order_by="Member.name")

    def get_external_attendee_list(self) -> list[str]:
        if not self.external_attendees:
            return []
        return [item.strip().lower() for item in self.external_attendees.split(",") if item.strip()]

    def set_external_attendee_list(self, emails: list[str]) -> None:
        self.external_attendees = ", ".join(dict.fromkeys(e.strip().lower() for e in emails if e.strip()))


class ProjectAuditEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="SET NULL"), nullable=True, index=True)
    project_key = db.Column(db.String(40), nullable=False)
    project_name = db.Column(db.String(180), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    actor_name = db.Column(db.String(120), nullable=False)
    actor_email = db.Column(db.String(254), nullable=False)
    action = db.Column(db.String(10), nullable=False)
    entity_type = db.Column(db.String(40), nullable=False)
    entity_id = db.Column(db.Integer, nullable=True)
    summary = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    project = db.relationship("Project", back_populates="audit_events")


class ProjectMembership(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    team_roles = db.Column(db.String(300), nullable=False, default="")
    responsibility = db.Column(db.String(300), nullable=False, default="")
    __table_args__ = (
        # One row per (project, member, role), which is what lets a member hold
        # several roles on one project while still holding only one of each.
        db.UniqueConstraint("project_id", "member_id", "role"),
        db.CheckConstraint("role IN (%s)" % ", ".join(f"'{r}'" for r in MEMBERSHIP_ROLES)),
    )

    project = db.relationship("Project", back_populates="memberships")
    member = db.relationship("Member", back_populates="memberships")

    def get_team_roles_list(self) -> list[str]:
        if not self.team_roles:
            return []
        return [r.strip() for r in self.team_roles.split(",") if r.strip()]

    def set_team_roles_list(self, roles: list[str]) -> None:
        self.team_roles = ", ".join(dict.fromkeys(r.strip() for r in roles if r.strip()))

    def has_team_role(self, role: str) -> bool:
        wanted = role.strip().casefold()
        return any(item.casefold() == wanted for item in self.get_team_roles_list())

    @property
    def role_label(self) -> str:
        """The role as the screens name it, falling back to the stored value."""
        return MEMBERSHIP_LABELS.get(self.role, self.role)

    def grants(self, capability: str) -> bool:
        """True when this one role grants ``capability``.

        Imported inside the method because access.py imports this module, so a
        module-level import would be circular.
        """
        from .access import role_grants

        return role_grants(self.role, capability)


class BusinessDepartment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    department_name = db.Column(db.String(160), nullable=False)
    responsibility = db.Column(db.String(300), nullable=False, default="")
    project = db.relationship("Project", back_populates="departments")


class ProjectDocument(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), unique=True, nullable=False)
    description = db.Column(db.String(300), nullable=False, default="")
    uploaded_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    project = db.relationship("Project", back_populates="documents")


class ProjectTechnology(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    technology = db.Column(db.String(160), nullable=False)
    project = db.relationship("Project", back_populates="technologies")


class Server(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    ip = db.Column(db.String(45), nullable=False)
    operating_system = db.Column(db.String(160), nullable=False, default="Linux")
    technology_packages = db.Column(db.Text, nullable=False, default="")
    os_type = db.Column(db.String(20), nullable=False, default="linux")  # linux / windows
    os_version = db.Column(db.String(120), nullable=False, default="")
    admin_notes = db.Column(db.Text, nullable=False, default="")
    ssh_port = db.Column(db.Integer, nullable=False, default=22)
    status = db.Column(db.String(20), nullable=False, default="available")  # available / failed
    last_checked = db.Column(db.DateTime(timezone=True), nullable=True)

    auth_token = db.Column(db.String(64), unique=True, nullable=True, index=True)
    agent_status = db.Column(db.String(30), nullable=False, default="offline")
    agent_last_seen = db.Column(db.DateTime(timezone=True), nullable=True)
    connection_attempts = db.Column(db.Integer, nullable=False, default=0)
    max_connection_attempts = db.Column(db.Integer, nullable=False, default=5)
    connection_message = db.Column(db.String(255), nullable=True, default="")

    # Comprehensive Hardware & Runtime Telemetry
    ram_total = db.Column(db.String(40), nullable=True, default="")
    ram_used = db.Column(db.String(40), nullable=True, default="")
    ram_percent = db.Column(db.Float, nullable=True, default=0.0)
    disk_total = db.Column(db.String(40), nullable=True, default="")
    disk_used = db.Column(db.String(40), nullable=True, default="")
    disk_percent = db.Column(db.Float, nullable=True, default=0.0)
    core_tags = db.Column(db.Text, nullable=False, default="")
    k8s_pods = db.Column(db.Text, nullable=True, default="")
    docker_compose_content = db.Column(db.Text, nullable=True, default="")

    test_projects = db.relationship("Project", foreign_keys="Project.test_server_id", back_populates="test_server")
    production_projects = db.relationship("Project", foreign_keys="Project.production_server_id", back_populates="production_server")
    credentials = db.relationship("ServerCredential", back_populates="server", cascade="all, delete-orphan")

    @property
    def friendly_name(self) -> str:
        return self.name

    @friendly_name.setter
    def friendly_name(self, val: str) -> None:
        self.name = val

    @property
    def packages_summary(self) -> str:
        return self.technology_packages

    @packages_summary.setter
    def packages_summary(self, val: str) -> None:
        self.technology_packages = val

    @property
    def is_available(self) -> bool:
        return self.status == "available"

    @property
    def default_credential(self):
        """Returns the admin default credential for this server, if configured."""
        for cred in self.credentials:
            if cred.is_default or cred.user_id is None:
                return cred
        return self.credentials[0] if self.credentials else None

    def ensure_auth_token(self) -> str:
        if not self.auth_token:
            self.auth_token = secrets.token_hex(32)
        return self.auth_token

    def has_tag(self, keyword: str) -> bool:
        """Fast case-insensitive check if a tag/capability is present (e.g. server.has_tag('drupal'))."""
        return keyword.lower() in (self.core_tags or "").lower()

    def get_tag_lines(self) -> list[str]:
        """Returns non-empty lines from core_tags."""
        if not self.core_tags:
            return []
        return [line.strip() for line in self.core_tags.splitlines() if line.strip()]

    def get_tag_values(self) -> list[str]:
        """Returns clean tag display values omitting any tag name prefix."""
        values = []
        for line in self.get_tag_lines():
            if ":" in line:
                val = line.split(":", 1)[1].strip()
                if val:
                    values.append(val)
            else:
                values.append(line.strip())
        return values

    @property
    def has_telemetry(self) -> bool:
        return bool(self.ram_total or self.disk_total or self.core_tags or self.k8s_pods or self.docker_compose_content)

    @property
    def has_docker_compose(self) -> bool:
        """Returns True if docker compose is detected or docker_compose_content is present."""
        return bool(self.docker_compose_content or "[compose:" in (self.core_tags or "").lower() or self.has_tag("compose") or self.has_tag("docker-compose"))

    @property
    def has_k8s(self) -> bool:
        """Returns True if Kubectl or Kubernetes is detected for this server."""
        return self.has_tag("kubernetes") or self.has_tag("kubectl") or bool(self.k8s_pods)

    def get_k8s_pods(self, only_user_running: bool = True) -> list[dict]:
        """Returns parsed list of pods: [{"name": ..., "id": ..., "status": ..., "age": ..., "namespace": ..., "ready": ...}].
        When only_user_running is True (default), filters out kube-system pods and returns only user running pods.
        """
        if not self.k8s_pods:
            return []
        try:
            data = json.loads(self.k8s_pods)
            if isinstance(data, list):
                result = []
                for p in data:
                    if isinstance(p, dict):
                        item = dict(p)
                        if "name" in item and "id" not in item:
                            item["id"] = item["name"]
                        elif "id" in item and "name" not in item:
                            item["name"] = item["id"]
                        result.append(item)
                    elif isinstance(p, str):
                        m = re.search(r"^(.*?)\s*\(([^)]+)\)$", p.strip())
                        if m:
                            pod_name = m.group(1).strip()
                            pod_age = m.group(2).strip()
                            result.append({"id": pod_name, "name": pod_name, "status": "Running", "age": pod_age, "namespace": "default", "ready": ""})
                        else:
                            result.append({"id": p, "name": p, "status": "Running", "age": "", "namespace": "default", "ready": ""})
                if only_user_running:
                    filtered = []
                    for pod in result:
                        ns = (pod.get("namespace") or "").strip().lower()
                        if ns == "kube-system" or ns.startswith("kube-system"):
                            continue
                        name = (pod.get("name") or pod.get("id") or "").strip().lower()
                        if name.startswith("kube-system/"):
                            continue
                        status = (pod.get("status") or "").strip().lower()
                        if status and status not in ("running", "active"):
                            continue
                        filtered.append(pod)
                    return filtered
                return result
        except Exception:
            pass
        return []



class ServerAccess(db.Model):
    """Member access to a Server (many-to-many)."""

    __tablename__ = "server_access"

    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey("server.id", ondelete="CASCADE"), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False, index=True)
    granted_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    granted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    can_view = db.Column(db.Boolean, nullable=False, default=True)
    can_edit = db.Column(db.Boolean, nullable=False, default=False)
    last_connected_at = db.Column(db.DateTime(timezone=True), nullable=True)
    connection_status = db.Column(db.String(20), nullable=False, default="unverified")

    server = db.relationship("Server", backref=db.backref("access_grants", cascade="all, delete-orphan"))
    member = db.relationship("Member", foreign_keys=[member_id], backref="server_access")
    granted_by = db.relationship("Member", foreign_keys=[granted_by_id])

    __table_args__ = (db.UniqueConstraint("server_id", "member_id"),)




class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    task_type = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(180), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="Not Started")
    due_date = db.Column(db.Date, nullable=True)
    priority = db.Column(db.Integer, nullable=False, default=5)
    weight = db.Column(db.Integer, nullable=False, default=5)
    source_type = db.Column(db.String(20), nullable=False, default="Project Plan")
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id", ondelete="SET NULL"), nullable=True)
    jira_ticket_id = db.Column(db.String(80), nullable=True)
    # Admin-configurable highlight labels drawn from the task_highlight_types setting,
    # stored comma separated in the same style as ProjectMembership.team_roles.
    highlights = db.Column(db.String(300), nullable=False, default="")
    # The date a highlight was recorded. Set to today on first mark, then editable.
    highlighted_on = db.Column(db.Date, nullable=True)
    # A milestone is a scheduled checkpoint rather than a highlight label, so it is a
    # flag: it keeps "next milestone" orderable by due_date and survives a label rename.
    is_milestone = db.Column(db.Boolean, nullable=False, default=False)
    # Set the first time a milestone task reaches Completed, and editable thereafter.
    milestone_achieved_on = db.Column(db.Date, nullable=True)
    # Display order for tasks (inferred from position, not shown in UI)
    task_order = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (
        db.CheckConstraint("priority BETWEEN 1 AND 10"),
        db.CheckConstraint("weight BETWEEN 1 AND 10"),
        db.CheckConstraint("task_type IN ('Development', 'Testing', 'UAT', 'Bug Fix', 'New Feature', 'Enhancement')"),
        db.CheckConstraint("status IN ('Not Started', 'In Progress', 'Blocked', 'Completed', 'Cancelled')"),
    )
    project = db.relationship("Project", back_populates="tasks")
    source_meeting = db.relationship("Meeting")
    steps = db.relationship("TaskStep", back_populates="task", cascade="all, delete-orphan", order_by="TaskStep.priority")
    owners = db.relationship("TaskOwner", back_populates="task", cascade="all, delete-orphan")

    def get_highlight_list(self) -> list[str]:
        if not self.highlights:
            return []
        return [item.strip() for item in self.highlights.split(",") if item.strip()]

    def set_highlight_list(self, labels: list[str]) -> None:
        self.highlights = ", ".join(dict.fromkeys(label.strip() for label in labels if label.strip()))

    def has_highlight(self, label: str) -> bool:
        wanted = label.strip().casefold()
        return any(item.casefold() == wanted for item in self.get_highlight_list())

    @property
    def is_achieved(self) -> bool:
        """True once a task counts as delivered.

        Status is authoritative when it is terminal, but a task whose steps all read
        100% is treated as achieved too, so a milestone cannot sit 'achieved-looking'
        in the summary while the status still says In Progress.
        """
        if self.status in {"Completed", "Cancelled"}:
            return self.status == "Completed"
        return bool(self.steps) and all(step.percentage_complete >= 100 for step in self.steps)

    @property
    def task_completion(self) -> int:
        """Percentage complete for this task, averaged across its steps."""
        if not self.steps:
            return 100 if self.status == "Completed" else 0
        return round(sum(step.percentage_complete for step in self.steps) / len(self.steps))

    @property
    def is_at_risk(self) -> bool:
        """An active task that is blocked, overdue, or has slipped its due date."""
        if self.status in {"Completed", "Cancelled"}:
            return False
        if self.status == "Blocked":
            return True
        return bool(self.due_date) and self.due_date < date.today()


class TaskStep(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id", ondelete="CASCADE"), nullable=False)
    priority = db.Column(db.Integer, nullable=False, default=1)
    header = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    percentage_complete = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (
        db.CheckConstraint("priority >= 1"),
        db.CheckConstraint("percentage_complete BETWEEN 0 AND 100"),
    )
    task = db.relationship("Task", back_populates="steps")


class TaskOwner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("task.id", ondelete="CASCADE"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False)
    responsibility = db.Column(db.String(300), nullable=False, default="")
    __table_args__ = (db.UniqueConstraint("task_id", "member_id"),)
    task = db.relationship("Task", back_populates="owners")
    member = db.relationship("Member", back_populates="task_assignments")


class JobRunLog(db.Model):
    """Records when a scheduled job last dispatched, so interval settings hold."""

    id = db.Column(db.Integer, primary_key=True)
    job_name = db.Column(db.String(60), nullable=False, index=True)
    project_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (db.Index("ix_job_run_log_name_project", "job_name", "project_id"),)


class AppSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, nullable=False, default="")
    description = db.Column(db.String(255), nullable=False, default="")
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


TASK_TYPES = ("Development", "Testing", "UAT", "Bug Fix", "New Feature", "Enhancement")
TASK_STATUSES = ("Not Started", "In Progress", "Blocked", "Completed", "Cancelled")
TASK_SOURCE_TYPES = ("Project Plan", "Meeting")
ACTIVE_TASK_STATUSES = tuple(status for status in TASK_STATUSES if status not in {"Completed", "Cancelled"})
# Re-exported so the role metadata stays importable from where it always was.
__all__ = ["MEMBERSHIP_ROLES", "MEMBERSHIP_LABELS", "MEMBERSHIP_ORDER", "PM_TEAM_ROLE"]


class BulkEmailJob(db.Model):
    """A bulk email job: who created it, when, and the final summary."""

    id = db.Column(db.Integer, primary_key=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    created_by_name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    cancelled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    total = db.Column(db.Integer, nullable=False, default=0)
    sent = db.Column(db.Integer, nullable=False, default=0)
    errors = db.Column(db.Integer, nullable=False, default=0)
    duration_seconds = db.Column(db.Integer, nullable=True)
    subject = db.Column(db.String(500), nullable=False)
    sender = db.Column(db.String(254), nullable=False)
    attachment_count = db.Column(db.Integer, nullable=False, default=0)
    summary = db.Column(db.Text, nullable=False, default="")

    created_by = db.relationship("Member", backref="bulk_email_jobs")
    recipients = db.relationship("BulkEmailRecipient", back_populates="job", cascade="all, delete-orphan", order_by="BulkEmailRecipient.id")

    @property
    def status_label(self) -> str:
        if self.cancelled_at:
            return "Cancelled"
        if not self.completed_at:
            return "Running"
        return "Complete"

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None


class BulkEmailRecipient(db.Model):
    """Per-recipient result for a bulk email job."""

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("bulk_email_job.id", ondelete="CASCADE"), nullable=False, index=True)
    to_email = db.Column(db.String(254), nullable=False)
    status = db.Column(db.String(20), nullable=False)  # SENT / ERROR / SKIPPED
    reason = db.Column(db.Text, nullable=False, default="")

    job = db.relationship("BulkEmailJob", back_populates="recipients")


class GowaDevice(db.Model):
    """A registered GOWA WhatsApp device."""

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    device_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="active")  # active / inactive / disconnected
    registered_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    registered_by_name = db.Column(db.String(120), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    registered_by = db.relationship("Member", backref="gowa_devices")
    access_grants = db.relationship("GowaDeviceAccess", back_populates="device", cascade="all, delete-orphan")


class GowaDeviceAccess(db.Model):
    """Member access to a GOWA device (many-to-many)."""

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("gowa_device.id", ondelete="CASCADE"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False)
    granted_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    granted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    device = db.relationship("GowaDevice", back_populates="access_grants")
    member = db.relationship("Member", foreign_keys=[member_id], backref="gowa_device_access")
    granted_by = db.relationship("Member", foreign_keys=[granted_by_id])

    __table_args__ = (db.UniqueConstraint("device_id", "member_id"),)


class WhatsAppJob(db.Model):
    """A bulk WhatsApp message job."""

    __tablename__ = "whatsapp_job"

    id = db.Column(db.Integer, primary_key=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    created_by_name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    cancelled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    total = db.Column(db.Integer, nullable=False, default=0)
    sent = db.Column(db.Integer, nullable=False, default=0)
    errors = db.Column(db.Integer, nullable=False, default=0)
    duration_seconds = db.Column(db.Integer, nullable=True)
    device_id = db.Column(db.Integer, db.ForeignKey("gowa_device.id", ondelete="SET NULL"), nullable=True)
    device_name = db.Column(db.String(120), nullable=False, default="")
    message_preview = db.Column(db.String(500), nullable=False, default="")
    attachment_count = db.Column(db.Integer, nullable=False, default=0)
    summary = db.Column(db.Text, nullable=False, default="")

    created_by = db.relationship("Member", backref="whatsapp_jobs")
    device = db.relationship("GowaDevice", backref="whatsapp_jobs")
    recipients = db.relationship("WhatsAppRecipient", back_populates="job", cascade="all, delete-orphan", order_by="WhatsAppRecipient.id")

    @property
    def status_label(self) -> str:
        if self.cancelled_at:
            return "Cancelled"
        if not self.completed_at:
            return "Running"
        return "Complete"

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None


class WhatsAppRecipient(db.Model):
    """Per-recipient result for a bulk WhatsApp job."""

    __tablename__ = "whatsapp_recipient"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("whatsapp_job.id", ondelete="CASCADE"), nullable=False, index=True)
    phone = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), nullable=False)  # SENT / ERROR / SKIPPED
    reason = db.Column(db.Text, nullable=False, default="")

    job = db.relationship("WhatsAppJob", back_populates="recipients")


class SmsJob(db.Model):
    """A bulk SMS job.

    The same shape as :class:`WhatsAppJob` rather than a shared base, because
    the two differ in what they record: SMS has no device to send from, and it
    stores the sender because that, not the device, is what an auditor needs to
    reproduce a campaign.
    """

    __tablename__ = "sms_job"

    id = db.Column(db.String(48), primary_key=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    created_by_name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    cancelled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    total = db.Column(db.Integer, nullable=False, default=0)
    sent = db.Column(db.Integer, nullable=False, default=0)
    errors = db.Column(db.Integer, nullable=False, default=0)
    duration_seconds = db.Column(db.Integer, nullable=True)
    sender = db.Column(db.String(30), nullable=False, default="")
    region = db.Column(db.String(10), nullable=False, default="")
    message_preview = db.Column(db.String(500), nullable=False, default="")
    summary = db.Column(db.Text, nullable=False, default="")

    created_by = db.relationship("Member", backref="sms_jobs")
    recipients = db.relationship("SmsRecipient", back_populates="job", cascade="all, delete-orphan", order_by="SmsRecipient.id")

    @property
    def status_label(self) -> str:
        if self.cancelled_at:
            return "Cancelled"
        if not self.completed_at:
            return "Running"
        return "Complete"

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None


class SmsRecipient(db.Model):
    """Per-recipient result for a bulk SMS job."""

    __tablename__ = "sms_recipient"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String(48), db.ForeignKey("sms_job.id", ondelete="CASCADE"), nullable=False, index=True)
    phone = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), nullable=False)  # SENT / ERROR / SKIPPED
    reason = db.Column(db.Text, nullable=False, default="")
    tracking_id = db.Column(db.String(80), nullable=False, default="")

    job = db.relationship("SmsJob", back_populates="recipients")


# ─── Feature access ─────────────────────────────────────────────────────────

# Stable keys for the app's optional services. Stored in AppFeatureAccess and
# referenced by the admin UI, the navigation, and each service's own guard.
FEATURE_BULK_EMAIL = "bulk_email"
FEATURE_BULK_WHATSAPP = "bulk_whatsapp"
FEATURE_BULK_SMS = "bulk_sms"
FEATURE_VISION = "vision"
FEATURE_SPEECH = "speech"
FEATURE_CHAT = "chat"
FEATURE_UPLOAD = "upload_file"
FEATURE_VIDEO_UPLOAD = "video_upload"
FEATURE_REMOTE_EXEC = "remote_exec"

#: feature_key -> human label shown in the admin UI and service cards
FEATURE_LABELS = {
    FEATURE_BULK_EMAIL: "Bulk Emailer",
    FEATURE_BULK_WHATSAPP: "Bulk Messenger (WhatsApp)",
    FEATURE_BULK_SMS: "Bulk SMS",
    FEATURE_VISION: "Vision Test Service",
    FEATURE_SPEECH: "Speech Test Service",
    FEATURE_CHAT: "Chat Service",
    FEATURE_UPLOAD: "Upload File",
    FEATURE_VIDEO_UPLOAD: "Video Upload",
    FEATURE_REMOTE_EXEC: "Remote Command Execution",
}

FEATURE_KEYS = tuple(FEATURE_LABELS)


# ─── Remote Execution & Command Orchestration (AsyncSSH) ─────────────────────

COMMAND_STATUS_PENDING = "pending"
COMMAND_STATUS_RUNNING = "running"
COMMAND_STATUS_COMPLETED = "completed"
COMMAND_STATUS_FAILED = "failed"
COMMAND_STATUS_TERMINATED = "terminated"

COMMAND_TYPE_REMOTE = "remote"
COMMAND_TYPE_LOCAL = "local"
COMMAND_TYPE_UPLOAD = "upload"

VAR_TYPE_TEXT = "text"
VAR_TYPE_NUMBER = "number"
VAR_TYPE_BOOLEAN = "boolean"

GROUP_EXEC_SEQUENTIAL = "sequential"
GROUP_EXEC_PARALLEL = "parallel"


class ServerCredential(db.Model):
    """Encrypted SSH credentials (password and/or private key) for a target server.

    If is_default is True or user_id is None, this credential serves as the Admin
    server default credential used by any authorized user with run permissions.
    """

    __tablename__ = "server_credential"

    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey("server.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=True, index=True)
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    username = db.Column(db.String(80), nullable=False, default="")
    encrypted_password = db.Column(db.Text, nullable=False, default="")
    encrypted_private_key = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="available")  # available / failed
    last_tested = db.Column(db.DateTime(timezone=True), nullable=True)

    server = db.relationship("Server", back_populates="credentials")
    user = db.relationship("Member")

    @property
    def password(self) -> str:
        from .crypto_vault import decrypt_secret
        return decrypt_secret(self.encrypted_password)

    @password.setter
    def password(self, val: str) -> None:
        from .crypto_vault import encrypt_secret
        self.encrypted_password = encrypt_secret(val)

    @property
    def private_key(self) -> str:
        from .crypto_vault import decrypt_secret
        return decrypt_secret(self.encrypted_private_key)

    @private_key.setter
    def private_key(self, val: str) -> None:
        from .crypto_vault import encrypt_secret
        self.encrypted_private_key = encrypt_secret(val)


class CommandItem(db.Model):
    """An individual command step (Remote, Local, or File Upload).

    Only runnable against the server it was created for.
    """

    __tablename__ = "command_item"

    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey("server.id", ondelete="CASCADE"), nullable=True, index=True)
    name = db.Column(db.String(180), nullable=False)
    command_type = db.Column(db.String(30), nullable=False, default=COMMAND_TYPE_REMOTE)
    command_content = db.Column(db.Text, nullable=False, default="")
    created_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False, index=True)
    created_on = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    server = db.relationship("Server", backref=db.backref("command_items", lazy="dynamic"))
    creator = db.relationship("Member", backref=db.backref("created_command_items", lazy="dynamic"))
    variables = db.relationship("CommandVariable", back_populates="command_item", cascade="all, delete-orphan")
    permissions = db.relationship("CommandItemPermission", back_populates="command_item", cascade="all, delete-orphan")

    def sync_inferred_variables(self, var_overrides=None):
        """Infer {{ Variable-Name }} placeholders from self.command_content.

        Keep existing user overrides (var_type, default_value, description).
        Prune variables no longer present in script.
        """
        import re
        if self.command_type == "upload" or not self.command_content:
            self.variables.clear()
            return

        from .shared import extract_template_variables
        found_names = extract_template_variables(self.command_content)

        existing_by_name = {v.name: v for v in self.variables}
        new_vars = []
        for name in found_names:
            if name in existing_by_name:
                v = existing_by_name[name]
                if var_overrides and name in var_overrides:
                    ov = var_overrides[name]
                    v.var_type = ov.get("var_type", v.var_type)
                    v.default_value = ov.get("default_value", v.default_value)
                    v.description = ov.get("description", v.description)
                new_vars.append(v)
            else:
                ov = (var_overrides or {}).get(name, {})
                v = CommandVariable(
                    command_item=self,
                    name=name,
                    var_type=ov.get("var_type", VAR_TYPE_TEXT),
                    default_value=ov.get("default_value", ""),
                    description=ov.get("description", ""),
                )
                new_vars.append(v)
        self.variables = new_vars


class CommandVariable(db.Model):
    """Placeholder variable ({{ Name }}) for a CommandItem."""

    __tablename__ = "command_variable"

    id = db.Column(db.Integer, primary_key=True)
    command_item_id = db.Column(db.Integer, db.ForeignKey("command_item.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255), nullable=True, default="")
    var_type = db.Column(db.String(20), nullable=False, default=VAR_TYPE_TEXT)  # text, number, boolean
    default_value = db.Column(db.Text, nullable=False, default="")

    command_item = db.relationship("CommandItem", back_populates="variables")


class CommandGroup(db.Model):
    """Collection of CommandItems to run sequentially (in a single session) or in parallel.

    Only runnable against the server it was created for.
    """

    __tablename__ = "command_group"

    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey("server.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    run_in_parallel = db.Column(db.Boolean, nullable=False, default=False)
    stop_on_error = db.Column(db.Boolean, nullable=False, default=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False, index=True)
    created_on = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    server = db.relationship("Server", backref=db.backref("command_groups", lazy="dynamic"))
    creator = db.relationship("Member", backref=db.backref("created_command_groups", lazy="dynamic"))
    items = db.relationship("CommandGroupItem", back_populates="group", cascade="all, delete-orphan", order_by="CommandGroupItem.sequence_order")
    permissions = db.relationship("CommandGroupPermission", back_populates="group", cascade="all, delete-orphan")


class CommandGroupItem(db.Model):
    """An individual ordered step linking a CommandGroup to a CommandItem."""

    __tablename__ = "command_group_item"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("command_group.id", ondelete="CASCADE"), nullable=False, index=True)
    command_item_id = db.Column(db.Integer, db.ForeignKey("command_item.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence_order = db.Column(db.Integer, nullable=False, default=0)

    group = db.relationship("CommandGroup", back_populates="items")
    command_item = db.relationship("CommandItem")


class CommandGroupPermission(db.Model):
    """User access control for running, viewing logs, editing, and duplicating Command Groups."""

    __tablename__ = "command_group_permission"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("command_group.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False, index=True)
    can_view = db.Column(db.Boolean, nullable=False, default=True)
    can_run = db.Column(db.Boolean, nullable=False, default=True)
    can_view_logs = db.Column(db.Boolean, nullable=False, default=False)
    can_edit = db.Column(db.Boolean, nullable=False, default=False)  # includes delete
    can_create = db.Column(db.Boolean, nullable=False, default=False)
    can_duplicate = db.Column(db.Boolean, nullable=False, default=False)

    group = db.relationship("CommandGroup", back_populates="permissions")
    user = db.relationship("Member")
    __table_args__ = (db.UniqueConstraint("group_id", "user_id"),)


class CommandItemPermission(db.Model):
    """User access control for running, viewing logs, editing, and duplicating Command Items."""

    __tablename__ = "command_item_permission"

    id = db.Column(db.Integer, primary_key=True)
    command_item_id = db.Column(db.Integer, db.ForeignKey("command_item.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False, index=True)
    can_view = db.Column(db.Boolean, nullable=False, default=True)
    can_run = db.Column(db.Boolean, nullable=False, default=True)
    can_view_logs = db.Column(db.Boolean, nullable=False, default=False)
    can_edit = db.Column(db.Boolean, nullable=False, default=False)
    can_duplicate = db.Column(db.Boolean, nullable=False, default=False)

    command_item = db.relationship("CommandItem", back_populates="permissions")
    user = db.relationship("Member")
    __table_args__ = (db.UniqueConstraint("command_item_id", "user_id"),)


class CommandAudit(db.Model):
    """Unified execution audit trail and state for Command Items and Command Groups."""

    __tablename__ = "command_audit"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False, index=True)
    server_id = db.Column(db.Integer, db.ForeignKey("server.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = db.Column(db.Integer, db.ForeignKey("command_group.id", ondelete="SET NULL"), nullable=True, index=True)
    command_item_id = db.Column(db.Integer, db.ForeignKey("command_item.id", ondelete="SET NULL"), nullable=True, index=True)

    entry_type = db.Column(db.String(20), nullable=False, default="item")  # item / group
    title = db.Column(db.String(180), nullable=False, default="")
    command = db.Column(db.Text, nullable=False)
    executed_command = db.Column(db.Text, nullable=False, default="")
    remote_pid = db.Column(db.Integer, nullable=True)

    status = db.Column(db.String(30), nullable=False, default=COMMAND_STATUS_PENDING, index=True)
    start_time = db.Column(db.DateTime(timezone=True), nullable=True)
    end_time = db.Column(db.DateTime(timezone=True), nullable=True)
    exit_code = db.Column(db.Integer, nullable=True)
    full_output = db.Column(db.Text, nullable=False, default="")
    step_results = db.Column(db.Text, nullable=False, default="")
    client_ip = db.Column(db.String(45), nullable=True, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    user = db.relationship("Member", backref=db.backref("command_audits", lazy="dynamic"))
    server = db.relationship("Server", backref=db.backref("command_audits", lazy="dynamic"))
    group = db.relationship("CommandGroup", backref=db.backref("executions", lazy="dynamic"))
    command_item = db.relationship("CommandItem", backref=db.backref("executions", lazy="dynamic"))

    @property
    def steps_data(self) -> list[dict]:
        import json
        if not self.step_results:
            return []
        try:
            return json.loads(self.step_results)
        except Exception:
            return []

    @steps_data.setter
    def steps_data(self, val: list[dict]) -> None:
        import json
        self.step_results = json.dumps(val)

    def get_step_items(self) -> list[dict]:
        """Returns structured step data for nested accordion display, reconstructing if needed."""
        steps = self.steps_data
        if steps:
            return steps
        if self.entry_type == "group" and self.group:
            parsed_steps = []
            output_text = self.full_output or ""
            for gi in self.group.items:
                if not gi.command_item:
                    continue
                c = gi.command_item
                tag = f"[{self.group.name}] [{c.name}]"
                step_lines = [l.replace(tag, "").strip() for l in output_text.splitlines() if tag in l]
                step_out = "\n".join(step_lines).strip()
                status = "completed" if (self.status == "completed" and (self.exit_code == 0 or self.exit_code is None)) else ("failed" if self.status == "failed" else "pending")
                parsed_steps.append({
                    "item_id": c.id,
                    "name": c.name,
                    "command_type": c.command_type,
                    "command_content": c.command_content,
                    "status": status,
                    "exit_code": self.exit_code,
                    "output": step_out or ("(Executed with group)" if self.status == "completed" else ""),
                })
            return parsed_steps
        return []

    @property
    def has_output(self) -> bool:
        return bool(self.full_output and self.full_output.strip())

    @property
    def last_10_lines_output(self) -> str:
        """Returns the last 10 lines of the output for concise accordion display."""
        if not self.full_output:
            return ""
        lines = self.full_output.splitlines()
        return "\n".join(lines[-10:])

    @property
    def last_lines_output(self) -> str:
        return self.last_10_lines_output

    @property
    def is_success(self) -> bool:
        return self.status == COMMAND_STATUS_COMPLETED and (self.exit_code == 0 or self.exit_code is None)

    @property
    def duration_seconds(self) -> float | None:
        """Duration in seconds if completed, or elapsed time from start if running."""
        if not self.start_time:
            return None
        now_or_end = self.end_time or datetime.now(timezone.utc)
        start = self.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if now_or_end.tzinfo is None:
            now_or_end = now_or_end.replace(tzinfo=timezone.utc)
        return max(0.0, (now_or_end - start).total_seconds())



class AppFeatureAccess(db.Model):
    """Per-member access to an optional app service (many-to-many).

    Mirrors :class:`GowaDeviceAccess`, but keyed by a stable feature slug
    rather than a device row, because the messaging services have no
    per-service database record to own the grant.
    """

    __tablename__ = "app_feature_access"

    id = db.Column(db.Integer, primary_key=True)
    feature_key = db.Column(db.String(40), nullable=False, index=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="CASCADE"), nullable=False)
    granted_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    granted_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    member = db.relationship("Member", foreign_keys=[member_id], backref="feature_access")
    granted_by = db.relationship("Member", foreign_keys=[granted_by_id])

    __table_args__ = (db.UniqueConstraint("feature_key", "member_id"),)


# ─── Chat service documents ─────────────────────────────────────────────────

class ChatDocument(db.Model):
    """A document uploaded to the Chat service for question answering."""

    __tablename__ = "chat_document"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    content_hash = db.Column(db.String(64), nullable=False, index=True)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    chunk_count = db.Column(db.Integer, nullable=False, default=0)
    text_length = db.Column(db.Integer, nullable=False, default=0)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    uploaded_by_name = db.Column(db.String(120), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    uploaded_by = db.relationship("Member", backref="chat_documents")
    chunks = db.relationship(
        "ChatDocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="ChatDocumentChunk.ordinal",
    )


class ChatDocumentChunk(db.Model):
    """One retrievable slice of a :class:`ChatDocument`."""

    __tablename__ = "chat_document_chunk"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("chat_document.id", ondelete="CASCADE"), nullable=False, index=True)
    ordinal = db.Column(db.Integer, nullable=False, default=0)
    text = db.Column(db.Text, nullable=False, default="")
    heading = db.Column(db.String(300), nullable=False, default="")

    document = db.relationship("ChatDocument", back_populates="chunks")


# ─── Upload File service ────────────────────────────────────────────────────

class UploadedFile(db.Model):
    """One file a member has placed in the shared Upload File folder.

    The bytes live on disk under :attr:`stored_name`; this row is the index the
    service lists, so the folder can be browsed without reading the directory,
    and so every file keeps its original name and a record of who put it there.

    :attr:`stored_name` is a generated name, never the name the browser sent.
    That keeps two members who each upload ``report.docx`` from overwriting one
    another, and keeps a hostile filename from escaping the upload directory.
    """

    __tablename__ = "uploaded_file"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    uploaded_by_name = db.Column(db.String(120), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    uploaded_by = db.relationship("Member", backref="uploaded_files")


# ─── Video Upload service ────────────────────────────────────────────────────

#: Statuses a stored video moves through. The order is also the order the list
#: sorts in, so the ones needing attention appear first.
VIDEO_STATUSES = ("uploading", "processing", "ready", "error", "unknown")


class VideoUpload(db.Model):
    """One video a member has sent to Cloudflare Stream through this app.

    The bytes are not here and never pass through this application: a direct
    creator upload sends them from the member's browser straight to Cloudflare.
    This row is the local index - enough to show a member what they uploaded, to
    stop the same reservation being issued twice, and to know which Cloudflare id
    to delete later.

    :attr:`media_id` is the ``stream-media-id`` Cloudflare returns when the upload
    is provisioned. It is blank rather than absent when Cloudflare did not send
    one, because an upload that works is worth keeping even if the id to manage it
    later is missing; the list says so rather than pretending the video is fine.

    :attr:`status` starts at ``"uploading"`` because that is the truth at the
    moment the row is written: the URL has been issued but no byte has arrived. It
    is only advanced to ``ready`` from Cloudflare's own answer, never from the
    browser's word, so an interrupted upload cannot look finished.
    """

    __tablename__ = "video_upload"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    #: Cloudflare's own video identifier. Empty when Cloudflare omitted it.
    media_id = db.Column(db.String(64), nullable=False, default="", index=True)
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="uploading", index=True)
    #: Cloudflare's own status text, kept verbatim so the list can show what
    #: Cloudflare said (for example "errored") rather than a translation of it.
    status_detail = db.Column(db.String(120), nullable=False, default="")
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    uploaded_by_name = db.Column(db.String(120), nullable=False, default="")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    uploaded_by = db.relationship("Member", backref="video_uploads")

    @property
    def watch_url(self) -> str:
        return f"https://watch.cloudflarestream.com/{self.media_id}" if self.media_id else ""

    @property
    def embed_url(self) -> str:
        return f"https://iframe.videodelivery.net/{self.media_id}" if self.media_id else ""

    @property
    def hls_manifest_url(self) -> str:
        return f"https://videodelivery.net/{self.media_id}/manifest/video.m3u8" if self.media_id else ""


# ─── Service Audit Log ───────────────────────────────────────────────────────

class ServiceAuditLog(db.Model):
    """Complete audit trail for all service calls (AI, Messaging, Storage).

    Records:
    - Who made the call (actor_id, actor_name, actor_email)
    - When was the call made (created_at timestamp)
    - Values of the request (request_data)
    - Response values (response_data)
    - Service key, action, status, and duration
    """

    __tablename__ = "service_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    service_key = db.Column(db.String(40), nullable=False, index=True)
    service_name = db.Column(db.String(120), nullable=False)
    action = db.Column(db.String(80), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    actor_name = db.Column(db.String(120), nullable=False, default="System/Anonymous")
    actor_email = db.Column(db.String(254), nullable=False, default="")
    request_data = db.Column(db.Text, nullable=False, default="")
    response_data = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="SUCCESS")  # SUCCESS / ERROR / PARTIAL
    duration_ms = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    actor = db.relationship("Member", foreign_keys=[actor_id])


# ─── Utility App Feedback & Suggestions ────────────────────────────────────

FEEDBACK_TARGET_EXISTING = "existing"
FEEDBACK_TARGET_NEW = "new"

FEEDBACK_STATUS_ACTIVE = "active"
FEEDBACK_STATUS_HIDDEN = "hidden"


class UtilityAppFeedback(db.Model):
    """Member feedback, ratings, and suggestions for utility applications.

    Logged-in members can rate (1-10) and suggest improvements to utility apps
    they have access to (target_type='existing'), or request entirely new utility
    apps (target_type='new').

    Admins can review these on the Admin UI dashboard, hide/unhide entries, and
    record administrative notes/comments. Entries are preserved rather than deleted.
    """

    __tablename__ = "utility_app_feedback"

    id = db.Column(db.Integer, primary_key=True)
    target_type = db.Column(db.String(20), nullable=False, default=FEEDBACK_TARGET_EXISTING, index=True)
    # feature_key matches FEATURE_KEYS if target_type is 'existing', or empty if 'new'
    feature_key = db.Column(db.String(40), nullable=True, index=True)
    # Human label of the app or title of the requested app
    app_title = db.Column(db.String(180), nullable=False)
    # Rating 1-10 for existing utility apps; None for new app suggestions
    rating = db.Column(db.Integer, nullable=True)
    # Optional or descriptive comments / improvement suggestions
    comments = db.Column(db.Text, nullable=False, default="")
    
    # Author
    member_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True, index=True)
    member_name = db.Column(db.String(120), nullable=False, default="")
    member_email = db.Column(db.String(254), nullable=False, default="")

    # Admin moderation & notes
    status = db.Column(db.String(20), nullable=False, default=FEEDBACK_STATUS_ACTIVE, index=True)  # active / hidden
    admin_notes = db.Column(db.Text, nullable=False, default="")
    admin_reviewed_by_id = db.Column(db.Integer, db.ForeignKey("member.id", ondelete="SET NULL"), nullable=True)
    admin_reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    member = db.relationship("Member", foreign_keys=[member_id], backref="feedbacks")
    admin_reviewer = db.relationship("Member", foreign_keys=[admin_reviewed_by_id])

    __table_args__ = (
        db.CheckConstraint("rating IS NULL OR (rating >= 1 AND rating <= 10)"),
        db.CheckConstraint(f"target_type IN ('{FEEDBACK_TARGET_EXISTING}', '{FEEDBACK_TARGET_NEW}')"),
        db.CheckConstraint(f"status IN ('{FEEDBACK_STATUS_ACTIVE}', '{FEEDBACK_STATUS_HIDDEN}')"),
    )

    @property
    def is_hidden(self) -> bool:
        return self.status == FEEDBACK_STATUS_HIDDEN


