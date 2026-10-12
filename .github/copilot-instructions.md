# Majmaa Instructions

- Follow the existing Flask app factory and SQLAlchemy model structure.
- Preserve strict project visibility: only project team, client, stakeholder, and vendor memberships grant access. Task ownership and admin status do not bypass project checks.
- Only Project Team members may edit project data. Project deletion additionally requires admin status and Project Team membership; other project roles are read-only.
- Apply `get_visible_project` to project-scoped routes and validate related IDs against the current project.
- Keep project list filters and member selection scoped to the signed-in user's visible projects. Never render the full member directory to a user who can only see one project.
- Record successful project mutations in the audit history. Meeting action tasks must reference a meeting on the same project.
- Validate JIRA ticket IDs and use only the hot `jira_base_url` setting to build safe browse links.
- Build future Teams meeting links with the Teams `meeting/new` deep-link URL; include agenda, member attendee and ad-hoc invitee emails, browser-local start/end, and the 30-minute duration. Do not add Microsoft Graph/Entra auth; Teams sends invitations only after organizer confirmation.
- Validate meeting references against the current project; meeting attendees do not receive project access unless separately assigned a project role.
- Meeting attendees are a pre-checked checkbox group — all scoped members when creating, only current attendees when editing. Account-less invitees go in the comma-separated ad-hoc field and are stored on `Meeting.external_attendees`; do not create member rows for them.
- Keep CSRF protection enabled for state-changing requests and store uploads under the configured upload directory using generated storage names.
- Preserve the task-weighted average of individual step percentages and ascending step-priority ordering for current steps. Use `project_completion()` anywhere a completion percentage is shown or emailed.
- Preserve additive SQLite schema upgrades; new tasks default to a Project Plan source, with optional same-project meeting source and JIRA ticket ID.
- Authenticate the `/api/v1` blueprint with an API key only. It is CSRF-exempt, so a logged-in session must not be accepted as API auth.
- Validate API input with `majmaa.validators`: task `priority`/`weight` 1–10, step `priority` 1–10000, `percentage_complete` 0–100, ISO dates only. Return 400 on invalid input instead of writing an unbounded value.
- Read hot settings through `get_setting()` / `get_project_team_roles_list()` (database → boot config → default) rather than `app.config`, so `/settings` changes apply without a restart.
- Email links must use the integer `project.id` in `/projects/<id>` and `/` for the portfolio; the human project code does not route.
- Do not add default passwords, demo accounts, or secrets to source control.
- Keep admin password setup interactive through the `create-admin` CLI command; local signing keys belong only in the ignored `instance/` directory.
- Run tests with `.\.venv\Scripts\python.exe -m pytest -q`.
- Task key marks (`majmaa.highlights`): Labels are maintained in the hot `task_highlight_types` setting and applied per-task as a CSV in `Task.highlights` via `get_highlight_list()` / `set_highlight_list()`. `normalise_highlights()` sorts by setting order and raises on any unknown label; enforce this in both web routes and the REST API.
- Task milestones (`Task.is_milestone`, `Task.milestone_achieved_on`): Separate from key marks so they survive a label rename and stay ordered by `due_date`. `apply_milestone()` stamps `milestone_achieved_on` once on the first Completed save and never rewrites it; `milestones_for()` returns (achieved, upcoming-by-due-date, open) with undated last.
- The Project Summary screen (`/projects/<id>/summary`) is readable by every project role (team, client, stakeholder, vendor) and returns 404 for outsiders. It renders nine native `<details>` accordions collapsed by default plus an always-visible four-tile pulse row (completion, next milestone, needs-attention, next meeting) and a **Full project details** link. `majmaa.highlights` drives its badge and milestone rendering.
- Styled 403/404/500 error handlers are registered in `majmaa.__init__` alongside the existing 413 handler so `abort(403)`/`abort(404)` from `access.py` always render `error.html` (with a link back to the portfolio). The 404 message names no project so a hidden record does not leak its existence.

## Setup Progress

- [x] Clarify project requirements
- [x] Scaffold the Flask project
- [x] Implement project, member, and server workflows
- [x] Add tests for access control, progress, and CSRF
- [x] Document local setup and launch commands