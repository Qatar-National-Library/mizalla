# Mizalla

A server-rendered Flask and SQLite project-management app with member accounts, scoped project visibility, project scope and milestone dates, meeting records with Teams compose links, task and step tracking, key marks (Key Amendment, Key Finding, Key Decision) and milestones, a always-on Project Summary landing screen with native accordions, document uploads, separate member/server administration screens, a styled error surface (403/404/500/413), REST API endpoints, and FastMCP integration for AI agents.

## Run locally

Use Python 3.9 or newer. In PowerShell, from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\flask.exe --app mizalla create-admin
.\.venv\Scripts\flask.exe --app mizalla run --debug
```

Open `http://127.0.0.1:5000/apps/mizalla`. The database is created at `instance/mizalla.db`; attachments are stored in `instance/uploads`; and a random Flask signing key is generated once at `instance/.secret_key`. That file is ignored by source control and keeps sessions valid across restarts. Run `flask --app mizalla create-admin` in a terminal to set the first administrator's email, name, and password interactively. The command prompts for the password twice; no default password is shipped.

Set `JIRA_BASE_URL` in the process environment to override the default `https://jira.qnl.qa/` from `.env.example`. `JIRA_BASE_URL` and `PROJECT_TEAM_ROLES` are hot settings: an administrator override saved at `/apps/admin/settings` takes effect immediately, and the environment value is used only when no database override exists. See "Hot DB Settings" below.

For a deployed environment, provide a persistent, randomly generated `SECRET_KEY` through environment configuration, set `SESSION_COOKIE_SECURE=true` when HTTPS is enabled, and serve Flask behind a production WSGI server and HTTPS reverse proxy. The built-in development server is for local use only. Back up the SQLite database and upload directory together.

## Access rules

Roles are **per project** and describe a person's relationship to one project, not to the organisation. A member can hold several roles on the same project, and can be a Project Manager on one, a Developer on the next, and a Vendor on the third. A project is visible only to members holding any role on it. Task ownership, business-department responsibility, and administrator status do not independently grant project access. Creating a project adds its creator to the Project Team as its Project Manager. Project list, detail, task, step, and document routes apply the same membership check; a project outside the user's scope returns 404.

### Roles and what they grant

| Role | Grants |
| --- | --- |
| `delegated` — Delegated PM | Acts for the Project Manager on this project: same access as a PM, including assigning roles. |
| `team` — Project team | Builds the project: creates and edits tasks, steps, meetings, and documents. Adding the `Project Manager` **team role** makes this member a PM of the project. |
| `delegated_backup` — Delegated Backup | A stand-in for a Delegated PM. Deliberately the same access as a Developer, so it cannot reassign anyone's role. |
| `client` / `stakeholder` / `vendor` | Read-only: view the project, download its documents, open the Project Summary. |

The table lives in `mizalla/role_meta.py` and the role-to-capability mapping in `mizalla/access.py`, so the labels, the database CHECK constraint, and the authorisation logic cannot drift apart. When a member holds several roles on one project, every role is considered and the most permissive one wins — a Vendor who is also a team member can edit.

**Who manages a project.** A member manages a project by holding the `delegated` role on it, or by being a Project Team member who also holds the `Project Manager` team role. Administrators can always manage any project. Only those managers — and admins — can assign or remove roles on a project, over both the web form and the REST API; a PM of one project cannot staff another.

**The `is_pm` flag is no longer project authority.** It is a directory-level marker meaning "may create new projects", and it makes the flagged member the Project Manager of anything they create. It does not grant authority over projects they did not create, which is what lets a PM of one project be a Developer on another. On first start the app converts the old behaviour into explicit per-project roles: each flagged member who already held a Project Team membership gains the `Project Manager` team role on it. That preserves the authority they had without keeping the blanket grant alive. Administrators can still set the flag via the member edit form.

