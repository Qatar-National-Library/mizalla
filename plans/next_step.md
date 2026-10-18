# Implementation Plan: Command Group UI Revamp

## 1. Overview & Objectives
This plan outlines the UI/UX revamp for the Remote Command Orchestration dashboard (`/apps/remote-exec/`) and the unified Command Group creation and edit interface (`/apps/remote-exec/groups/new` and `/apps/remote-exec/groups/<id>/edit`).

### Key Goals
1. **Streamline Dashboard**: Remove the two top-bar buttons (`+ NEW COMMAND ITEM` and `+ NEW COMMAND GROUP`).
2. **Full-Width Command Groups on Dashboard**: Remove the Command Items panel from `/apps/remote-exec/`, allowing the **Command Groups** panel to expand to full container width.
3. **Unified Group Edit Workflow**: Each Command Group card displays an **Edit** button that opens the unified Command Group builder with pre-populated values and attached Command Items.
4. **70/30 Split Command Group Builder (`/groups/new` & `/groups/<id>/edit`)**:
   - **Left Column (70%)**:
     - *Command Group Form*: Group Name, Target Server dropdown, Description, Execution options (Sequential single-session vs. Parallel, Stop on error).
     - *Group Execution Items List* (rendered beneath the form): Displays all items in the group with dynamic sequence numbers (`#1`, `#2`, ...). Supports **Drag & Drop** to reorder execution sequence and an **`x` button** to remove items.
   - **Right Column (30%)**:
     - *Available Command Items List*: Displays all Command Items the user created or has access to.
     - *Accordion Item Format*: Each item displays its name and small muted subtitle `Created by: <Creator Name>`.
     - *Actions*:
       - `+` Button: Appends the Command Item to the Command Group list in the left column.
       - Expand Button (Expand icon): Toggles the accordion body to view the full script/content and details.

---

## 2. Technical Architecture & Component Design

### 2.1 Dashboard Updates (`mizalla/templates/remote_exec_dashboard.html`)
- **Top Header Action Area**: Remove the `+ New Command Item` button and `+ New Command Group` button from lines 19–26.
- **Section 2 Grid Layout**:
  - Remove the `<section>` containing the "Command Items" list.
  - Convert the `display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));` layout into a single full-width container (`width: 100%`) for Command Groups.
  - On the Command Groups panel header, ensure the `+ New Group` button remains accessible for users with `can_add_group` capability.
  - Each Command Group card retains its **Run**, **Duplicate**, **Edit**, and **Delete** buttons.
  - Clicking **Edit** navigates to `url_for('remote_exec.group_edit', group_id=grp.id)`.

### 2.2 Unified Command Group Builder (`mizalla/templates/command_group_form.html`)
- **Grid Layout**: 
  - CSS layout with 70% left column and 30% right column:
    ```css
    .group-builder-grid {
      display: grid;
      grid-template-columns: 7fr 3fr;
      gap: 1.5rem;
      align-items: start;
    }
    @media (max-width: 992px) {
      .group-builder-grid {
        grid-template-columns: 1fr;
      }
    }
    ```
- **Left Column (70%)**:
  1. **Command Group Form Fields**:
     - Group Name (required text input)
     - Target Server (required `<select>` of connectable servers)
     - Description (optional `<textarea>`)
     - Options: Run in Parallel (`run_in_parallel`), Stop on Error (`stop_on_error`)
  2. **Command Items in this Group (Under the form)**:
     - Header: "Command Items in this Group" with count badge and a "Clear All" button.
     - Item cards render in order of execution with:
       - Drag handle (`<i data-lucide="grip-vertical"></i>`) enabling HTML5 drag-and-drop reordering.
       - Dynamic badge: `#1`, `#2`, `#3`... (auto-updated when items are added, removed, or dragged).
       - Item Name and Command Type badge (`On Remote Server`, `On Local Server`, `Upload File(s)`).
       - Remove button (`<button class="remove-item-btn"><i data-lucide="x"></i></button>`).
       - Hidden input `<input type="hidden" name="item_ids[]" value="<item_id>">` maintaining the exact submitted sequence.
     - Empty placeholder state: "No items added yet. Click '+' on any Command Item in the right column to add it to this group."
  3. **Submit / Cancel Actions**:
     - `Save Command Group` button and `Cancel` link.

