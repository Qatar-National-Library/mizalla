# Google Analytics (GA4) Property-Level Access & Executive Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Google Analytics 4 (GA4) auto-discovery of properties via Service Account, granular Admin UI property-level view-only access management, and an Executive Reporting dashboard with property dropdown selector, date range filtering, executive KPI cards, and an interactive Grid.js table.

**Architecture:** 
1. **Google Analytics Integration (`mizalla/google_analytics.py`)**: Add `discover_properties()` using Google Analytics Admin API v1beta (`https://analyticsadmin.googleapis.com/v1beta/accountSummaries`) using the existing OAuth 2.0 JWT service account credentials. Enhance `run_executive_report()` to query GA4 Data API for executive metrics and page breakdown.
2. **Database Models (`mizalla/models.py`)**: Create `GoogleAnalyticsProperty` and association table `ga_property_access` linking properties to authorized `Member` accounts with view-only permissions.
3. **Admin Management (`mizalla/admin.py`, templates)**: Dedicated Admin screen to trigger auto-discovery sync from Google, view all properties, and assign authorized members to each property (or grant to all members).
4. **Analytics View Dashboard (`mizalla/analytics.py`, `mizalla/templates/analytics.html`)**: Filter properties accessible to the logged-in user, provide a property selector dropdown, date range controls, executive metric cards (Page Views, New Sessions, Min/Max Page Views/Session, Returning Users, Bounce Rate, Avg/Min/Max Session Length, Top 5 Countries), and a searchable, sortable, paginated (25/page) Grid.js table of all pages by view count.

**Tech Stack:** Python 3.14 / Flask, SQLAlchemy, Google Analytics Admin & Data APIs (v1beta REST via JWT), HTML5/CSS, Lucide Icons, Grid.js (via CDN/UMD), Pytest.

---

## Global Constraints
- Strictly adhere to Universal Icon Standard: Lucide icons (`data-lucide`) using green `var(--green-dark, #103e30)` on light backgrounds and white `#ffffff` on dark backgrounds.
- View-only access: Regular users cannot modify properties, refresh service account keys, or alter access rights.
- Maintain existing tests: `python -m pytest tests/test_analytics_service.py` and new tests must pass 100%.
- Virtual environment: Use project `venv` (`.\venv\Scripts\python.exe`).

## Review Focus
1. **Empty / No Access state**: If a user is granted `FEATURE_ANALYTICS` but has no property assignments yet, show a friendly empty state prompting them to contact an administrator.
2. **Service Account without Admin API access**: If auto-discovery encounters permission issues (e.g. Service Account only has property-level access rather than Account-level), provide graceful error reporting and an option for Admins to manually register a Property ID.
3. **Date range validation**: Support preset date ranges (7 days, 30 days, 90 days) and valid custom date range (`YYYY-MM-DD`), preventing invalid start/end dates.
4. **Grid.js rendering & styling**: Ensure Grid.js renders smoothly, is fully responsive, respects dark/green headers, and paginates at 25 pages per view.
5. **Zero data / New properties**: If GA4 returns 0 rows for a date range, KPI cards display 0 and Grid.js displays "No page views recorded in this date range" without crashing.

---

### Task 1: Data Models for GA Properties & Access Control

**Files:**
- Modify: `mizalla/models.py`
- Test: `tests/test_analytics_service.py`

- [x] **Step 1: Write tests for `GoogleAnalyticsProperty` and access control relationships**
  - Add test creating a `GoogleAnalyticsProperty` with `property_id`, `display_name`, `account_name`.
  - Add test granting view access for a property to a member via association.
  - Verify member lookup returns only authorized properties (and admin has access to all).
- [x] **Step 2: Implement models in `mizalla/models.py`**
  - Define `ga_property_access` association table (`property_id` FK, `member_id` FK, `granted_at`, `granted_by_id`).
  - Define `GoogleAnalyticsProperty` class:
    - `id`: Integer PK
    - `property_id`: String(64) unique, indexed (e.g., `properties/12345678` or `12345678`)
    - `display_name`: String(200), nullable=False
    - `account_name`: String(200), default=""
    - `is_active`: Boolean, default=True
    - `all_members`: Boolean, default=False (allows granting to all users with analytics feature)
    - `created_at`: DateTime
    - `last_synced_at`: DateTime
    - `authorized_members`: relationship to `Member` via `ga_property_access`
  - Add helper method `can_access_property(member, property_obj)` and classmethod `accessible_properties_for(member)`.
- [x] **Step 3: Run pytest to verify model tests pass**
  - Run: `.\venv\Scripts\python.exe -m pytest tests/test_analytics_service.py`

---

### Task 2: GA Auto-Discovery & Executive Reporting Client Helpers

**Files:**
- Modify: `mizalla/google_analytics.py`
- Test: `tests/test_analytics_service.py`

- [x] **Step 1: Write unit tests with mocked Google API responses**
  - Mock `urllib.request.urlopen` for `https://analyticsadmin.googleapis.com/v1beta/accountSummaries`.
  - Verify `discover_properties()` extracts property IDs, display names, and account names.
  - Mock `run_report` for executive metrics query (page views, sessions, bounce rate, etc.) and top pages.
- [x] **Step 2: Implement `discover_properties()` in `mizalla/google_analytics.py`**
  - Query `https://analyticsadmin.googleapis.com/v1beta/accountSummaries` using Service Account OAuth token.
  - Parse `accountSummaries[].propertySummaries[]` into a structured list of dicts: `[{"property_id": "...", "display_name": "...", "account_name": "..."}]`.
  - Handle HTTP 403 / 404 gracefully with descriptive messages if Admin API is not enabled in Google Cloud Console.