Project deletion requires both administrator status and edit access to the project. The portfolio supports text search across projects, tasks, and JIRA IDs, plus task-status, task-owner, and due-date filters; member choices are limited to projects already visible to the current user. Every member picker — task owners, meeting attendees, membership roles — lists only members who hold a membership on a project the current user can already see, so a user with access to one project cannot enumerate the whole member directory.

Member and server administration screens are restricted to administrators. Create additional member accounts from the Members screen; passwords must be at least 12 characters. Only administrators created with `flask --app mizalla create-admin` have administrator access. Administrators can designate members as Project Managers via the member edit form.

**Service access** is granted per member, per service, on the member edit form (`AppFeatureAccess`). The six services - Bulk Emailer, Bulk Messenger, Bulk SMS, Vision, Speech, Chat - are all granted the same way, and each gate is enforced on the server rather than by hiding links, so a bookmarked or typed URL for an ungranted service returns 403. Administrators always pass the gate so a service can be verified before rollout. `Member.can_bulk_email` is retained only as a one-time, idempotent backfill source: existing Bulk Emailer members are seeded into the new table at startup and the flag is no longer consulted for gating.

## Every screen

### Portfolio

The **Projects** page lists only projects visible to your account. Each row shows the project name, project ID, a mini progress bar for overall completion, and the first in-progress task by ascending step priority with its owners. Search by project name, project ID, task name, or JIRA ticket ID. Filter by task status, owner, and due date. Clear filters to return to the full visible portfolio.

### Project record

The project detail page (`/projects/<id>`) shows the project's definition (description, scope), schedule (estimated completion, actual start/completion), business context (owner departments), people (Project Team, Clients, Stakeholders, Vendors — each with their responsibilities and team roles), technology stack, test and production servers, and documents. A recent activity history shows who changed records and when, with actor and project name snapshots preserved so the history stays understandable if an account or project is later removed. Project Team members can create and edit tasks, steps, meetings, memberships, and documents.

### Project Summary

The **Project Summary** page (`/projects/<id>/summary`) is the clean landing screen, available to every project role — including Delegated PM and Delegated Backup — and 404 for outsiders.

- **Always-visible pulse row** — four tiles answer "is this project OK?" without opening any section: **Completion** (step-weighted percentage), **Next milestone** (name of the soonest upcoming milestone), **Needs attention** (count of blocked, overdue, or at-risk tasks), and **Next meeting** (date and agenda, or count of past meetings).
- **Nine accordions**, each collapsed under a native `<details>` heading by default (no JavaScript required): **Scope and timeline**, **Milestones** (achieved with dates, then upcoming by due date), **Key highlights** (all marks grouped by label, with dates and links to the owning task), **Current tasks** (status, % complete, in-progress step, responsible member, milestone flag, highlight badges), **Blocked, overdue and at risk**, **Meetings** (agenda, date, attendee count, external guest count, location, and a **Join in Teams** button for future meetings that have a start time), **Documents**, and **Project team and responsibilities** (with `mailto:` links).
- A **Full project details** link opens the full detail view where project-team members can edit. **Breadcrumbs** (`Home > Projects > Project name > Summary`) appear at the top, with the current page marked `aria-current="page"`.

### Tasks

The project detail and inline task-create forms show each task's type (Development, Testing, UAT, Bug Fix, New Feature, Enhancement), status (Not Started, In Progress, Blocked, Completed, Cancelled), priority, weight, optional due date, optional JIRA ticket ID, one or more owners, ordered steps, milestone flag, highlight badges, and the date a mark was recorded. **Milestones** are flagged with a separate boolean and ordered by due date; the first time a milestone task reaches Completed its achievement date is stamped automatically (an explicit date always wins and is never rewritten). **Key marks** appear as coloured badges. The **current step** is shown as the first in-progress step by ascending priority.

### Meetings and Teams