- **Right Column (30%)**:
  1. **Header**: "Available Command Items" with count badge.
  2. **Accordion Item Cards**:
     - Header row with:
       - Item Name and type badge.
       - Subtitle in small muted text: `Created by: {{ item.creator.name if item.creator else 'Unknown' }}`.
       - Action buttons:
         - `+` Button: `<button type="button" class="button button-small button-primary add-to-group-btn" title="Add to group" onclick="appendItemToGroup({{ item.id }}, '{{ item.name|escape }}', '{{ type_label }}')"><i data-lucide="plus" class="icon-white"></i></button>`
         - Expand Button: `<button type="button" class="button button-quiet button-small" onclick="toggleItemScriptAccordion({{ item.id }})" title="Expand script"><i data-lucide="chevron-down" id="item-chevron-{{ item.id }}" class="icon-green"></i></button>`
     - Accordion Body:
       - Collapsible container (`display: none` by default).
       - Pre-formatted script / uploaded files view with syntax highlighting styling.
       - Variable count and variable list if present.

---

## 3. Backend Routes & Controllers (`mizalla/remote_exec.py`)

### 3.1 `group_create()` (`/apps/remote-exec/groups/new`)
- **GET**:
  - Pass `available_items`: all Command Items the current user has permission to view (`[i for i in CommandItem.query.order_by(CommandItem.name).all() if user_can_view_item(current_user, i)]`).
  - Pass `servers`: list of connectable servers.
  - Pass `group=None`.
- **POST**:
  - Retrieve form inputs: `name`, `server_id`, `description`, `run_in_parallel`, `stop_on_error`.
  - Retrieve submitted items: `item_ids = request.form.getlist("item_ids[]") or request.form.getlist("item_ids")`.
  - Create the `CommandGroup`.
  - For each `item_id` in `item_ids`, insert a `CommandGroupItem` with `sequence_order = idx + 1`.
  - Commit and redirect to `/apps/remote-exec/`.

### 3.2 `group_edit()` (`/apps/remote-exec/groups/<int:group_id>/edit`)
- **GET**:
  - Pass `group`, `servers`, and `available_items`.
- **POST**:
  - Update `group.name`, `group.description`, `group.run_in_parallel`, `group.stop_on_error`.
  - If `item_ids` are submitted in the form, sync group items to match the submitted list and order:
    - Clear existing `CommandGroupItem` entries for this group.
    - Add new entries with the exact updated sequence.
  - Commit and redirect to `/apps/remote-exec/`.

---

## 4. Frontend Drag-and-Drop & Dynamic List Logic

### 4.1 HTML5 Drag-and-Drop Implementation
- Make each added group item draggable (`draggable="true"`).
- Event handlers:
  - `ondragstart`: store index/dragged element ID and apply dragging visual cue (`opacity: 0.5`).
  - `ondragover`: prevent default and apply insertion indicator.
  - `ondrop`: swap or insert dragged element before/after target element in the DOM.
  - `ondragend`: remove drag styles and invoke `updateGroupSequenceBadges()`.

### 4.2 Dynamic Synchronization
- `appendItemToGroup(itemId, itemName, itemType)`:
  - Appends new item card HTML into the `#group-items-container`.
  - Appends `<input type="hidden" name="item_ids[]" value="${itemId}">`.
  - Triggers `updateGroupSequenceBadges()` to update `#1`, `#2`... labels.
  - Calls `lucide.createIcons()`.
- `removeGroupItem(element)`:
  - Removes the item card from `#group-items-container`.
  - Triggers `updateGroupSequenceBadges()`.
- `updateGroupSequenceBadges()`:
  - Iterates through all child cards in `#group-items-container`.
  - Updates the badge text `#${idx + 1}`.
  - Toggles the empty placeholder message visibility.

