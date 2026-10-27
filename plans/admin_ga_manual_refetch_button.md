# Plan: Admin UI Manual GA Properties Refetch Button & Elimination of Auto/Timer Sync

## Goal
Add a dedicated, prominent "Refetch Properties from GA" button on the Admin UI so administrators can manually fetch/synchronize Google Analytics 4 properties on-demand. Ensure properties are strictly fetched on click and **never** automatically refetched on page refresh, page visits, or via background timers/polling.

---

## Current Architecture & State
1. **Database-backed Cache**:
   - Properties are stored in the `GoogleAnalyticsProperty` table (`mizalla/models.py`).
   - `GET /apps/admin/analytics-properties` currently queries local database records.
   - `GET /apps/analytics` queries user-accessible properties from the database.
2. **Current Sync Endpoint**:
   - `POST /apps/admin/analytics-properties/sync` calls `google_analytics.discover_properties()`.
3. **Current Missing Elements / Improvements Needed**:
   - **Admin Navigation Integration**: `analytics_properties` is missing from `admin_nav` macro in `mizalla/templates/macros.html` (the primary tab-bar for Admin UI). Admins navigating through Admin (Settings, Members, Servers, Permissions, Audit, Feedback) cannot switch to GA Properties seamlessly.
   - **Template & Button Prominence**: On `analytics_properties.html`, `admin_nav('analytics_properties')` is not included. The action button should be explicitly labeled "Refetch Properties from GA" (with a clean Lucide refresh icon), and display the latest synchronization timestamp and an explicit note that properties are cached and only updated on demand.
   - **Zero Auto-Fetch Verification**: Verify and guarantee there are no `setInterval`, `setTimeout`, or automatic sync triggers on page visits, neither in Admin UI nor in user reporting.
   - **Automated Regression Tests**: Add unit/integration tests confirming that:
     - `GET /apps/admin/analytics-properties` does **not** invoke `discover_properties()`.
     - `GET /apps/analytics` does **not** invoke `discover_properties()`.
     - `POST /apps/admin/analytics-properties/sync` **does** invoke `discover_properties()`.

---

## Detailed Implementation Tasks

### Task 1: Update Admin Navigation & Header Integration
- **File**: `mizalla/templates/macros.html`
  - In `admin_nav(active_page)`, add a tab for GA Properties:
    ```html
    <a href="{{ url_for('admin.analytics_properties') }}" class="tab{% if active_page == 'analytics_properties' %} active{% endif %}"{% if active_page == 'analytics_properties' %} aria-current="page" aria-selected="true"{% else %} aria-selected="false"{% endif %}><i data-lucide="bar-chart-3" class="icon-green"></i> GA Properties &amp; Permissions</a>
    ```
- **File**: `mizalla/templates/analytics_properties.html`
  - Include `admin_nav('analytics_properties')` at the top of `content` block to maintain consistent Admin UI structure.
  - Refine page header to prominently feature the **"Refetch Properties from GA"** button with Lucide icon (`<i data-lucide="refresh-cw" class="icon-white"></i>`).
  - Add a "Last Refetched" status badge/timestamp showing when the properties cache was last updated from Google Analytics.
  - Add an informational note badge: "On-demand sync: GA properties are cached locally and only refetched when this button is clicked (no auto-refresh or timer polling)."

### Task 2: Enhance Admin Sync Route & Feedback
- **File**: `mizalla/admin.py`
  - In `analytics_properties_sync()`, ensure clean handling of new vs updated properties, updating `last_synced_at`.
  - Provide clear flash messaging (e.g. `"Refetched {count} properties from Google Analytics ({new_count} new, {updated_count} updated)."`).
  - Ensure `GET /apps/admin/analytics-properties` only queries the database and performs zero external network calls.

### Task 3: Regression & Isolation Tests
- **File**: `tests/test_analytics_service.py`
  - Add `test_admin_properties_page_does_not_call_discover_properties`:
    - Spy/mock `google_analytics.discover_properties`.
    - Perform `client.get("/apps/admin/analytics-properties")`.
    - Assert `call_count == 0`.
  - Add `test_analytics_dashboard_does_not_call_discover_properties`:
    - Perform `client.get("/apps/analytics")`.
    - Assert `call_count == 0`.
  - Add `test_admin_properties_refetch_button_triggers_sync`:
    - Perform `client.post("/apps/admin/analytics-properties/sync")`.
    - Assert `call_count == 1`.
  - Run all pytest test suites: `.\venv\Scripts\python.exe -m pytest tests/test_analytics_service.py`.

---

## Verification Plan
1. **Automated Tests**:
   - Run `.\venv\Scripts\python.exe -m pytest tests/test_analytics_service.py` and ensure 100% pass rate.
   - Run full test suite `.\venv\Scripts\python.exe -m pytest` to prevent any regressions across the codebase.
2. **Code & UI Review**:
   - Verify Lucide icon standards (`data-lucide` with green on light, white on dark).
   - Verify absence of any `setInterval`, `setTimeout`, or auto-refresh headers in `analytics_properties.html`.