The meeting form records date, optional local time, location, agenda, attendees (pre-checked tick boxes), and notes. Ad-hoc attendees are entered as comma-separated emails. Saving a meeting with a future date and time opens a prefilled Teams `meeting/new` compose link carrying the project subject, agenda, all attendee and guest emails, and a 30-minute time range. Past meetings and meetings without a time do not open Teams. **On the Project Summary**, every future meeting with a start time shows a **Join in Teams** button in the Meetings accordion and on the pulse tile — any project member can join, even without being invited. Action tasks created from a meeting retain the meeting as their source.

### Members and servers

Administrators see the **Members** screen (name, email, designation, organization, Admin tag, Edit/Delete buttons) and the **Servers** screen (name, IP, operating system, technology/packages). New member passwords must be at least 12 characters. You cannot delete your own administrator account, and a member with project roles or task ownership must be removed from those first.

### Settings

The **Settings** screen (admin-only) at `/apps/admin/settings` manages hot settings that take effect immediately across the web UI, API handlers, MCP tools, and email jobs. Each setting shows its key, current value, a **Default** or **Modified** badge, an optional hint, and an **In effect now** notice when an environment override wins over the stored value. Per-setting **Reset to default** buttons restore the built-in default. Label-list settings (`project_team_roles`, `task_highlight_types`) are validated before writing — invalid entries are reported and nothing is written.

The screen also carries an **Azure service credentials** panel reporting only whether each service is configured - a tick or a cross per service, with any missing `.env` variable names listed in red. The values themselves are never displayed, entered, or stored.

### Service credentials

Every service credential - the Azure Vision, Speech and OpenAI keys, and the Pingram key for Bulk SMS - is read from the process environment, populated from `.env` by `_load_env_file()` in `create_app`, and from nowhere else. `azure_ai` does not import the settings table at all.

This is a deliberate security boundary rather than a stylistic one. A key typed into a web form is a key sitting in a database table, in every backup of it, and in every screenshot of the settings screen. Reading from the environment means the admin UI has no way to show one even if a future change tried to - there is no code path from a credential to a template. `RETIRED_SETTING_KEYS` and `purge_retired_settings()` delete the rows written by earlier versions on startup.

| Service | Variables |
| --- | --- |
| Vision | `MS_VISION_ENDPOINT`, `MS_VISION_KEY` |
| Speech | `MS_SPEECH_KEY`, and `MS_REGION` or a full `MS_SPEECH_ENDPOINT` (the region is derived from the host) |
| Chat | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_KEY`, `AZURE_OPENAI_DEPLOYMENT_NAME` |
| Bulk SMS | `PINGRAM_API_KEY`, `PINGRAM_REGION_CODE` |

`_ENV_NAMES` lists the conventional alternative spellings for each, so a `.env` written either way works without a code change. Adding a credential is a deployment change: edit `.env` and restart.

`PINGRAM_REGION_CODE` is required rather than optional because it selects the API host (`us` → `api.pingram.io`, `eu` → `api.eu.pingram.io`, `ca` → `api.ca.pingram.io`). Sending through the wrong region's host routes the campaign to the wrong sender pool, where the messages appear to succeed but never arrive, so an unset region is reported as unconfigured rather than silently defaulting. Bulk Emailer and Bulk Messenger are absent from the table because their credentials are not in `.env` at all - SendGrid and GOWA each bring their own configuration.

Because `create_app` loads `.env` itself, a WSGI entry point, `flask run`, and a test-built app all agree on configuration. The test suite clears these variables per test via an autouse `clean_secrets` fixture - otherwise a developer's real keys would decide whether a service renders as configured, and a test that fell through to a real HTTP call would hit the live API.

### Error pages

Every `abort(403)`, `abort(404)`, and `abort(500)` renders the styled **error.html** template with a link back to the portfolio. The 404 page deliberately names no project so a hidden record does not leak its existence.

## REST API and MCP

The **REST API** (`/apps/mizalla/api/v1`) uses `Authorization: Bearer <api_key>` or `X-API-Key: <api_key>` — CSRF-exempt, so a logged-in session cookie is not accepted. All numeric input is bounded and invalid input is rejected with 400. The MCP server (`mizalla_mcp.py`) exposes tools for every screen: `get_visible_projects`, `get_project_details`, `get_project_summary`, `get_highlight_types`, `get_milestones`, `create_project`, `update_project`, `create_task`, `update_task`, `add_task_step`, `update_task_step`, `create_meeting`, `add_project_member`, `generate_secure_document_link`, `generate_teams_meeting_link`, and `get_project_team_roles`. The summary, highlights, and milestone tools expose the same data the web UI renders, so AI agents see the full project picture.

## Completion and current work

Project completion is the weighted average of every step: each step percentage is multiplied by its parent task's weight, and each step contributes that task weight to the denominator. A project with no steps has 0% completion. The project list shows every task not marked Completed or Cancelled, its first step by ascending priority with completion from 1% through 99%, and all task owners. Tasks without an in-progress step show “No in-progress step.”

Tasks default to a Project Plan source; they may instead reference a meeting on the same project. A task may include an optional JIRA ticket ID. Existing SQLite databases are upgraded additively at application startup; existing tasks are assigned the Project Plan source by default.

Set `JIRA_BASE_URL` to your JIRA browse path (for example, `https://company.atlassian.net/browse`) to render valid ticket IDs as links. IDs are format-checked; no JIRA credentials or API synchronization are used. The JIRA base URL is read from the hot `jira_base_url` setting, so a change at `/settings` renders new links on the next page load without a restart.

