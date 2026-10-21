from pathlib import Path

from flask import current_app

from .extensions import db
from .models import AppSetting

DEFAULT_APP_SETTINGS = {
    "jira_base_url": ("https://jira.qnl.qa/", "Default base URL for rendering JIRA ticket links"),
    "project_team_roles": (
        "Project Manager, Analyst, Developer/Implementer, Qa/Tester, Infrastructure Management, Integration Manager, Configuration Manager, Deployment Manager",
        "Comma-separated list of configurable Project Team roles",
    ),
    "app_base_url": ("http://127.0.0.1:5000/apps/mizalla", "Base URL of the application for email links back to projects/tasks"),
    "sendgrid_from_email": ("noreply@mizalla.local", "Sender email address for SendGrid notification emails"),
    "email_status_report_days": ("7", "Interval in days for emailing Project Status summaries to Project Managers"),
    "email_due_reminder_days": ("2", "Days before task due date to send reminder to Task Owners"),
    "email_stale_task_nudge_days": ("5", "Days of inactivity on active tasks before sending a gentle morale nudge to Task Owners"),
    "email_overdue_alert_days": ("1", "Days past due date before sending an overdue task alert digest"),
    "email_upcoming_meeting_hours": ("24", "Hours before scheduled meeting to send attendee reminder"),
    "task_highlight_types": (
        "Key Amendment, Key Finding, Key Decision",
        "Comma-separated highlight labels an administrator may apply to a task (Key Amendment, Key Finding, Key Decision, ...)",
    ),
    "gowa_mcp_base_url": ("http://127.0.0.1:3000", "Base URL of the GOWA MCP server for WhatsApp integration"),
    "test_email_account": ("", "Email address for testing bulk email campaigns before sending"),
    "test_whatsapp_number": ("", "WhatsApp number for testing bulk WhatsApp campaigns before sending"),
    "whatsapp_throttle_base_delay": ("5", "Base delay in seconds between sending WhatsApp messages"),
    "whatsapp_throttle_jitter": ("5", "Random jitter in seconds added to the base delay"),
    "whatsapp_batch_size": ("100", "Number of WhatsApp messages to send before taking a batch pause"),
    "whatsapp_batch_delay": ("120", "Pause in seconds between batches of WhatsApp messages"),
    "test_sms_number": ("", "Mobile number for testing bulk SMS campaigns before sending"),
    "task_edit_permission": ("team", "Who can edit/add/re-order tasks and steps: 'team' (all project team members) or 'pm' (Project Manager only)"),
    # ── Azure AI services ──
    # Only behaviour belongs in this table. Credentials are read from the
    # environment by azure_ai.credential() and are deliberately absent here, so
    # there is no settings row for a key and no way to type one into the UI.
    "vision_id_mapping": (
        '{"ID": ""}',
        "JSON mapping for the Vision service. Keys are output field names. A value may be a JSONPath into the "
        "Vision response (e.g. read.blocks.0.lines.0.text), a label to search for in the OCR text (e.g. 'ID:'), "
        "or empty to fall back to the full OCR text. Tried in that order.",
    ),
    "chat_chunk_chars": ("1200", "Approximate characters per retrievable document chunk"),
    "chat_max_chunks": ("6", "Maximum number of document chunks sent to the model per question"),
    # ── TLS ──
    # Some corporate networks terminate TLS with an inspecting proxy whose CA is
    # not in the machine trust store. Azure SDK calls then fail with
    # CERTIFICATE_VERIFY_FAILED. This drops certificate verification so those
    # calls succeed. It is a security trade-off: leave it off on any network
    # you do not control.
    "azure_skip_tls_verify": (
        "true",
        "Skip TLS certificate verification for Azure AI service calls. Enable only when a "
        "TLS-inspecting proxy or antivirus on this network causes CERTIFICATE_VERIFY_FAILED.",
    ),
    # ── Upload File service ──
    # Empty means "use the application's own uploads folder", which is the
    # behaviour before this setting existed. The service still creates its own
    # subfolder underneath, so pointing this at the shared uploads folder cannot
    # mix stored files in with project attachments.
    "upload_file_folder": (
        "",
        "Folder the Upload File service stores files in, on a drive or share the application can "
        "write to. A relative path is anchored when it is saved. Leave blank to use the "
        "application's own uploads folder.",
    ),
    "upload_file_base_url": (
        "",
        "Base URL for generating public file links after upload (e.g. https://files.example.com or http://127.0.0.1:5000/uploads). When set, uploading a file displays a success message with a link built as base URL / filename.",
    ),
    # ── Video Upload service ──
    # Cloudflare Stream deducts this much of the account's storage from the moment
    # an upload URL is issued until the video is either stored (at its real
    # duration) or the reservation lapses. So it is a budget decision, not a
    # technical limit, and it belongs with the other hot settings.
    "video_upload_max_duration_seconds": (
        "3600",
        "Maximum video length, in seconds, that Cloudflare Stream will accept from the Video "
        "Upload service. This much storage is reserved per upload while it runs. 3600 is one hour.",
    ),
    "font_paths": (
        "C:/Windows/Fonts/arial.ttf\n"
        "C:/Windows/Fonts/tahoma.ttf\n"
        "C:/Windows/Fonts/seguiemj.ttf\n"
        "C:/Windows/Fonts/calibri.ttf\n"
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf\n"
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf\n"
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf\n"
        "/Library/Fonts/Arial.ttf\n"
        "/Library/Fonts/Helvetica.ttf",
        "Configured font file paths for PDF rendering and substitution (one path per line, in order of preference). The system checks paths sequentially and selects the first one that supports the text language.",
    ),
    "lock_substituted_pdf": (
        "true",
        "Lock substituted PDF attachments against editing using permissions encryption. When enabled, generated PDFs open without a password prompt but are restricted from modification, form editing, and copying.",
    ),
    "pdf_owner_password": (
        "",
        "Optional owner password for unlocking secured PDF attachments in PDF editors (leave blank to use system secure key).",
    ),
    "pdf_text_alignment": (
        "center",
        "Default text alignment for substituted PDF attachments ('center', 'directional', 'left', 'right'). Directional uses left alignment for English and right alignment for Arabic.",
    ),
}