- [x] **Step 3: Implement `fetch_executive_analytics()` in `mizalla/google_analytics.py`**
  - Support user-selected `date_range` (`start_date`, `end_date`).
  - Query 1: Executive KPI Metrics:
    - Metrics: `screenPageViews`, `sessions`, `newUsers`, `activeUsers`, `bounceRate`, `averageSessionDuration`.
    - Daily distribution to compute Min / Max Page Views per session and Min / Max session length.
  - Query 2: Top 5 Countries:
    - Dimension: `country`, Metric: `activeUsers`, limit 5, order by `activeUsers` desc.
  - Query 3: All Pages Table Data:
    - Dimensions: `pagePath`, `pageTitle`, Metrics: `screenPageViews`, `activeUsers`, `bounceRate`.
  - Format cleanly for consumption by templates and Grid.js JSON serialization.
- [x] **Step 4: Run pytest to verify helpers and parsing pass**
  - Run: `.\venv\Scripts\python.exe -m pytest tests/test_analytics_service.py`

---

### Task 3: Admin UI for GA Property Access Management

**Files:**
- Modify: `mizalla/admin.py`
- Create / Modify: `mizalla/templates/admin/analytics_properties.html`
- Modify: `mizalla/templates/admin/index.html` (or settings / admin navigation)
- Test: `tests/test_analytics_service.py`

- [x] **Step 1: Write tests for Admin property management**
  - Test Admin endpoint `/admin/analytics-properties/` requires Admin authentication.
  - Test Sync action calls `discover_properties()` and updates `GoogleAnalyticsProperty` records.
  - Test Assigning specific members to a property updates the permissions correctly.
  - Test Manual property addition fallback.
- [x] **Step 2: Implement Admin routes in `mizalla/admin.py`**
  - `@bp.route("/analytics-properties")`: Lists all known properties with authorized member counts and status.
  - `@bp.route("/analytics-properties/sync", methods=["POST"])`: Invokes auto-discovery, creates new properties, updates existing ones, and flashes status.
  - `@bp.route("/analytics-properties/<int:prop_id>/access", methods=["POST"])`: Updates member access list or `all_members` flag.
  - `@bp.route("/analytics-properties/add", methods=["POST"])`: Allows manual entry of Property ID and Name.
- [x] **Step 3: Create `analytics_properties.html` template**
  - Clean layout with Lucide icons (`refresh-cw`, `shield`, `users`, `plus`, `check`).
  - Table of properties with badges for active/inactive, member assignment chips, and "Manage Access" modal.
- [x] **Step 4: Run pytest to verify Admin routes pass**
  - Run: `.\venv\Scripts\python.exe -m pytest tests/test_analytics_service.py`

---

### Task 4: Analytics View-Only Dashboard with Dropdown, Cards & Grid.js

**Files:**
- Modify: `mizalla/analytics.py`
- Modify: `mizalla/templates/analytics.html`
- Test: `tests/test_analytics_service.py`

- [x] **Step 1: Write integration tests for Analytics page property selector and report**
  - User with access to Property A can select it and view report.
  - User cannot view Property B if not authorized (returns 403 or falls back to first authorized property).
  - Date range filtering passes parameters to query.
- [x] **Step 2: Update `mizalla/analytics.py`**
  - Retrieve list of properties accessible to `current_user`:
    - If `current_user.is_admin`, all properties.
    - Otherwise, properties where `current_user.id in property.authorized_members` or `all_members=True`.
  - Accept `property_id`, `date_preset` (7d, 30d, 90d, custom), `start_date`, and `end_date`.
  - Fetch executive analytics data for selected property.
  - Pass structured KPI metrics, top countries, and page table data to the template.
- [x] **Step 3: Modernize `mizalla/templates/analytics.html`**
  - Top Control Bar:
    - Property dropdown selector with Lucide icon (`<i data-lucide="bar-chart-3"></i>`).
    - Date range dropdown / date pickers with Apply button.
  - Executive Reporting Cards Section:
    - Card 1: **Page Views** (Total Views, with visual indicator)
    - Card 2: **New Sessions & Returning Users** (Breakdown of new vs returning visitors)
    - Card 3: **Page Views per Session** (Average, along with Minimum & Maximum per session)
    - Card 4: **Bounce Rate** (Formatted percentage)
    - Card 5: **Session Duration** (Average, Minimum, Maximum session length in readable MM:SS format)
    - Card 6: **Top 5 Countries by Users** (Progress list with user counts and percentage share)
  - Interactive Pages Grid Section (Grid.js):
    - Embed Grid.js (`gridjs.umd.js` and `mermaid.min.css`) styled with Mizalla dark-green theme.
    - Columns: Page Path / URL, Page Title, Page Views, Unique Visitors, Bounce Rate.
    - Configured with `search: true`, `sort: true`, `pagination: { limit: 25 }`.
    - Client-side initialization script with fallback handling for zero records.
- [x] **Step 4: Run full test suite to verify end-to-end functionality**
  - Run: `.\venv\Scripts\python.exe -m pytest tests/test_analytics_service.py`

---

### Task 5: Verification & End-to-End Regression Testing

**Files:**
- Test: Full project test suite `tests/`

- [x] **Step 1: Run complete test suite**
  - Run: `.\venv\Scripts\python.exe -m pytest`
  - Verify all existing and new tests pass without error or regression.
- [x] **Step 2: Verify UI and visual appearance**
  - Verify Lucide icons render correctly on all buttons, headers, and cards.
  - Verify responsive behavior on both desktop and mobile viewports.