## Key highlights and milestones

Every task carries an optional set of **key marks** — *Key Amendment*, *Key Finding*, *Key Decision* and any other labels an administrator maintains on the `/settings` screen. Marks are applied on the task form (task detail and inline task-create) and appear as coloured badges on the task row and on the **Project Summary** screen.

The labels come from the hot `task_highlight_types` setting. Removing a label from that setting hides it from the picker but does **not** silently drop a mark already recorded on a task: the Summary still surfaces it, so no evidence is lost mid-project.

A separate **Milestone** flag marks a scheduled checkpoint. Milestones are ordered by `due_date` and shown in an always-visible pulse row and in the Milestones accordion: achieved milestones carry a `milestone_achieved_on` date that is stamped automatically the first time a milestone task reaches *Completed* (an explicit date always wins), and upcoming milestones rank earliest-first with undated milestones last.

The Project Summary page (`/projects/<id>/summary`) is the clean landing screen: nine sections are collapsed under native `<details>` accordions, with an always-visible four-tile pulse row (completion, next milestone, needs-attention, next meeting). It shows every document, the technology stack, the full team with `mailto:` links, meeting dates and agendas, milestone and highlight history with dates and links to the owning task, and the current task list with the in-progress step. Every section is read-only except for project-team members, and a **Full project details** link opens the detail view. Breadcrumbs appear on every hierarchical screen.

The REST API and MCP server accept `highlights`, `highlighted_on`, `is_milestone` and `milestone_achieved_on`; an unconfigured highlight label is rejected with 400 rather than stored.

## Meetings and attendees

Attendees are chosen as checkboxes rather than a multi-select list. When creating a meeting every member visible on the project is pre-checked, so unticking is the only action needed to narrow the invitation. When editing a meeting only the current attendees are pre-checked, so an unrelated save never adds people the meeting did not originally include.

Ad-hoc invitees who have no member account are entered in a comma-separated field. Addresses are validated, lowercased, and de-duplicated, then stored on the meeting. They receive Teams compose links and reminder emails but gain no project access, and they are not added to the member directory. Member attendees still do not gain project access unless they also hold a project role.

The project detail page records who created, changed, or deleted project data and when. Audit entries keep actor/project name snapshots so the history remains understandable if an account or project is later removed. Meeting action tasks can be created from the meeting record and retain that meeting as their source.

## Deep integration: REST API & MCP Service

Mizalla supports deep integration for external applications, webhooks, and AI tools:

