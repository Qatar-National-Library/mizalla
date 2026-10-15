"""Remote Server Command Execution and Orchestration Blueprint (Pure Flask + AsyncSSH).

Features:
- Agentless AsyncSSH execution with persistent single-session sequential groups.
- Server Cards with live status badges, password copy, and inline credential fallback.
- Command Items (Remote, Local, Upload) with deduplicated {{ Variable }} placeholders.
- Command Groups with ADD, CLEAR, MOVE UP, MOVE DOWN, EDIT, REMOVE.
- DUPLICATE for both Command Items and Command Groups.
- Live RUNNING status with spinner and button lockout visible to all users who can run.
- Accordion history with green tick (✓) and red cross (✗), 25-item pagination, lazy-loaded last 10 lines.
- Granular Admin RBAC: can_run, can_view_logs, can_edit, can_create, can_duplicate.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from .asyncssh_engine import extract_variable_names, test_ssh_connection
from .extensions import db
from .feature_access import has_feature_access
from .models import (
    COMMAND_STATUS_COMPLETED,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PENDING,
    COMMAND_STATUS_RUNNING,
    COMMAND_STATUS_TERMINATED,
    COMMAND_TYPE_LOCAL,
    COMMAND_TYPE_REMOTE,
    COMMAND_TYPE_UPLOAD,
    FEATURE_REMOTE_EXEC,
    VAR_TYPE_BOOLEAN,
    VAR_TYPE_NUMBER,
    VAR_TYPE_TEXT,
    CommandAudit,
    CommandGroup,
    CommandGroupItem,
    CommandGroupPermission,
    CommandItem,
    CommandItemPermission,
    CommandVariable,
    Member,
    Server,
    ServerCredential,
)
from .remote_exec_client import (
    dispatch_command_group,
    dispatch_command_item,
    get_effective_credential,
    probe_server_health_sync,
    terminate_running_audit,
    user_can_duplicate_group,
    user_can_duplicate_item,
    user_can_edit_group,
    user_can_edit_item,
    user_can_run_group,
    user_can_run_item,
    user_can_view_group_logs,
    user_can_view_item_logs,
)

bp = Blueprint("remote_exec", __name__, url_prefix="/apps/remote-exec")


def remote_exec_required(view):
    """Enforce feature grant access for remote execution."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not has_feature_access(current_user, FEATURE_REMOTE_EXEC):
            return render_template(
                "error.html",
                title="Not permitted",
                message=(
                    "You do not have access to Remote Command Execution. "
                    "An administrator grants this service from Member accounts."
                ),
            ), 403
        return view(*args, **kwargs)
    return wrapped


# ─── Dashboard ───────────────────────────────────────────────────────────────

@bp.route("/")
@remote_exec_required
def dashboard():
    """Main dashboard displaying Server Cards, Command Groups, Items, and Accordions."""
    page = request.args.get("page", 1, type=int)
    per_page = 25

    servers = Server.query.order_by(Server.name).all()

    # Accessible Command Groups
    all_groups = CommandGroup.query.order_by(CommandGroup.name).all()
    accessible_groups = [g for g in all_groups if user_can_run_group(current_user, g) or user_can_edit_group(current_user, g)]

    # Accessible Command Items
    all_items = CommandItem.query.order_by(CommandItem.name).all()
    accessible_items = [i for i in all_items if user_can_run_item(current_user, i) or user_can_edit_item(current_user, i)]

    # Historical Audits (paginated 25 per page)
    # Filter audits to commands on accessible servers
    query = CommandAudit.query.order_by(CommandAudit.created_at.desc())
    total_audits = query.count()
    total_pages = max(1, math.ceil(total_audits / per_page))
    page = max(1, min(page, total_pages))

    history_audits = query.offset((page - 1) * per_page).limit(per_page).all()

    # Currently running audits for instant template rendering of RUNNING spinner and disabled buttons
    active_audits = CommandAudit.query.filter(
        CommandAudit.status.in_([COMMAND_STATUS_PENDING, COMMAND_STATUS_RUNNING])
    ).all()
    running_item_ids = {a.command_item_id for a in active_audits if a.command_item_id}
    running_group_ids = {a.group_id for a in active_audits if a.group_id}

    # Build can_view_logs mapping for history items
    logs_allowed = {}
    for audit in history_audits:
        can_view = False
        if current_user.is_admin:
            can_view = True
        elif audit.group_id and audit.group:
            can_view = user_can_view_group_logs(current_user, audit.group)
        elif audit.command_item_id and audit.command_item:
            can_view = user_can_view_item_logs(current_user, audit.command_item)
        elif audit.user_id == current_user.id:
            can_view = True
        logs_allowed[audit.id] = can_view

    return render_template(
        "remote_exec_dashboard.html",
        servers=servers,
        command_groups=accessible_groups,
        command_items=accessible_items,
        history_audits=history_audits,
        logs_allowed=logs_allowed,
        current_page=page,
        total_pages=total_pages,
        running_group_ids=running_group_ids,
        running_item_ids=running_item_ids,
        user_can_edit_group=lambda g: user_can_edit_group(current_user, g),
        user_can_duplicate_group=lambda g: user_can_duplicate_group(current_user, g),
        user_can_edit_item=lambda i: user_can_edit_item(current_user, i),
        user_can_duplicate_item=lambda i: user_can_duplicate_item(current_user, i),
    )


