from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

TEAMS_NEW_MEETING_URL = "https://teams.microsoft.com/l/meeting/new?"
MEETING_DURATION_MINUTES = 30


def _meeting_start_utc(meeting):
    if meeting.meeting_start_at is None or meeting.meeting_utc_offset_minutes is None:
        return None
    return (meeting.meeting_start_at + timedelta(minutes=meeting.meeting_utc_offset_minutes)).replace(tzinfo=timezone.utc)


def is_future_teams_meeting(meeting) -> bool:
    start_utc = _meeting_start_utc(meeting)
    return start_utc is not None and start_utc > datetime.now(timezone.utc)


def meeting_recipient_emails(meeting) -> list[str]:
    """All invitee addresses: member attendees plus ad-hoc external addresses."""
    emails = [attendee.email for attendee in meeting.attendees if attendee.email]
    return list(dict.fromkeys(emails + list(meeting.get_external_attendee_list())))


def create_teams_meeting_url(project, meeting) -> str | None:
    start_utc = _meeting_start_utc(meeting)
    if start_utc is None or start_utc <= datetime.now(timezone.utc):
        return None

    end_local = meeting.meeting_start_at + timedelta(minutes=MEETING_DURATION_MINUTES)
    params = {
        "subject": f"{project.name} meeting",
        "content": meeting.agenda,
        "startTime": meeting.meeting_start_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "endTime": end_local.strftime("%Y-%m-%dT%H:%M:%S"),
        "attendees": ",".join(meeting_recipient_emails(meeting)),
    }
    return TEAMS_NEW_MEETING_URL + urlencode(params)