1. **REST API (`/apps/mizalla/api/v1`)**:
   - Authenticate with `Authorization: Bearer <api_key>` or `X-API-Key: <api_key>`.
   - The blueprint is CSRF-exempt, so a logged-in session cookie is deliberately **not** accepted. Use a per-member API key. Anything else returns 401.
   - All numeric input is bounded and invalid input is rejected with 400 before any write: task `priority` and `weight` must be 1–10, step `priority` 1–10000, step `percentage_complete` 0–100, and dates must be ISO `YYYY-MM-DD`. Booleans and non-numeric strings are rejected rather than coerced.
   - `GET /apps/mizalla/api/v1/projects` — List visible projects.
   - `POST /apps/mizalla/api/v1/projects` — Create project.
   - `GET /apps/mizalla/api/v1/projects/<id>` — Retrieve full project context.
   - `PATCH /apps/mizalla/api/v1/projects/<id>` — Update project details.
   - `POST /apps/mizalla/api/v1/projects/<id>/members` — Assign a member one or more project roles, with optional team roles. Requires a PM of that project (a Delegated PM role, or a team membership holding the `Project Manager` team role); a plain team member gets 403. Send `roles: ["team", "client"]` to grant several at once, or the singular `role: "team"`.
   - `POST /apps/mizalla/api/v1/projects/<id>/tasks` — Create task.
   - `PATCH /apps/mizalla/api/v1/tasks/<id>` — Update task properties.
   - `POST /apps/mizalla/api/v1/tasks/<id>/steps` — Add task step.
   - `PATCH /apps/mizalla/api/v1/steps/<id>` — Update step completion.

2. **Model Context Protocol (MCP) Server (`mizalla_mcp.py`)**:
   - Run `.\.venv\Scripts\python.exe mizalla_mcp.py`.
   - The MCP server exposes the same project data and mutations as the web UI and REST API.
   - Exposes tools: `get_visible_projects`, `get_project_details`, `get_project_summary`, `get_highlight_types`, `get_milestones`, `create_project`, `update_project`, `create_task`, `update_task`, `add_task_step`, `update_task_step`, `create_meeting`, `add_project_member`, `generate_secure_document_link`, `generate_teams_meeting_link`, and `get_project_team_roles`.
   - All mutations enforce Project Team edit permissions and log `ProjectAuditEvent` history.

## Hot DB Settings & Scheduled Email Jobs

1. **Hot Configurable Settings Table (`AppSetting` / `/settings`)**:
   - Managed exclusively by administrators via the `/settings` Web UI.
   - Hot-configurable parameters include: `jira_base_url`, `project_team_roles`, `app_base_url`, `sendgrid_from_email`, and scheduled email interval thresholds.
   - Settings resolve in the order **database → boot config (process environment / `.env`) → built-in default**. A database value is an administrator override and wins; the environment value seeds a fresh database and is used when no override exists.
   - Settings take effect immediately across web UI views, API handlers, MCP tools, and email jobs without redeploying or restarting Flask. Code reads them through `get_setting()` / `get_project_team_roles_list()`; reading `app.config` directly is what makes a setting cold.
   - Sensitive cryptographic keys (`SECRET_KEY`) and service API credentials (`SENDGRID_API_KEY`) remain securely loaded from `.env` and are never hot settings.

2. **SendGrid Timed Scheduled Email Jobs**:
   - SendGrid API v3 integration with safe local in-memory fallback log when `SENDGRID_API_KEY` is omitted.
   - Scheduled jobs include:
     - **Project Manager Status Digest**: Periodic status updates for active projects, sent only to members who manage that project — a `delegated` role, or a team membership holding the `Project Manager` team role. A project with no Project Manager is skipped rather than emailed to its wider membership. Re-sends are gated by the `email_status_report_days` setting, tracked in a `JobRunLog` row per project. The reported completion percentage is the same step-weighted figure shown on the project page, not a completed-task count.
     - **Task End Date Reminders**: Approaching deadline alerts sent to Task Owners.
     - **Stale Task Morale Nudge**: Encouraging check-ins for active tasks untouched for multiple days.
     - **Overdue Task Alert Digest**: Past-due task alerts for Task Owners and Project Managers.
     - **Upcoming Meeting Reminders**: Advance reminders with project context and Teams links, sent to member attendees and ad-hoc invitees alike.
   - **Clickable Links**: Every email generated includes working HTML links back to the specific project or task anchor. Links use the internal integer project id that the web routes actually serve, and the portfolio link points at `/`, so nothing 404s.
   - **Run Scheduled Jobs CLI**: `.\.venv\Scripts\flask.exe --app mizalla run-scheduled-jobs` (or trigger on demand via Admin Settings Web UI).