#: Keys that used to be editable here and are now environment-only. Any row
#: still carrying one is deleted on startup, so a credential typed into an older
#: admin form stops sitting in the database (and in its backups) rather than
#: lingering as a dead but readable secret.
RETIRED_SETTING_KEYS = frozenset({
    "azure_vision_endpoint",
    "azure_vision_key",
    "azure_speech_key",
    "azure_speech_region",
    "azure_speech_voice",
    "azure_openai_endpoint",
    "azure_openai_key",
    "azure_openai_deployment",
    "azure_openai_api_version",
})

# Maps a hot setting key to the app.config key that holds its boot-time value.
# The Azure credentials are absent: they are environment-only, and
# azure_ai.credential() reads them from os.environ directly.
_CONFIG_KEYS = {
    "jira_base_url": "JIRA_BASE_URL",
    "project_team_roles": "PROJECT_TEAM_ROLES",
    "app_base_url": "APP_BASE_URL",
    "sendgrid_from_email": "SENDGRID_FROM_EMAIL",
}


def _config_fallback(key: str) -> str:
    """Return the boot-time environment value for a setting, if configured.

    ``create_app`` loads these from the process environment or ``.env`` into
    ``app.config``. They act as the value to seed a fresh database with and as
    the fallback whenever an admin has not overridden the setting in the UI.
    """
    configured = current_app.config.get(_CONFIG_KEYS.get(key, ""), "")
    return configured.strip() if isinstance(configured, str) else ""


def get_setting(key: str, default: str | None = None) -> str:
    """Resolve a setting: database value, then boot config, then default.

    The database wins so an administrator override takes effect immediately.
    The boot config is consulted next so an environment-provided value is not
    silently discarded by the built-in default.
    """
    try:
        with db.session.no_autoflush:
            setting = db.session.scalar(db.select(AppSetting).filter_by(key=key))
            if setting and setting.value and setting.value.strip():
                return setting.value.strip()
    except Exception:
        pass

    configured = _config_fallback(key)
    if configured:
        return configured

    if key in DEFAULT_APP_SETTINGS:
        return DEFAULT_APP_SETTINGS[key][0]
    return default or ""