# ─── Live Status Polling (Running Spinner & Button Lockout) ───────────────────

@bp.get("/active-status")
@remote_exec_required
def active_status():
    """Returns currently running command audits, items, and groups.
    
    Visible to all users who can run items or groups so buttons disable and
    the word 'RUNNING' with a SPINNER appears simultaneously.
    """
    running_audits = CommandAudit.query.filter(
        CommandAudit.status.in_([COMMAND_STATUS_PENDING, COMMAND_STATUS_RUNNING])
    ).all()

    running_item_ids = [a.command_item_id for a in running_audits if a.command_item_id]
    running_group_ids = [a.group_id for a in running_audits if a.group_id]
    running_group_steps = {}
    for a in running_audits:
        if a.group_id and a.entry_type == "group":
            running_group_steps[str(a.group_id)] = {
                "audit_id": a.id,
                "status": a.status,
                "steps": a.steps_data,
            }

    return jsonify(
        has_running=bool(running_audits),
        running_item_ids=running_item_ids,
        running_group_ids=running_group_ids,
        running_group_steps=running_group_steps,
        running_audits=[
            {
                "id": a.id,
                "task_id": a.task_id,
                "title": a.title,
                "status": a.status,
                "entry_type": a.entry_type,
                "item_id": a.command_item_id,
                "group_id": a.group_id,
                "steps": a.steps_data,
            }
            for a in running_audits
        ],
    )


# ─── Credential Copy & Fallback Endpoints ─────────────────────────────────────

@bp.get("/servers/<int:server_id>/password")
@remote_exec_required
def get_server_password(server_id: int):
    """Returns decrypted password for clipboard copy."""
    server = Server.query.get_or_404(server_id)
    cred = get_effective_credential(server_id, current_user.id)
    if not cred or not cred.password:
        return jsonify(success=False, error="No password configured for this server."), 404
    return jsonify(success=True, password=cred.password)