## Bulk Emailer, Bulk WhatsApp, and Bulk SMS

The **Bulk Emailer** (`/apps/emailer`) sends personalized emails via SendGrid with CSV recipient lists, template variable substitution (`{{ COLUMN_NAME}}`), HTML cleanup, PDF conversion, and per-recipient delivery tracking.

The **Bulk WhatsApp Messenger** (`/apps/whatsapp`) sends personalized WhatsApp messages via GOWA MCP with phone number lists, template variable substitution, and media attachments (image, video, audio, document).

The **Bulk SMS** service (`/apps/sms`) sends personalized SMS via Pingram. It reads a `PHONE` column, substitutes `{{ COLUMN }}` placeholders per recipient, and returns each recipient's result plus the Pingram tracking id.

All three support drag-and-drop file uploads, case-insensitive column matching, live per-recipient delivery tracking, and Excel export of results. Each is granted per member through `AppFeatureAccess` and enforces the grant server-side.

### Recipient lists

All three composers read the same shape of input from one parser, `recipient_list.py`: a header row, one column headed `EMAIL` or `PHONE`, and any further columns that become `{{ COLUMN }}` values. Comma, semicolon and tab separated files are all accepted, the delimiter being chosen by which one yields the required column in the header rather than by guessing from the data. UTF-8, UTF-8 with a byte-order mark, UTF-16 and cp1252 all decode, because Excel is inconsistent about which it writes.

CSV is also the safer format for this input than the spreadsheets these lists used to be. A phone number in a spreadsheet is a *number*: `97411111111` reads back as `97411111111.0`, and one long enough to leave a float's exact integer range is silently rounded, so a corrupted number is sent to a real person. Every value in a CSV is text, so what was typed is what is sent.

### The one-SMS limit

An SMS is billed per segment: 160 characters on GSM-7 networks, 70 on the wider encoding that Arabic, Cyrillic and emoji require. The service sends **exactly one segment per recipient** and rejects anything longer, because a longer message is silently concatenated on the handset and billed as N messages - rarely what someone composing an SMS intended.

`bulk_sms._measure()` is the single place that decides this. It returns `(encoding, units, budget)`, where `units` is not always the character count: a GSM-7 extension character (`{}`, `€`, `|`) encodes as an escape *plus* the character, so it consumes two of the 160 septets. `fits_one_sms`, `segment_count`, and `limit_message` all read from it rather than each recomputing, because when the rules were applied separately they drifted and a message could pass one check and fail another.

Two places enforce it. `send()` checks the message as submitted, and `process_job()` re-checks each message *after* substitution - a placeholder of 8 characters can become a 60-character name, so the campaign-level check cannot catch a recipient who would be billed for two segments. Those recipients are recorded as errors with the reason spelled out, never truncated.

The composer mirrors the same arithmetic in JavaScript so the meter and the `maxlength` move with the encoding as you type. That is a prompt, not the enforcement: a client-side cap is trivially bypassed, and the server is what actually decides.

## AI services

Three Azure-backed services — Vision, Speech, Chat — have **no landing page**. Each is a link in the header next to Bulk Emailer, Bulk Messenger, and Bulk SMS, and the header is the single place the gating logic lives (`service_nav.nav_services`), rendered through one Jinja macro for both the desktop and mobile bars so the two cannot drift.