def set_setting(key: str, value: str, description: str = "") -> AppSetting:
    """Create or update a hot setting in the database."""
    setting = db.session.scalar(db.select(AppSetting).filter_by(key=key))
    if setting is None:
        setting = AppSetting(key=key, value=value.strip(), description=description)
        db.session.add(setting)
    else:
        setting.value = value.strip()
        if description:
            setting.description = description
    db.session.commit()
    return setting


def _comma_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_project_team_roles_list() -> list[str]:
    raw = get_setting("project_team_roles")
    if raw:
        roles = [r.strip() for r in raw.split(",") if r.strip()]
        if roles:
            return roles
    return _comma_list(DEFAULT_APP_SETTINGS["project_team_roles"][0])


def can_edit_tasks(project, member) -> bool:
    """Check if a member can edit/add/re-order tasks and steps for a project.

    Resolution order:
    1. If member is admin, they can always edit
    2. If none of the member's roles on this project grant edit, they cannot.
       Delegated PM and Delegated Backup are team-equivalent, so both pass; the
       read-only roles do not.
    3. Otherwise the task_edit_permission setting decides:
       - 'team': anyone who passed step 2
       - 'pm': only Project Managers of this project, which is a Delegated PM
         role or a Project team membership holding the "Project Manager" team role

    Every role the member holds is considered, not just the first one found: a
    member can be a Vendor and a Developer on the same project, and the Vendor
    row must not decide the answer.
    """
    from .access import can_edit_project, is_project_pm

    if member.is_admin:
        return True
    if not can_edit_project(project, member):
        return False

    perm = get_setting("task_edit_permission", "team").strip().lower()
    if perm == "pm":
        return is_project_pm(project, member)
    return True


def get_task_highlight_types_list() -> list[str]:
    """Highlight labels an administrator may apply to a task, de-duplicated in order.

    A stored label that no longer appears in the setting is still honoured when it is
    read back from a task, so removing a label from the setting hides it from the
    picker without silently discarding what was already recorded.
    """
    raw = get_setting("task_highlight_types")
    labels = list(dict.fromkeys(_comma_list(raw)))
    if labels:
        return labels
    return _comma_list(DEFAULT_APP_SETTINGS["task_highlight_types"][0])