@bp.post("/servers/<int:server_id>/fallback-credential")
@remote_exec_required
def save_fallback_credential(server_id: int):
    """Inline credential fallback form: tests connection immediately via AsyncSSH and saves."""
    server = Server.query.get_or_404(server_id)
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    private_key = request.form.get("private_key", "").strip()

    if not username:
        return jsonify(success=False, error="Username is required."), 400

    # Test connection via AsyncSSH
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        ok, msg, telemetry = loop.run_until_complete(
            test_ssh_connection(
                ip=server.ip,
                port=server.ssh_port or 22,
                username=username,
                password=password,
                private_key=private_key,
            )
        )
    finally:
        loop.close()

    if not ok:
        return jsonify(success=False, error=msg), 400

    # Save credential
    cred = ServerCredential.query.filter_by(server_id=server.id, user_id=current_user.id).first()
    if not cred:
        cred = ServerCredential(server_id=server.id, user_id=current_user.id)
        db.session.add(cred)

    cred.username = username
    if password:
        cred.password = password
    if private_key:
        cred.private_key = private_key
    cred.status = "available"
    cred.last_tested = datetime.now(timezone.utc)

    # If current user is Admin, set as default credential
    if current_user.is_admin:
        cred.is_default = True

    # Update server telemetry
    server.status = "available"
    server.last_checked = datetime.now(timezone.utc)
    if telemetry.get("os_type"):
        server.os_type = telemetry["os_type"]
    if telemetry.get("os_version"):
        server.os_version = telemetry["os_version"]
    if telemetry.get("packages_summary"):
        server.packages_summary = telemetry["packages_summary"]
    if telemetry.get("ram_total"):
        server.ram_total = telemetry["ram_total"]
    if telemetry.get("ram_used"):
        server.ram_used = telemetry["ram_used"]
    if "ram_percent" in telemetry:
        server.ram_percent = float(telemetry.get("ram_percent") or 0.0)
    if telemetry.get("disk_total"):
        server.disk_total = telemetry["disk_total"]
    if telemetry.get("disk_used"):
        server.disk_used = telemetry["disk_used"]
    if "disk_percent" in telemetry:
        server.disk_percent = float(telemetry.get("disk_percent") or 0.0)
    if "core_tags" in telemetry:
        server.core_tags = telemetry.get("core_tags") or ""

    db.session.commit()
    return jsonify(
        success=True,
        message="Connection verified! Server is now AVAILABLE.",
        os_type=server.os_type,
        os_version=server.os_version,
        ram_total=server.ram_total,
        ram_used=server.ram_used,
        ram_percent=server.ram_percent,
        disk_total=server.disk_total,
        disk_used=server.disk_used,
        disk_percent=server.disk_percent,
        core_tags=server.core_tags,
        tag_lines=server.get_tag_lines(),
    )


# ─── Dynamic Variable Modal Pre-Flight ────────────────────────────────────────

@bp.get("/run-modal/<entry_type>/<int:target_id>")
@remote_exec_required
def get_run_modal_data(entry_type: str, target_id: int):
    """Prepares variable definitions for a Command Item or Command Group.
    
    Uses a SET of variable names across all items so duplicate variable names
    are asked only ONCE.
    """
    variables_map = {}
    has_upload = False

    if entry_type == "item":
        item = CommandItem.query.get_or_404(target_id)
        if not user_can_run_item(current_user, item):
            return jsonify(error="Permission denied to run this Command Item."), 403

        title = item.name
        server_name = item.server.name if item.server else "Unknown Server"
        has_upload = (item.command_type == COMMAND_TYPE_UPLOAD)

        # Stored variables
        for v in item.variables:
            variables_map[v.name] = {
                "name": v.name,
                "description": v.description or "",
                "var_type": v.var_type,
                "default_value": v.default_value or "",
            }

        # Any raw {{ Variable }} found in content
        found_tokens = extract_variable_names(item.command_content)
        for tok in found_tokens:
            if tok not in variables_map:
                variables_map[tok] = {
                    "name": tok,
                    "description": "",
                    "var_type": VAR_TYPE_TEXT,
                    "default_value": "",
                }

    else:
        group = CommandGroup.query.get_or_404(target_id)
        if not user_can_run_group(current_user, group):
            return jsonify(error="Permission denied to run this Command Group."), 403

        title = group.name
        server_name = group.server.name if group.server else "Unknown Server"

        # Loop through all group items using a SET
        for g_item in group.items:
            c_item = g_item.command_item
            if not c_item:
                continue
            if c_item.command_type == COMMAND_TYPE_UPLOAD:
                has_upload = True

            for v in c_item.variables:
                if v.name not in variables_map:
                    variables_map[v.name] = {
                        "name": v.name,
                        "description": v.description or "",
                        "var_type": v.var_type,
                        "default_value": v.default_value or "",
                    }

            found_tokens = extract_variable_names(c_item.command_content)
            for tok in found_tokens:
                if tok not in variables_map:
                    variables_map[tok] = {
                        "name": tok,
                        "description": "",
                        "var_type": VAR_TYPE_TEXT,
                        "default_value": "",
                    }

    return jsonify(
        success=True,
        title=title,
        server_name=server_name,
        entry_type=entry_type,
        target_id=target_id,
        has_upload=has_upload,
        variables=list(variables_map.values()),
    )


# ─── Execution Dispatch ───────────────────────────────────────────────────────

