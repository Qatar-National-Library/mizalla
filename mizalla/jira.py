import re
from urllib.parse import quote, urlsplit, urlunsplit

from .settings import get_setting

JIRA_TICKET_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,49}-[0-9]+")


def is_valid_jira_ticket_id(ticket_id: str) -> bool:
    return JIRA_TICKET_PATTERN.fullmatch(ticket_id) is not None


def jira_ticket_url(ticket_id: str, base_url: str = "") -> str | None:
    """Build a browse link for a ticket ID.

    With no explicit base_url the hot ``jira_base_url`` setting is resolved,
    so an administrator change applies without a restart.
    """
    if not is_valid_jira_ticket_id(ticket_id):
        return None
    url_to_use = base_url.strip() if base_url and base_url.strip() else get_setting("jira_base_url")
    if not url_to_use:
        return None
    try:
        parts = urlsplit(url_to_use)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            return None
        path = f"{parts.path.rstrip('/')}/{quote(ticket_id, safe='-_')}"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except ValueError:
        return None