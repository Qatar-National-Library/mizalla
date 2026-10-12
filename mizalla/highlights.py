"""Shared highlight and milestone rules for the web routes, REST API, and MCP tools.

Highlights are free-text labels drawn from the admin-editable ``task_highlight_types``
setting. Milestones are a separate boolean flag rather than a fourth label, because a
milestone is a scheduled checkpoint: it orders by ``due_date`` and must not be broken by
an administrator renaming a highlight label.

Every helper raises ``ValueError`` with a caller-facing message. Web routes flash it;
the REST API and MCP tools turn it into an error response.
"""

from datetime import date

from .models import Task

MAX_HIGHLIGHT_LABELS = 8
MAX_HIGHLIGHT_LENGTH = 80


def normalise_highlights(raw_labels, allowed_labels: list[str]) -> list[str]:
    """Return the requested highlights, filtered to the configured labels.

    Labels are matched case-insensitively but returned using the administrator's own
    spelling and in the order the setting lists them, so the summary screen renders a
    stable order regardless of the order the labels were ticked.

    Unknown labels are rejected rather than silently dropped: a typo in the API or a
    stale form should be reported, not written to the database.
    """
    if raw_labels is None:
        return []
    if isinstance(raw_labels, str):
        raw_labels = [part.strip() for part in raw_labels.split(",")]

    lookup = {label.casefold(): label for label in allowed_labels}
    chosen: dict[str, str] = {}
    for item in raw_labels:
        if not isinstance(item, str):
            raise ValueError("Each highlight must be text.")
        label = item.strip()
        if not label:
            continue
        if len(label) > MAX_HIGHLIGHT_LENGTH:
            raise ValueError(f"Highlight '{label[:40]}...' is too long.")
        match = lookup.get(label.casefold())
        if match is None:
            raise ValueError(f"'{label}' is not a configured highlight type.")
        chosen[match.casefold()] = match

    if len(chosen) > MAX_HIGHLIGHT_LABELS:
        raise ValueError(f"A task can carry at most {MAX_HIGHLIGHT_LABELS} highlights.")
    return [lookup[key] for key in lookup if key in chosen]


def apply_highlights(task: Task, labels: list[str], marked_on: date | None, today: date | None = None) -> None:
    """Write the highlight set and its date onto a task.

    The date defaults to today the first time any highlight is applied, so the summary
    screen can say when a finding was recorded without the author having to remember.
    Clearing every highlight clears the date too, so a task never keeps a stale date
    with no highlight attached to it.
    """
    task.set_highlight_list(labels)
    if not labels:
        task.highlighted_on = None
        return
    task.highlighted_on = marked_on or today or date.today()


def apply_milestone(task: Task, is_milestone: bool, status: str, achieved_on: date | None, today: date | None = None) -> None:
    """Write the milestone flag and its achieved date onto a task.

    Precedence is explicit date, then auto-stamp, then leave alone. An explicit
    ``achieved_on`` always wins so a user can correct the day after the fact. Failing
    that the date is stamped the first time the milestone lands as Completed and then
    left untouched, so re-saving an unrelated field never rewrites history. Un-flagging
    clears the date, because a task that is not a milestone cannot have been achieved
    as one.
    """
    task.is_milestone = bool(is_milestone)
    if not task.is_milestone:
        task.milestone_achieved_on = None
        return
    if achieved_on is not None:
        task.milestone_achieved_on = achieved_on
    elif status == "Completed" and task.milestone_achieved_on is None:
        task.milestone_achieved_on = today or date.today()


def milestones_for(tasks) -> tuple[list[Task], list[Task], list[Task]]:
    """Split a project's tasks into milestones.

    Returns ``(achieved, upcoming, active)``:
      * ``achieved``  - milestones delivered, most recently achieved first.
      * ``upcoming``  - milestones still open, soonest due date first, undated last.
      * ``active``    - everything not yet achieved, for the "next milestone" readout.

    A milestone counts as achieved when it is Completed or when all of its steps read
    100%, so a task that was ticked off step by step still surfaces as delivered.
    """
    milestones = [task for task in tasks if task.is_milestone]
    achieved = sorted(
        (task for task in milestones if task.is_achieved),
        key=lambda task: (task.milestone_achieved_on or date.min, task.due_date or date.min),
        reverse=True,
    )
    open_milestones = [task for task in milestones if not task.is_achieved]
    upcoming = sorted(open_milestones, key=lambda task: (task.due_date is None, task.due_date or date.min, task.priority, task.id))
    return achieved, upcoming, open_milestones


def at_risk_tasks(tasks) -> list[Task]:
    """Active tasks that are blocked, overdue, or past their due date."""
    return sorted(
        (task for task in tasks if task.is_at_risk),
        key=lambda task: (task.due_date or date.max, task.priority, task.id),
    )