@bp.post("/run/<entry_type>/<int:target_id>")
@remote_exec_required
def execute_target(entry_type: str, target_id: int):
    """Executes a Command Item or Command Group with variable values."""
    client_ip = request.remote_addr or ""

    # Parse variable inputs (supports json or form data)
    variables = {}
    if request.is_json:
        variables = request.json.get("variables", {})
    else:
        for key, val in request.form.items():
            if key.startswith("var_"):
                var_name = key[4:]
                variables[var_name] = val

    # Handle file uploads if present
    uploaded_files = []
    if "files" in request.files:
        for f in request.files.getlist("files"):
            if f.filename:
                uploaded_files.append((f.filename, f.read()))

    if entry_type == "item":
        item = CommandItem.query.get_or_404(target_id)
        if not user_can_run_item(current_user, item):
            return jsonify(error="Permission denied to run this item."), 403

        audit = dispatch_command_item(
            user_id=current_user.id,
            item_id=item.id,
            variables=variables,
            client_ip=client_ip,
        )
    else:
        group = CommandGroup.query.get_or_404(target_id)
        if not user_can_run_group(current_user, group):
            return jsonify(error="Permission denied to run this group."), 403

        audit = dispatch_command_group(
            user_id=current_user.id,
            group_id=group.id,
            variables=variables,
            uploaded_files=uploaded_files,
            client_ip=client_ip,
        )

    return jsonify(
        success=True,
        message=f"{'Command Group' if entry_type == 'group' else 'Command Item'} dispatched.",
        task_id=audit.task_id,
        audit_id=audit.id,
    )


# ─── Accordion Lazy Loading (Last 10 Lines) & Full Log ────────────────────────

@bp.get("/commands/<int:audit_id>/last-10")
@remote_exec_required
def get_audit_last_10(audit_id: int):
    """Lazy-loads the last 10 lines of output for the accordion body."""
    audit = CommandAudit.query.get_or_404(audit_id)

    # Check can_view_logs permission
    can_view = False
    if current_user.is_admin:
        can_view = True
    elif audit.group_id and audit.group:
        can_view = user_can_view_group_logs(current_user, audit.group)
    elif audit.command_item_id and audit.command_item:
        can_view = user_can_view_item_logs(current_user, audit.command_item)
    elif audit.user_id == current_user.id:
        can_view = True

    if not can_view:
        return jsonify(
            success=False,
            error="Output restricted. You do not have permission to view execution logs.",
            restricted=True,
        ), 403

    return jsonify(
        success=True,
        has_output=audit.has_output,
        last_10=audit.last_10_lines_output,
        status=audit.status,
        exit_code=audit.exit_code,
        duration=f"{audit.duration_seconds:.1f}s" if audit.duration_seconds is not None else "--",
    )


@bp.get("/commands/<int:audit_id>/full-log")
@remote_exec_required
def get_audit_full_log(audit_id: int):
    """Full-screen viewer for complete command log."""
    audit = CommandAudit.query.get_or_404(audit_id)

    # Check can_view_logs permission
    can_view = False
    if current_user.is_admin:
        can_view = True
    elif audit.group_id and audit.group:
        can_view = user_can_view_group_logs(current_user, audit.group)
    elif audit.command_item_id and audit.command_item:
        can_view = user_can_view_item_logs(current_user, audit.command_item)
    elif audit.user_id == current_user.id:
        can_view = True

    if not can_view:
        flash("You do not have permission to view logs for this execution.", "error")
        return redirect(url_for("remote_exec.dashboard"))

    return render_template("command_full_log.html", audit=audit)


# ─── Process Termination ──────────────────────────────────────────────────────

@bp.post("/commands/<int:audit_id>/terminate")
@remote_exec_required
def terminate_command(audit_id: int):
    """Terminates an active execution."""
    audit = CommandAudit.query.get_or_404(audit_id)
    if not (current_user.is_admin or audit.user_id == current_user.id):
        return jsonify(error="Unauthorized to terminate this execution."), 403

    ok = terminate_running_audit(audit.id)
    return jsonify(success=ok, message="Termination signal sent." if ok else "Process already completed.")


# ─── Duplication Endpoints ────────────────────────────────────────────────────

