# Plan: Admin Account Management, Action Buttons Replication & Self-Account Security

## Context & Objectives
1. **Re-enable Account Deletion**:
   - Administrators should be able to permanently delete member accounts, gated with a confirmation dialog (`confirm(...)`).
   - Self-deletion must be forbidden (an administrator cannot delete their own account).
2. **Replicate Actions on User Details Page (`member_form.html`)**:
   - Replicate the **Lock / Unlock** and **Delete** action buttons on the user details page.
3. **Logged-in Account Security & Self Password Change**:
   - When viewing the account of the logged-in administrator (`member.id == current_user.id`):
     - In the Account Directory (`members.html`): disable `Edit`, `Lock`, and `Delete` buttons.
     - Provide a **Change Password** action for the logged-in administrator.
     - In the User Details page (`member_form.html`): disable editing and saving member fields, disable `Lock` and `Delete` buttons, and display a dedicated **Change Password** form (current password, new password of min 12 characters, confirmation, with Lucide eye toggles).
     - For other users (`member.id != current_user.id`), administrators cannot view or edit their passwords, but can edit metadata, lock/unlock, and delete accounts.
4. **Forgot Password Workflow**:
   - Confirm and detail the end-to-end self-service password reset workflow on the login page.

---

## Proposed Changes

### 1. Backend Endpoints (`mizalla/admin.py`)
- **`member_delete(member_id)`**:
  - Check `member.id != current_user.id` (self-deletion forbidden).
  - Delete `member` from database (cleanly cascades feature access, credentials, audits, and server access).
  - Flash success message: `Member account '{name}' has been deleted.`
  - Redirect to `admin.member_list`.
- **`member_edit(member_id)`**:
  - Prevent modifying roles/admin flags if `member.id == current_user.id`.
  - Handle password update when submitted by the logged-in user on their own account:
    - Verify `current_password` via `member.check_password(...)`.
    - Validate `new_password` length >= 12 and `new_password == confirm_password`.
    - Update password hash, commit, and flash success.
- **Safety Guards**:
  - `member_lock`: Keep check ensuring `member.id != current_user.id`.
  - `member_delete`: Enforce check ensuring `member.id != current_user.id`.

### 2. Account Directory Template (`mizalla/templates/members.html`)
- For rows where `member.id != current_user.id`:
  - Show active `Edit` link.
  - Show active `Lock` / `Unlock` button (with confirmation).
  - Show active `Delete` button (with confirmation: *"Are you sure you want to permanently delete the account for [Name]? This action cannot be undone."*).
- For row where `member.id == current_user.id` (logged-in user):
  - Disable `Edit` button (`disabled` attribute, muted styling, tooltip: *"You cannot edit your own account permissions from the directory."*).
  - Disable `Lock` button (`disabled` attribute, muted styling, tooltip: *"You cannot lock your own account."*).
  - Disable `Delete` button (`disabled` attribute, muted styling, tooltip: *"You cannot delete your own account."*).
  - Render an active `Change Password` button linking to the user details page anchor or password change form.

### 3. User Details Template (`mizalla/templates/member_form.html`)
- Add an action header banner containing replicated actions:
  - If `member.id != current_user.id`:
    - **Lock / Unlock** button (POST to `admin.member_lock` / `admin.member_unlock` with confirmation).
    - **Delete** button (POST to `admin.member_delete` with confirmation).
  - If `member.id == current_user.id`:
    - Disabled **Lock** button.
    - Disabled **Delete** button.
    - Disabled **Save member** button on the metadata form.
    - Render a **Change Your Password** panel with:
      - Current Password input with in-field eye toggle.
      - New Password input (min. 12 characters) with in-field eye toggle.
      - Confirm New Password input with in-field eye toggle.
      - "Update Password" submission button.

---

## Verification & Testing
1. **Automated Unit & Integration Tests (`tests/test_admin_account_management.py`)**:
   - Admin can delete other member accounts; database cascades cleanly.
   - Admin cannot delete own account.
   - Admin cannot lock own account.
   - Directory listing has disabled buttons for own account and active buttons for others.
   - Logged-in admin can change their password on own account.
   - Admin cannot change passwords of other users.
2. **Regression Testing**:
   - Run `tests/test_deletion_cascades.py`.
   - Run `tests/test_admin_role_management.py`.
   - Run `tests/test_password_reset_and_lockout.py`.