A header entry is decided by two independent facts:

- **Access** — not granted, so the entry is omitted entirely rather than shown and then 403'd on click. Typing the URL directly still returns 403.
- **Credentials** — granted but unconfigured, so the entry is rendered as a dimmed, non-clickable `span` whose `title` names the exact `.env` variable to fill in.

`/apps/ai` survives only as a 302 to the first service the member can actually use, so existing bookmarks and any documentation link still land somewhere sensible.

A configured service page returns 200; an unconfigured one returns 503 naming the missing variables rather than rendering a form that could only fail.

**Vision Test Service** (`/apps/ai/vision`) — upload an image via Browse or by dropping it on the target. One `ImageAnalysisClient.analyze()` call (CAPTION + READ) produces three outputs on the page: a natural-language description, the named fields defined by the administrator's mapping, and the entire service JSON exactly as Azure returned it.

The mapping is JSON stored in the `vision_id_mapping` setting. Each value is resolved as a waterfall, per field:

1. **Response path** — read as a path into the Vision response, e.g. `read.blocks.0.lines.0.text`. `camelCase` and `snake_case` are equivalent, and a bare field name fans out across a list of objects.
2. **Label** — matched on word boundaries in the recognised text, returning whatever follows it. A label alone on its line takes the next line, which is the common card/forms layout.
3. **Whole OCR text** — the final rung, also what an empty value means.

The default is `{"ID": ""}`. Response keys are normalised before lookup, and a mapping that is not valid JSON degrades to the default with the reason shown rather than breaking the service. Each result row reports which rung fired, so a field that resolves to the wrong value is diagnosable instead of silently wrong.

Word-boundary label matching is not cosmetic: a plain substring search for `ID` matches the `ID` inside `IDENTIFICATION` and reports the rest of that word as the identifier.

**Speech Test Service** (`/apps/ai/speech`) — type up to 5,000 characters and receive an MP3 rendered in a browser `<audio>` player. The voice is picked from a fixed catalogue (`VOICE_GROUPS`) of 24 real Azure voices across eight accents — `en-US`, `en-GB`, `en-AU`, `en-IN` with two male and two female each, then `ar-EG`, `ar-SA`, `ar-MA`, `ar-QA` with one male and one female each. `normalise_voice` is an allow-list against `VOICE_INDEX`, not a regex: a name outside the catalogue is refused with a message pointing back at the picker, because a junk name otherwise surfaces only as an opaque Azure error.

The Arabic accents carry one voice per gender rather than two because Azure publishes only that pair per Arabic locale. The list is not padded with names Azure would reject, and the shortfall is stated in the code, the UI, and `test_each_arabic_accent_offers_the_one_male_and_one_female_azure_publishes`. Clips are written under `instance/speech_audio/` with a name shaped `<member id>-<uuid>.mp3`; the serving route requires that leading id to match the current user, so no member can read another's clip. The five most recent clips per member are kept and older ones pruned as new ones arrive.

**Chat Service** (`/apps/ai/chat`) — upload documents (PDF, DOCX, DOC, XLS, XLSX, PPT, PPTX, TXT, MD, CSV, RTF) via Browse or by dropping them on the target, then ask questions in plain language.

- **Extraction** (`document_text.py`) is per-format: `pypdf` per page, `python-docx` paragraphs plus tables, `openpyxl` per sheet, `python-pptx` per slide including notes, and RTF control-word stripping. Each extractor is best-effort and reports a document that yields no text — a scanned PDF with no text layer is an error, not an empty context, because an empty context reads to the model as "this document says nothing" and invites a confident wrong answer. Legacy `.doc`/`.xls` use LibreOffice when it is on PATH and otherwise scrape printable runs, flagged as approximate in the UI.
- **Chunking** is keyword-based with no embeddings, so there are no embedding calls and no extra dependency. Paragraphs are packed to `chat_chunk_chars` (floored at 400), oversized paragraphs are split at sentence boundaries rather than truncated, and each chunk records the section marker above it (`[Page 4]`, `[Sheet: Data]`) as a breadcrumb.
- **Retrieval** scores chunks by coverage of the question's terms, with term density breaking ties so a focused chunk outranks a long one that mentions everything. Selected chunks are re-sorted into document order so the model reads them as a narrative. Total context is hard-capped regardless of settings.
- **Answers** are built only from the matched chunks and must cite them by number; the system prompt tells the model to say plainly when the extracts do not cover something rather than filling the gap from its own memory. If nothing matches, the service says so without calling Azure at all. Follow-up questions carry the previous exchange forward through a bounded hidden field, so the page needs no session or JavaScript.

