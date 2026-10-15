# Implementation Plan: Remote Execution Server Lifecycle & Role-Based Permissions Grid

## 1. Overview
This plan implements the complete lifecycle for Registered Server Cards on `/apps/remote-exec`, user-driven connection verification with last-connected timestamps, selective server filtering for Command Items/Groups, and an Admin-only Permissions Grid for Servers, Command Items, and Command Groups.

---

## 2. Server Cards Accordion Lifecycle & Connection Rules (`/apps/remote-exec`)

### 2.1 Accordion Structure & Visual Design
- **Removal of Redundant Elements**:
  - The misleading "Live Status Sync" pill at the top of the server grid is removed.
  - The static `AVAILABLE` and `FAILED` badges are removed from server cards.
  - The standalone `RECONNECT` button is removed.
- **Accordion Header (Always Visible)**:
  - **Left**: Server Name, Host IP (`mono` badge), OS + Version badge (e.g. `🐧 Ubuntu 22.04 LTS`).
  - **Right**:
    - **`LAST_CONNECT` Indicator**:
      - If previously connected: `<span id="last-connected-{{ server.id }}"><i data-lucide="clock"></i> Connected: [Date & Time]</span>`.
      - If never connected by this user: `<span id="last-connected-{{ server.id }}" class="tag tag-unverified">UNVERIFIED</span>`.
    - **Chevron**: Disclosure arrow `<i data-lucide="chevron-down" id="server-chevron-{{ server.id }}"></i>` that rotates $180^\circ$ when open.
- **Accordion Body (Expanded on Demand)**:
  - RAM usage bar graph (`ram_used` / `ram_total`, percentage fill).
  - Storage usage bar graph (`disk_used` / `disk_total`, percentage fill).
  - Core capability tag capsules (Drupal, Python, Docker, etc.).
  - Kubernetes Pods list (if detected).
  - Administrator Notes (if provided).
  - Footer Controls:
    - `Copy Password` button: Enabled if user saved own credentials; disabled (with tooltip) if Admin fallback or no credentials.
    - `Credentials` button: Expands inline SSH credential drawer to update username/password.

### 2.2 On-Demand Verification on Expand
- Expanding an accordion automatically triggers a connection verification & telemetry refresh:
  1. The accordion body opens. Multiple accordions can remain open simultaneously.
  2. The header's `LAST_CONNECT` element is temporarily swapped with an inline spinner: `<span class="spinner"></span> Connecting...`.
  3. Client dispatches `POST /apps/remote-exec/servers/<id>/verify`.
  4. **On Success**:
     - Header's `LAST_CONNECT` updates with fresh date & time (`Connected: [Timestamp]`).
     - Live RAM, Storage, Core tags, and Pods immediately populate the accordion body.
     - Server is marked connectable for Command Item / Group target dropdowns.
     - Password copy button unlocks if permitted.
  5. **On Failure**:
     - Header's `LAST_CONNECT` displays `<span style="color:#c62828;"><i data-lucide="alert-circle"></i> Connection Failed</span>`.
     - Credentials drawer inside the body automatically expands so the user can correct credentials immediately.

### 2.3 Password Copy Endpoint Security
- Secure `GET /apps/remote-exec/servers/<id>/password`:
  - Returns password only if `user_cred` is present for `current_user`, OR if `current_user.is_admin`.
  - Returns `403 Forbidden` if a regular member attempts to copy an Admin fallback password.

---

## 3. Server Access Control (Admin Grid Display)

### 3.1 Rules
- **Only Admin** can grant **View** or **Edit** access to MEMBERs for each registered server.
- **View access**: Member can see the server card on `/apps/remote-exec`, view telemetry, and select the server in Command Items / Groups if connectable.
- **Edit access**: Member has edit rights for server configuration.
- Regular members without View access cannot see the server card.

### 3.2 Data Model Updates
- Update `ServerAccess` model:
  - `can_view = db.Column(db.Boolean, nullable=False, default=True)`
  - `can_edit = db.Column(db.Boolean, nullable=False, default=False)`
  - `last_connected_at = db.Column(db.DateTime(timezone=True), nullable=True)`
  - `connection_status = db.Column(db.String(20), nullable=False, default="unverified")`

### 3.3 Admin UI Grid Display
- Add Admin Permissions Matrix (`/apps/admin/permissions-matrix`):
  - Dedicated tab in Admin navigation with Lucide icons.
  - Tab 1: **Server Access Grid** (Servers vs Members with `[✓] View` and `[✓] Edit`).