def validate_label_list(raw: str, setting_key: str) -> list[str]:
    """Parse an admin-edited comma-separated label list, rejecting unusable values.

    Raises ValueError with an admin-readable message so the settings page can flash the
    problem, rather than saving a list that would leave every checkbox blank.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        if len(item) > 80:
            raise ValueError(f"'{item[:40]}...' is too long. Keep each label under 80 characters.")
        if item.casefold() in seen:
            raise ValueError(f"'{item}' is listed more than once.")
        seen.add(item.casefold())
        cleaned.append(item)
    if not cleaned:
        raise ValueError(f"At least one label is required for {setting_key}.")
    return cleaned


# Settings whose value is a hand-maintained comma-separated list. These are validated
# on save so a blank entry or a stray comma cannot leave the UI with nothing to pick.
LABEL_LIST_SETTING_KEYS = ("project_team_roles", "task_highlight_types")

#: Settings whose value is a multiline textarea (one entry per line, preserving order).
MULTILINE_SETTING_KEYS = ("font_paths",)

#: Settings stored as the strings "true"/"false" and edited as a checkbox. Anything
#: not "true" counts as off, so a hand-edited row cannot enable one by accident.
BOOLEAN_SETTING_KEYS = ("azure_skip_tls_verify",)

#: Settings naming a directory on the server. Blank means "use the application default".
DIRECTORY_SETTING_KEYS = ("upload_file_folder",)

#: Settings holding a whole number, with the bounds each one is held to.
#:
#: key -> (inclusive minimum, inclusive maximum). The bounds exist because these
#: values are forwarded to an upstream API as limits, and a mistyped value there
#: either reserves an absurd amount of storage or silently rejects every upload.
INTEGER_SETTING_KEYS = {
    "video_upload_max_duration_seconds": (60, 86400),
    "chat_chunk_chars": (100, 10000),
    "chat_max_chunks": (1, 50),
    "email_status_report_days": (1, 365),
    "email_due_reminder_days": (0, 30),
    "email_stale_task_nudge_days": (1, 90),
    "email_overdue_alert_days": (0, 30),
    "email_upcoming_meeting_hours": (1, 168),
    "whatsapp_throttle_base_delay": (0, 600),
    "whatsapp_throttle_jitter": (0, 600),
    "whatsapp_batch_size": (1, 1000),
    "whatsapp_batch_delay": (0, 3600),
}


def validate_integer_setting(raw: str, setting_key: str) -> str:
    """Check a whole-number setting against its bounds, or raise ValueError.

    Bounds live in :data:`INTEGER_SETTING_KEYS` rather than in the call site so
    that the settings page and the service cannot disagree about what is legal.
    """
    low, high = INTEGER_SETTING_KEYS[setting_key]
    cleaned = (raw or "").strip()
    try:
        value = int(cleaned)
    except ValueError:
        raise ValueError(f"'{cleaned}' is not a whole number.") from None

    if not low <= value <= high:
        raise ValueError(f"Enter a number between {low} and {high}.")
    return str(value)


def validate_directory_setting(raw: str, setting_key: str) -> str:
    """Normalise and check a directory setting, or raise ValueError for the admin.

    Checked at save time rather than at first use, because the alternative is an
    administrator saving a typo and only finding out when a member's upload
    fails. The checks are the ones that need no write:

    * blank is allowed and means the application default.
    * ``~`` is expanded and the path made absolute, so the stored value keeps
      meaning the same folder after a restart or a change of working directory.
    * it must not name an existing *file* - that would otherwise turn every upload
      into an error with a confusing "is a file" message at send time.
    * the nearest existing ancestor must be a directory, which catches a path
      running through a file, such as ``/tmp/notes.txt/uploads``.

    Deliberately not checked: that the application can actually write there. That
    needs a write attempt, and doing one from a settings save would create
    directories as a side effect of a form post.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""

    path = Path(cleaned).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()

    if path.exists() and not path.is_dir():
        raise ValueError(f"'{path}' is a file, not a folder.")

    ancestor = path
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if ancestor.exists() and not ancestor.is_dir():
        raise ValueError(f"'{path}' cannot be created because '{ancestor}' is a file.")

    return str(path)


def reset_setting(key: str) -> bool:
    """Restore a setting to its built-in default. Returns False for an unknown key."""
    if key not in DEFAULT_APP_SETTINGS:
        return False
    default_value, description = DEFAULT_APP_SETTINGS[key]
    set_setting(key, default_value, description)
    return True


def purge_retired_settings() -> int:
    """Delete settings rows for keys that are now environment-only.

    Returns the number of rows removed. Runs on every startup so a deployment
    that previously stored a credential in the database stops doing so the first
    time it boots the new code, without waiting for someone to visit /settings.
    """
    try:
        stale = db.session.scalars(
            db.select(AppSetting).where(AppSetting.key.in_(sorted(RETIRED_SETTING_KEYS)))
        ).all()
        if not stale:
            return 0
        for setting in stale:
            db.session.delete(setting)
        db.session.commit()
        return len(stale)
    except Exception:
        db.session.rollback()
        return 0


def seed_default_settings() -> None:
    """Ensure default settings exist in the AppSetting table.

    Seeding uses the resolved value (environment first, then the built-in
    default) so an environment-provided setting is recorded rather than
    overwritten by the built-in default on the first visit to /settings.
    """
    try:
        existing = {s.key: s for s in AppSetting.query.all()}
        added = False
        for key, (_, desc) in DEFAULT_APP_SETTINGS.items():
            if key not in existing:
                db.session.add(AppSetting(key=key, value=get_setting(key), description=desc))
                added = True
        if added:
            db.session.commit()
    except Exception:
        db.session.rollback()
