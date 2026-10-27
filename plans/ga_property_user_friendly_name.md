# Plan: User-Friendly Names for GA Properties & Technical ID Masking for Non-Admins

## Problem Statement
Google Analytics properties fetched from GA or registered manually have technical display names (e.g., `qnl-mobile`), internal account names (e.g., `Mobile Application`), and numeric IDs (e.g., `301981519`). 
Admins need to record a clean, **User-Friendly Name** (e.g., "Qatar National Library Mobile App" or "Digital Library Portal") on the Admin UI. Regular users should see this friendly name across all Google Analytics user-facing pages, and should not be exposed to the internal GA property IDs or technical account names. Only Administrators should see the underlying actual GA name and Property ID.

---

## Architectural Changes

### 1. Database Model & SQLite Schema Migration
- **`mizalla/models.py` (`GoogleAnalyticsProperty`)**:
  - Add column:
    ```python
    user_friendly_name = db.Column(db.String(200), nullable=False, default="")
    ```
  - Add helper property:
    ```python
    @property
    def friendly_label(self) -> str:
        return self.user_friendly_name.strip() if self.user_friendly_name and self.user_friendly_name.strip() else self.display_name
    ```
- **`mizalla/schema.py` (`SQLITE_ADDITIONS`)**:
  - Add entry:
    ```python
    "google_analytics_property": {
        "user_friendly_name": "VARCHAR(200) NOT NULL DEFAULT ''",
    },
    ```
  - Ensures existing databases are automatically migrated on app startup with zero manual SQL.

### 2. Admin UI & Property Management
- **`mizalla/admin.py`**:
  - Update `analytics_property_update_access` (or `analytics_property_update`):
    - Accept `user_friendly_name` from request form and update `prop.user_friendly_name = request.form.get("user_friendly_name", "").strip()`.
  - Update `analytics_property_add_manual`:
    - Accept optional `user_friendly_name` from form.
  - In `analytics_properties_sync`:
    - When discovering and updating existing properties, **preserve** `existing.user_friendly_name` (do NOT overwrite it with raw GA `display_name`).
- **`mizalla/templates/analytics_properties.html`**:
  - In the Discovered Properties table:
    - Display the **User-Friendly Name** as the primary highlighted title (or indicating `Not set (using GA default)` if empty).
    - Display the technical GA Name (`display_name`), Account (`account_name`), and Property ID (`property_id`) with clear admin badges.
  - In the modal dialog (`Manage Property & Access`):
    - Add a form field:
      ```html
      <label for="user_friendly_name">User-Friendly Name (shown to regular users)</label>
      <input type="text" name="user_friendly_name" value="{{ prop.user_friendly_name }}" placeholder="e.g. Qatar National Library Mobile App">
      ```
  - In the manual property addition drawer:
    - Add field for optional `user_friendly_name`.

### 3. User-Facing Analytics Dashboard
- **`mizalla/templates/analytics.html`**:
  - In the property selector `<select id="property-select">`:
    - For non-admins:
      ```jinja2
      <option value="{{ p.property_id }}">
        {{ p.friendly_label }}
      </option>
      ```
      (No numeric Property ID, no technical account names).
    - For admins:
      ```jinja2
      <option value="{{ p.property_id }}">
        {{ p.friendly_label }} (ID: {{ p.property_id }}){% if p.account_name %} — {{ p.account_name }}{% endif %}
      </option>
      ```
  - In the report cards & headings:
    - Display `selected_property.friendly_label`.
    - Mask or hide technical IDs from regular non-admin viewers.

### 4. Automated Tests
- **`tests/test_analytics_service.py`**:
  - Test Admin can set and update `user_friendly_name` for a property.
  - Test that `discover_properties()` sync preserves the configured `user_friendly_name`.
  - Test that regular user dashboard response includes `friendly_label` and masks raw GA Property ID and internal account name.
  - Test that Admin still sees the underlying technical IDs.

---

## Verification Plan
1. Run `.\venv\Scripts\python.exe -m pytest tests/test_analytics_service.py`.
2. Run full pytest suite `.\venv\Scripts\python.exe -m pytest`.
3. Verify SQLite column addition via `mizalla/schema.py`.