---

## 5. Implementation Steps & Status

- [x] **Step 1: Dashboard Refactor (`remote_exec_dashboard.html`)**
  - Removed top `+ New Command Item` and `+ New Command Group` header buttons.
  - Removed the Command Items `<section>`.
  - Expanded Command Groups panel to full page width (`width: 100%`).
  - Added "+ New Command Group" action button directly in the Command Groups panel header.
  - Retained Edit button linking to `/apps/remote-exec/groups/<id>/edit`.

- [x] **Step 2: Backend Route Enhancements (`mizalla/remote_exec.py`)**
  - Updated `group_create`:
    - On GET: Fetches viewable Command Items (`available_items`) and passes them to `command_group_form.html`.
    - On POST: Reads `item_ids[]` array from form and creates initial `CommandGroupItem` records with proper sequence numbers (`position = 1..N`).
    - Redirects to `/apps/remote-exec/`.
  - Updated `group_edit`:
    - On POST: Synchronizes `CommandGroupItem` sequence with submitted `item_ids[]` array, deleting removed associations and adding/updating existing ones in matching order.
    - Redirects to `/apps/remote-exec/`.

- [x] **Step 3: Redesign Command Group Form (`command_group_form.html`)**
  - Implemented responsive 70/30 grid layout (`.group-builder-grid`).
  - **Left column (70%)**:
    - Group details form (Name, Target Server, Description, Parallel, Stop on error).
    - Draggable Command Items sequence container with drag handles (`grip-vertical`), sequence badges (`#1`, `#2`...), hidden `item_ids[]` inputs, and `x` removal buttons.
    - Dynamic empty state placeholder when no items are selected.
  - **Right column (30%)**:
    - Available Command Items browser.
    - Each item displays its name and `Created by: <Creator Name>` in muted font.
    - `+` button appends item to group in left column.
    - Expand toggle button with chevron/expand icon opens accordion body showing full script text and inferred variables.
  - **JavaScript interactions**:
    - Native HTML5 drag-and-drop reordering with dragover visual feedback.
    - `appendItemToGroup`: Appends item card, syncs badge sequence, and invokes `lucide.createIcons()`.
    - `removeGroupItem`: Removes item card and updates sequence numbering.
    - `toggleItemScriptAccordion`: Toggles collapsed/expanded state of script view.

- [x] **Step 5: Command Item Creation & In-Place Editing Modals on Command Group New/Edit Pages**
  - **New Command Item Trigger**: Added "+ New Command Item" button (`btn-new-command-item`) in the Available Command Items panel header of `command_group_form.html`, controlled by user capability `can_add_item`.
  - **Modal Forms Integration**: Included `<dialog id="item-modal">` and `<dialog id="variables-modal">` directly within `command_group_form.html` to support adding new items or editing existing items without navigating away from in-progress group editing.
  - **Edit Button on Command Items**:
    - Right column available items cards display a pencil Edit button (`btn-edit-avail-item`) if the logged-in user is the item creator, an Admin, or has explicit `can_edit` permission granted by the Admin.
    - Left column included items cards also display the pencil Edit button when editable.
  - **Asynchronous In-Place Save**:
    - `save_item` route in `mizalla/remote_exec.py` enhanced to handle `ajax=1` requests, returning JSON metadata (`id`, `name`, `type_label`, `command_content`, `creator_name`, `can_edit`, `variables`).
    - Editing an item updates its title, type label, script preview, and variables in the Available Items panel and synchronizes all referencing cards in the included group list without page reload.
    - Creating an item dynamically prepends the new item card into the Available Items browser, increments the count badge, and binds interactive handlers.
  - **Testing**: Added `test_command_group_item_modal_and_edit_permissions` in `tests/test_remote_exec.py` verifying modal triggers, creator/RBAC edit button permissions, and AJAX creation/updating. Passed 29/29 tests in `test_remote_exec.py` and 28/28 in `test_server_management.py`.