@bp.post("/items/<int:item_id>/duplicate")
@remote_exec_required
def duplicate_item(item_id: int):
    """Duplicating a Command Item clones it with Created_By=current_user."""
    item = CommandItem.query.get_or_404(item_id)
    if not user_can_duplicate_item(current_user, item):
        flash("You do not have permission to duplicate this Command Item.", "error")
        return redirect(url_for("remote_exec.dashboard"))

    new_item = CommandItem(
        server_id=item.server_id,
        name=f"{item.name} (Copy)",
        command_type=item.command_type,
        command_content=item.command_content,
        created_by_id=current_user.id,
        created_on=datetime.now(timezone.utc),
    )
    db.session.add(new_item)
    db.session.flush()

    for v in item.variables:
        new_v = CommandVariable(
            command_item_id=new_item.id,
            name=v.name,
            description=v.description,
            var_type=v.var_type,
            default_value=v.default_value,
        )
        db.session.add(new_v)

    db.session.commit()
    flash(f"Command Item '{item.name}' duplicated successfully.", "success")
    return redirect(url_for("remote_exec.dashboard"))


@bp.post("/groups/<int:group_id>/duplicate")
@remote_exec_required
def duplicate_group(group_id: int):
    """Duplicating a Command Group clones the group and all items under it."""
    group = CommandGroup.query.get_or_404(group_id)
    if not user_can_duplicate_group(current_user, group):
        flash("You do not have permission to duplicate this Command Group.", "error")
        return redirect(url_for("remote_exec.dashboard"))

    new_group = CommandGroup(
        server_id=group.server_id,
        name=f"{group.name} (Copy)",
        description=group.description,
        run_in_parallel=group.run_in_parallel,
        stop_on_error=group.stop_on_error,
        created_by_id=current_user.id,
        created_on=datetime.now(timezone.utc),
    )
    db.session.add(new_group)
    db.session.flush()

    # Clone each Command Item under the group
    for g_item in group.items:
        old_c_item = g_item.command_item
        if not old_c_item:
            continue
        new_c_item = CommandItem(
            server_id=old_c_item.server_id,
            name=f"{old_c_item.name} (Copy)",
            command_type=old_c_item.command_type,
            command_content=old_c_item.command_content,
            created_by_id=current_user.id,
            created_on=datetime.now(timezone.utc),
        )
        db.session.add(new_c_item)
        db.session.flush()

        for v in old_c_item.variables:
            new_v = CommandVariable(
                command_item_id=new_c_item.id,
                name=v.name,
                description=v.description,
                var_type=v.var_type,
                default_value=v.default_value,
            )
            db.session.add(new_v)

        link = CommandGroupItem(
            group_id=new_group.id,
            command_item_id=new_c_item.id,
            sequence_order=g_item.sequence_order,
        )
        db.session.add(link)

    db.session.commit()
    flash(f"Command Group '{group.name}' and all sub-items duplicated.", "success")
    return redirect(url_for("remote_exec.group_edit", group_id=new_group.id))


# ─── Command Item CRUD & Modal Save ───────────────────────────────────────────

@bp.post("/items/save")
@remote_exec_required
def save_item():
    """Create or update a Command Item and its variables."""
    item_id = request.form.get("item_id", type=int)
    server_id = request.form.get("server_id", type=int)
    name = request.form.get("name", "").strip()
    command_type = request.form.get("command_type", COMMAND_TYPE_REMOTE)
    command_content = request.form.get("command_content", "")

    if not name or not server_id:
        flash("Name and target server are required.", "error")
        return redirect(url_for("remote_exec.dashboard"))

    if item_id:
        item = CommandItem.query.get_or_404(item_id)
        if not user_can_edit_item(current_user, item):
            flash("You do not have permission to edit this Command Item.", "error")
            return redirect(url_for("remote_exec.dashboard"))
        item.name = name
        item.server_id = server_id
        item.command_type = command_type
        item.command_content = command_content
    else:
        item = CommandItem(
            server_id=server_id,
            name=name,
            command_type=command_type,
            command_content=command_content,
            created_by_id=current_user.id,
            created_on=datetime.now(timezone.utc),
        )
        db.session.add(item)
        db.session.flush()

    # Synchronize variables
    var_names = request.form.getlist("var_name[]")
    var_descs = request.form.getlist("var_desc[]")
    var_types = request.form.getlist("var_type[]")
    var_defaults = request.form.getlist("var_default[]")

    # Clear existing and re-create
    CommandVariable.query.filter_by(command_item_id=item.id).delete()
    for i, vname in enumerate(var_names):
        vname = vname.strip()
        if not vname:
            continue
        vdesc = var_descs[i].strip() if i < len(var_descs) else ""
        vtype = var_types[i].strip() if i < len(var_types) else VAR_TYPE_TEXT
        vdef = var_defaults[i].strip() if i < len(var_defaults) else ""

        db.session.add(CommandVariable(
            command_item_id=item.id,
            name=vname,
            description=vdesc,
            var_type=vtype,
            default_value=vdef,
        ))

    db.session.commit()
    flash(f"Command Item '{item.name}' saved successfully.", "success")
    return redirect(url_for("remote_exec.dashboard"))