---

## 4. Command Items & Command Groups Access Control (Admin Grid Display)

### 4.1 Rules
- **Only Admin** can grant access to **VIEW**, **EDIT**, and **ADD** Command Items and Command Groups to MEMBERs.
- **Ownership & Visibility**:
  - A MEMBER only has access to Command Items and Command Groups:
    1. Created by themselves (`created_by_id == member.id`), OR
    2. Explicitly granted access to by an Admin.
- **Permission Semantics**:
  - **VIEW access** = Can view in dashboard + **Can RUN it** + Can view logs.
  - **EDIT access** = Can view + **Can RUN it** + Can edit + **Can DELETE it** + Can duplicate.
  - **ADD access** = Can create new Command Items / Command Groups (if unchecked, `+ New Item` and `+ New Group` buttons are hidden, and create endpoints reject requests).

### 4.2 Data Model Updates
- `Member` model:
  - `can_add_command_items = db.Column(db.Boolean, nullable=False, default=False)`
  - `can_add_command_groups = db.Column(db.Boolean, nullable=False, default=False)`
- Permission helper alignment in `mizalla/remote_exec_client.py`:
  - `user_can_view_item`: Creator, Admin, or granted View/Edit.
  - `user_can_run_item`: Having VIEW access means you can also RUN it.
  - `user_can_edit_item`: Creator, Admin, or granted Edit.
  - `user_can_delete_item`: Having EDIT access means you can RUN/DELETE it.
  - Same rules for `CommandGroup`.

### 4.3 Admin UI Grid Display
- In the Permissions Matrix view (`/apps/admin/permissions-matrix`):
  - **Member Capabilities**: Grid toggling `[✓] Add Items` and `[✓] Add Groups`.
  - **Command Items Grid**: Items vs Members with `[✓] View (Run)` and `[✓] Edit (Run/Delete)`.
  - **Command Groups Grid**: Groups vs Members with `[✓] View (Run)` and `[✓] Edit (Run/Delete)`.
  - Clean AJAX save / Form submission with instant feedback.

---

## 5. Selective Target Server Filtering

### 5.1 Dropdown Filtering
- A server will only appear in the `Target Server` dropdown when creating a **Command Item** or **Command Group** if:
  1. The member has **View access** to the server (`ServerAccess.can_view == True`), AND
  2. The member has **connected to the server at least once** (`ServerAccess.last_connected_at is not None` and `connection_status == 'available'`).
- Servers marked `UNVERIFIED` are omitted from the dropdown until the user connects via Reconnect.

### 5.2 Server Creation/Edit Validation
- Backend route validation in `remote_exec.py`:
  - `item_save` and `group_create` / `group_edit` verify that the submitted `server_id` is connectable by `current_user`. If not, the request is rejected with a descriptive error.
  - Dual runtime check on command execution verifying both item/group permission and server connectability.

---

## 6. Implementation Sequence
1. **Model Updates**: Enhance `ServerAccess` and `Member` in `mizalla/models.py`.
2. **Permission Helpers**: Update `mizalla/remote_exec_client.py` for two-tier View=Run and Edit=Run/Delete semantics.
3. **Backend Routes**:
   - `POST /apps/remote-exec/servers/<id>/verify` in `mizalla/remote_exec.py`.
   - Security check on `GET /apps/remote-exec/servers/<id>/password`.
   - Connectable servers filtering for dashboard, item modal, and group forms.
   - Admin permissions grid routes in `mizalla/admin.py`.
4. **UI Templates**:
   - Update `mizalla/templates/remote_exec_dashboard.html`: Transform registered server cards into interactive accordions (Header: Name, Host IP, OS+version, Last Connected / UNVERIFIED badge, Chevron; Body: RAM/Storage gauges, capability tags, Pods list, Copy Password, Credentials button with inline drawer; On-expand auto-verification with spinner; Remove Live Status Sync placeholder; Filtered target server select).
   - Update `mizalla/templates/command_group_form.html`: Filtered target server select.
   - Create `mizalla/templates/admin_permissions_grid.html` and add tab to `admin_nav` macro in `mizalla/templates/macros.html`.
5. **Testing**:
   - Write comprehensive tests in `tests/test_remote_exec.py` and `tests/test_server_management.py`.
   - Run full pytest suite to verify 100% pass rate.
