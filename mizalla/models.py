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
    operating_system = db.Column(db.String(160), nullable=False)
    technology_packages = db.Column(db.Text, nullable=False, default="")
    test_projects = db.relationship("Project", foreign_keys="Project.test_server_id", back_populates="test_server")
    production_projects = db.relationship("Project", foreign_keys="Project.production_server_id", back_populates="production_server")


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
}

FEATURE_KEYS = tuple(FEATURE_LABELS)


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