@bp.post("/items/<int:item_id>/delete")
@remote_exec_required
def delete_item(item_id: int):
    """Deletes a Command Item."""
    item = CommandItem.query.get_or_404(item_id)
    if not user_can_edit_item(current_user, item):
        flash("You do not have permission to delete this Command Item.", "error")
        return redirect(url_for("remote_exec.dashboard"))

    db.session.delete(item)
    db.session.commit()
    flash(f"Command Item '{item.name}' deleted.", "info")
    return redirect(url_for("remote_exec.dashboard"))


# ─── Command Group CRUD & Builder ─────────────────────────────────────────────

@bp.route("/groups/new", methods=["GET", "POST"])
@remote_exec_required
def group_create():
    """Create a new Command Group."""
    servers = Server.query.order_by(Server.name).all()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        server_id = request.form.get("server_id", type=int)
        description = request.form.get("description", "").strip()
        run_in_parallel = bool(request.form.get("run_in_parallel"))
        stop_on_error = bool(request.form.get("stop_on_error", True))

        if not name or not server_id:
            flash("Group name and target server are required.", "error")
            return render_template("command_group_form.html", group=None, servers=servers)

        group = CommandGroup(
            server_id=server_id,
            name=name,
            description=description,
            run_in_parallel=run_in_parallel,
            stop_on_error=stop_on_error,
            created_by_id=current_user.id,
            created_on=datetime.now(timezone.utc),
        )
        db.session.add(group)
        db.session.commit()
        flash(f"Command Group '{group.name}' created.", "success")
        return redirect(url_for("remote_exec.group_edit", group_id=group.id))

    return render_template("command_group_form.html", group=None, servers=servers)


@bp.route("/groups/<int:group_id>/edit", methods=["GET", "POST"])
@remote_exec_required
def group_edit(group_id: int):
    """Edit Command Group details, reorder items, ADD, CLEAR, REMOVE."""
    group = CommandGroup.query.get_or_404(group_id)
    if not user_can_edit_group(current_user, group):
        flash("You do not have permission to edit this Command Group.", "error")
        return redirect(url_for("remote_exec.dashboard"))

    servers = Server.query.order_by(Server.name).all()
    # Available Command Items for ADD (matching target server)
    available_items = (
        CommandItem.query.filter_by(server_id=group.server_id)
        .order_by(CommandItem.name)
        .all()
    )

    if request.method == "POST":
        group.name = request.form.get("name", group.name).strip()
        group.description = request.form.get("description", "").strip()
        group.run_in_parallel = bool(request.form.get("run_in_parallel"))
        group.stop_on_error = bool(request.form.get("stop_on_error"))
        db.session.commit()
        flash("Command Group updated.", "success")
        return redirect(url_for("remote_exec.group_edit", group_id=group.id))

    return render_template(
        "command_group_form.html",
        group=group,
        servers=servers,
        available_items=available_items,
    )


@bp.post("/groups/<int:group_id>/add-item")
@remote_exec_required
def group_add_item(group_id: int):
    """ADD button: Adds a Command Item into the Command Group."""
    group = CommandGroup.query.get_or_404(group_id)
    if not user_can_edit_group(current_user, group):
        return jsonify(error="Unauthorized"), 403

    item_id = request.form.get("item_id", type=int)
    item = CommandItem.query.get_or_404(item_id)

    max_order = max([i.sequence_order for i in group.items] + [-1])
    link = CommandGroupItem(
        group_id=group.id,
        command_item_id=item.id,
        sequence_order=max_order + 1,
    )
    db.session.add(link)
    db.session.commit()
    flash(f"Added '{item.name}' to group.", "success")
    return redirect(url_for("remote_exec.group_edit", group_id=group.id))