The document library is shared by everyone with Chat access, and duplicate uploads are detected by content hash rather than filename.

**Failures are reported as something to fix, not as a stack trace.** Each service maps upstream failures onto the setting or input at fault: an HTTP status becomes "ask an administrator to check the key in `.env`", an unrecognised voice is caught before the call, and a network failure becomes "could not be reached". The last of these matters more than it looks — `str(exc)` for a DNS failure names the internal endpoint and the transport stack, and the OpenAI SDK re-raises the real cause as `__cause__` of a generic `APIConnectionError` whose own message is only `"Connection error."`, so the whole cause chain is inspected. A member who uploads an image or asks a question should never be shown the hostname of the Azure resource or the transport library's internals.

## GOWA MCP Integration

The **Bulk WhatsApp Messenger** integrates with the GOWA MCP server for WhatsApp messaging:

- **Device Registration:** Administrators register WhatsApp devices via QR code scan at `/apps/admin` → WhatsApp Devices
- **Device Access:** Administrators assign device access to members (0 or many devices per member)
- **Message Types:** Text, image, video, audio, and document messages
- **Template Substitution:** `{{ COLUMN_NAME }}` placeholders in messages and attachments are replaced with row data from the uploaded CSV file
- **Media Handling:** Attachments are saved to `instance/uploads/` and served via temporary URLs for the GOWA server to fetch

The GOWA MCP base URL is configurable via the `gowa_mcp_base_url` hot setting (default: `http://127.0.0.1:3000`).

## Teams meeting links

When a meeting has a future date and a time, saving it opens a prefilled Teams `meeting/new` link. The link contains the project-based subject, agenda, member attendee and ad-hoc invitee email addresses, and a 30-minute time range. The meeting date/time is entered in the creator's browser timezone; the browser offset is stored so future-time detection is reliable. The Teams client may open if installed and registered to handle Teams links; otherwise the browser opens Teams. This is a compose link, not Microsoft Graph integration: the organizer reviews and sends the invitation from Teams. Past meetings and meetings without a time do not open Teams.

## Application URLs

| Path | Application |
|---|---|
| `/apps/mizalla` | Project Status Application |
| `/apps/emailer` | Bulk Emailer Application |
| `/apps/whatsapp` | Bulk WhatsApp Messenger |
| `/apps/ai` | Redirects to the first Vision/Speech/Chat service the member can use |
| `/apps/ai/vision` | Vision Test Service |
| `/apps/ai/speech` | Speech Test Service |
| `/apps/ai/chat` | Chat Service (document Q&A) |
| `/apps/admin` | Admin UI |
| `/apps/login` | Sign in (shared across all apps) |

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The tests cover progress calculation, membership-scoped project queries, direct-access isolation, CSRF enforcement, secure signed links, REST API endpoints and their input bounds, API-key-only authentication, hot setting resolution order, member-picker scoping, MCP tools, database settings management, SendGrid email dispatch, scheduled background jobs, the resolution of every URL emitted in an email against the app's route table, task highlights and milestone auto-stamping, per-project role authorization (including the Delegated PM and Delegated Backup grants, multi-role membership, PM-only role assignment, and the membership CHECK-constraint rebuild), the Project Summary screen and its accordion/breadcrumb structure, and the styled 403/404/500 error handlers.