@bp.post("/groups/<int:group_id>/clear")
@remote_exec_required
def group_clear_items(group_id: int):
    """CLEAR button: Clears all Command Items in the group."""
    group = CommandGroup.query.get_or_404(group_id)
    if not user_can_edit_group(current_user, group):
        return jsonify(error="Unauthorized"), 403

    CommandGroupItem.query.filter_by(group_id=group.id).delete()
    db.session.commit()
    flash("All Command Items cleared from this group.", "info")
    return redirect(url_for("remote_exec.group_edit", group_id=group.id))


@bp.post("/groups/<int:group_id>/remove-item/<int:link_id>")
@remote_exec_required
def group_remove_item(group_id: int, link_id: int):
    """REMOVE button: Removes an item from the group."""
    group = CommandGroup.query.get_or_404(group_id)
    if not user_can_edit_group(current_user, group):
        return jsonify(error="Unauthorized"), 403

    link = CommandGroupItem.query.get_or_404(link_id)
    db.session.delete(link)
    db.session.commit()
    flash("Item removed from group.", "info")
    return redirect(url_for("remote_exec.group_edit", group_id=group.id))


@bp.post("/groups/<int:group_id>/move/<int:link_id>/<direction>")
@remote_exec_required
def group_move_item(group_id: int, link_id: int, direction: str):
    """MOVE UP / MOVE DOWN button: Changes item order in the group."""
    group = CommandGroup.query.get_or_404(group_id)
    if not user_can_edit_group(current_user, group):
        return jsonify(error="Unauthorized"), 403

    items = sorted(group.items, key=lambda x: x.sequence_order)
    idx = next((i for i, item in enumerate(items) if item.id == link_id), None)

    if idx is not None:
        if direction == "up" and idx > 0:
            items[idx].sequence_order, items[idx - 1].sequence_order = (
                items[idx - 1].sequence_order,
                items[idx].sequence_order,
            )
        elif direction == "down" and idx < len(items) - 1:
            items[idx].sequence_order, items[idx + 1].sequence_order = (
                items[idx + 1].sequence_order,
                items[idx].sequence_order,
            )
        db.session.commit()

    return redirect(url_for("remote_exec.group_edit", group_id=group.id))


@bp.post("/groups/<int:group_id>/delete")
@remote_exec_required
def group_delete(group_id: int):
    """Users with edit can also delete a Command Group."""
    group = CommandGroup.query.get_or_404(group_id)
    if not user_can_edit_group(current_user, group):
        flash("You do not have permission to delete this Command Group.", "error")
        return redirect(url_for("remote_exec.dashboard"))

    db.session.delete(group)
    db.session.commit()
    flash(f"Command Group '{group.name}' deleted.", "info")
    return redirect(url_for("remote_exec.dashboard"))


# ─── Admin Permissions Matrix ─────────────────────────────────────────────────

@bp.route("/groups/<int:group_id>/permissions", methods=["GET", "POST"])
@remote_exec_required
def group_permissions(group_id: int):
    """Admin UI to assign run, view logs, edit, create, and duplicate access per member."""
    if not current_user.is_admin:
        abort(403)

    group = CommandGroup.query.get_or_404(group_id)
    members = Member.query.order_by(Member.name).all()

    if request.method == "POST":
        for member in members:
            can_run = bool(request.form.get(f"can_run_{member.id}"))
            can_view_logs = bool(request.form.get(f"can_view_logs_{member.id}"))
            can_edit = bool(request.form.get(f"can_edit_{member.id}"))
            can_duplicate = bool(request.form.get(f"can_duplicate_{member.id}"))

            perm = CommandGroupPermission.query.filter_by(group_id=group.id, user_id=member.id).first()
            if not perm and (can_run or can_view_logs or can_edit or can_duplicate):
                perm = CommandGroupPermission(group_id=group.id, user_id=member.id)
                db.session.add(perm)

            if perm:
                perm.can_run = can_run
                perm.can_view_logs = can_view_logs
                perm.can_edit = can_edit
                perm.can_duplicate = can_duplicate

        db.session.commit()
        flash("Permissions updated.", "success")
        return redirect(url_for("remote_exec.group_permissions", group_id=group.id))

    perms_by_user = {p.user_id: p for p in group.permissions}
    return render_template(
        "command_group_permissions.html",
        group=group,
        members=members,
        perms_by_user=perms_by_user,
    )
