"""Background Orchestration & AsyncSSH Client Bridge for Flask.

Dispatches remote and local commands, manages background execution threads,
handles permissions, and bridges Flask with the asyncssh engine.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app

from .asyncssh_engine import (
    execute_parallel_command_group,
    execute_sequential_command_group,
    run_single_remote_command,
    run_local_command_async,
    substitute_variables,
    terminate_remote_pid,
    test_ssh_connection,
)
from .extensions import db
from .models import (
    COMMAND_STATUS_COMPLETED,
    COMMAND_STATUS_FAILED,
    COMMAND_STATUS_PENDING,
    COMMAND_STATUS_RUNNING,
    COMMAND_STATUS_TERMINATED,
    CommandAudit,
    CommandGroup,
    CommandGroupPermission,
    CommandItem,
    CommandItemPermission,
    Member,
    Server,
    ServerAccess,
    ServerCredential,
)

logger = logging.getLogger("mizalla.remote_exec_client")

# Active execution threads: task_id -> threading.Thread
_active_threads: Dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


# ─── Permissions & RBAC Helpers ──────────────────────────────────────────────

def user_can_view_server(user: Member, server: Server) -> bool:
    if getattr(user, "is_admin", False):
        return True
    grant = ServerAccess.query.filter_by(server_id=server.id, member_id=user.id).first()
    if grant is not None:
        return bool(grant.can_view)
    return True


def user_can_edit_server(user: Member, server: Server) -> bool:
    if getattr(user, "is_admin", False):
        return True
    grant = ServerAccess.query.filter_by(server_id=server.id, member_id=user.id).first()
    return bool(grant and grant.can_edit)


def user_can_add_item(user: Member) -> bool:
    return bool(getattr(user, "is_admin", False) or getattr(user, "can_add_command_items", False))


def user_can_add_group(user: Member) -> bool:
    return bool(getattr(user, "is_admin", False) or getattr(user, "can_add_command_groups", False))


def user_can_view_group(user: Member, group: CommandGroup) -> bool:
    if getattr(user, "is_admin", False) or group.created_by_id == user.id:
        return True
    perm = CommandGroupPermission.query.filter_by(group_id=group.id, user_id=user.id).first()
    return bool(perm and (perm.can_view or perm.can_run or perm.can_edit))


def user_can_run_group(user: Member, group: CommandGroup) -> bool:
    # Having VIEW access means you can also RUN it.
    return user_can_view_group(user, group)


def user_can_view_group_logs(user: Member, group: CommandGroup) -> bool:
    if getattr(user, "is_admin", False) or group.created_by_id == user.id:
        return True
    perm = CommandGroupPermission.query.filter_by(group_id=group.id, user_id=user.id).first()
    if not perm:
        return False
    return bool(perm.can_view_logs or perm.can_edit)


def user_can_edit_group(user: Member, group: CommandGroup) -> bool:
    if getattr(user, "is_admin", False) or group.created_by_id == user.id:
        return True
    perm = CommandGroupPermission.query.filter_by(group_id=group.id, user_id=user.id).first()
    return bool(perm and perm.can_edit)


def user_can_delete_group(user: Member, group: CommandGroup) -> bool:
    # Having EDIT access means you can RUN/DELETE it.
    return user_can_edit_group(user, group)


def user_can_duplicate_group(user: Member, group: CommandGroup) -> bool:
    if getattr(user, "is_admin", False) or group.created_by_id == user.id:
        return True
    perm = CommandGroupPermission.query.filter_by(group_id=group.id, user_id=user.id).first()
    return bool(perm and (perm.can_duplicate or perm.can_edit))


def user_can_view_item(user: Member, item: CommandItem) -> bool:
    if getattr(user, "is_admin", False) or item.created_by_id == user.id:
        return True
    perm = CommandItemPermission.query.filter_by(command_item_id=item.id, user_id=user.id).first()
    return bool(perm and (perm.can_view or perm.can_edit))


def user_can_run_item(user: Member, item: CommandItem) -> bool:
    # Command Items are building blocks and are executed exclusively via Command Groups.
    return False


def user_can_view_item_logs(user: Member, item: CommandItem) -> bool:
    if getattr(user, "is_admin", False) or item.created_by_id == user.id:
        return True
    perm = CommandItemPermission.query.filter_by(command_item_id=item.id, user_id=user.id).first()
    if not perm:
        return False
    return bool(perm.can_view_logs or perm.can_edit)


def user_can_edit_item(user: Member, item: CommandItem) -> bool:
    if getattr(user, "is_admin", False) or item.created_by_id == user.id:
        return True
    perm = CommandItemPermission.query.filter_by(command_item_id=item.id, user_id=user.id).first()
    return bool(perm and perm.can_edit)


def user_can_delete_item(user: Member, item: CommandItem) -> bool:
    # Having EDIT access means you can RUN/DELETE it.
    return user_can_edit_item(user, item)


def user_can_duplicate_item(user: Member, item: CommandItem) -> bool:
    if getattr(user, "is_admin", False) or item.created_by_id == user.id:
        return True
    perm = CommandItemPermission.query.filter_by(command_item_id=item.id, user_id=user.id).first()
    return bool(perm and (perm.can_duplicate or perm.can_edit))


# ─── Effective Credential & Auth Resolver ──────────────────────────────────────

def get_effective_credential(server_id: int, user_id: Optional[int] = None) -> Optional[ServerCredential]:
    """Finds user's credential for server, or falls back to Admin server default."""
    if user_id:
        user_cred = ServerCredential.query.filter_by(server_id=server_id, user_id=user_id).first()
        if user_cred and (user_cred.password or user_cred.private_key):
            return user_cred

    # Fallback to default
    default_cred = (
        ServerCredential.query.filter_by(server_id=server_id)
        .filter((ServerCredential.is_default == True) | (ServerCredential.user_id.is_(None)))
        .first()
    )
    if default_cred and (default_cred.password or default_cred.private_key):
        return default_cred

    return None


def get_server_user_auth_status(server: Server, user: Member) -> dict:
    """Calculates credential, connection, and button states for a specific user and server."""
    user_cred = None
    if user and user.is_authenticated:
        user_cred = ServerCredential.query.filter_by(server_id=server.id, user_id=user.id).first()

    admin_cred = (
        ServerCredential.query.filter_by(server_id=server.id)
        .filter((ServerCredential.is_default == True) | (ServerCredential.user_id.is_(None)))
        .first()
    )

    has_user_cred = bool(user_cred and (user_cred.password or user_cred.private_key))
    has_admin_cred = bool(admin_cred and (admin_cred.password or admin_cred.private_key))

    # Copy password button rules:
    # - User-owned credentials: Enabled
    # - Admin fallback: Disabled (unless user is Admin)
    # - No credentials: Disabled
    if getattr(user, "is_admin", False):
        can_copy_password = has_user_cred or has_admin_cred
    else:
        can_copy_password = has_user_cred

    # Connection history from ServerAccess
    grant = None
    if user and user.is_authenticated:
        grant = ServerAccess.query.filter_by(server_id=server.id, member_id=user.id).first()

    last_connected_at = grant.last_connected_at if grant else None
    connection_status = grant.connection_status if grant else "unverified"

    # If user is admin and hasn't connected individually, fallback to server's general availability
    if getattr(user, "is_admin", False) and not last_connected_at:
        if server.last_checked and server.is_available:
            last_connected_at = server.last_checked
            connection_status = "available"

    is_connectable = bool(
        (has_user_cred or has_admin_cred) and
        (getattr(user, "is_admin", False) or (last_connected_at and connection_status == "available"))
    )

    return {
        "has_user_cred": has_user_cred,
        "has_admin_cred": has_admin_cred,
        "is_fallback": not has_user_cred and has_admin_cred,
        "has_no_cred": not has_user_cred and not has_admin_cred,
        "can_copy_password": can_copy_password,
        "last_connected_at": last_connected_at,
        "connection_status": connection_status,
        "is_connectable": is_connectable,
    }


def get_connectable_servers_for_user(user: Member) -> list[Server]:
    """Returns servers the user has View access to AND has connected to at least once (or configured by Admin)."""
    all_servers = Server.query.order_by(Server.name).all()
    connectable = []
    for s in all_servers:
        if not user_can_view_server(user, s):
            continue
        auth = get_server_user_auth_status(s, user)
        if auth["is_connectable"]:
            connectable.append(s)
    return connectable


# ─── Command Execution Dispatchers ───────────────────────────────────────────

def dispatch_command_item(
    user_id: int,
    item_id: int,
    variables: Optional[Dict[str, Any]] = None,
    client_ip: str = "",
    app=None,
) -> CommandAudit:
    """Dispatches a single CommandItem asynchronously."""
    app = app or current_app._get_current_object()
    item = db.session.get(CommandItem, item_id)
    if not item:
        raise ValueError(f"CommandItem {item_id} not found.")

    server_id = item.server_id
    server = db.session.get(Server, server_id)
    task_id = f"item_{int(time.time())}_{secrets.token_hex(6)}"

    # Substitute variables
    vars_dict = variables or {}
    executed_cmd = substitute_variables(item.command_content, vars_dict)
    item_command_type = item.command_type
    server_ip = server.ip if server else "127.0.0.1"
    server_port = (server.ssh_port or 22) if server else 22
    os_type = (server.os_type or "linux") if server else "linux"

    audit = CommandAudit(
        task_id=task_id,
        user_id=user_id,
        server_id=server_id,
        command_item_id=item.id,
        entry_type="item",
        title=item.name,
        command=item.command_content,
        executed_command=executed_cmd,
        status=COMMAND_STATUS_PENDING,
        client_ip=client_ip,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(audit)
    db.session.commit()
    audit_id = audit.id

    def _worker():
        with app.app_context():
            a = db.session.get(CommandAudit, audit_id)
            if not a:
                return
            a.status = COMMAND_STATUS_RUNNING
            a.start_time = datetime.now(timezone.utc)
            db.session.commit()

            cred = get_effective_credential(server_id, user_id)

            if item_command_type == "local":
                # PyWA server local command
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    exit_code, output = loop.run_until_complete(run_local_command_async(executed_cmd))
                finally:
                    loop.close()
                pid = None
            else:
                # Remote command via AsyncSSH
                if not cred:
                    a.status = COMMAND_STATUS_FAILED
                    a.end_time = datetime.now(timezone.utc)
                    a.exit_code = 1
                    a.full_output = "No valid SSH credentials found for this server."
                    db.session.commit()
                    return

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    exit_code, output, pid = loop.run_until_complete(
                        run_single_remote_command(
                            ip=server_ip,
                            port=server_port,
                            username=cred.username,
                            password=cred.password,
                            private_key=cred.private_key,
                            command=executed_cmd,
                            os_type=os_type,
                        )
                    )
                finally:
                    loop.close()

            a.status = COMMAND_STATUS_COMPLETED if exit_code == 0 else COMMAND_STATUS_FAILED
            a.end_time = datetime.now(timezone.utc)
            a.exit_code = exit_code
            a.full_output = output
            a.remote_pid = pid
            db.session.commit()

    th = threading.Thread(target=_worker, name=f"ItemRunner-{task_id}", daemon=True)
    with _threads_lock:
        _active_threads[task_id] = th
    th.start()
    return audit


def dispatch_command_group(
    user_id: int,
    group_id: int,
    variables: Optional[Dict[str, Any]] = None,
    uploaded_files: Optional[List[Tuple[str, bytes]]] = None,
    client_ip: str = "",
    app=None,
) -> CommandAudit:
    """Dispatches a CommandGroup sequentially in a single persistent SSH session."""
    app = app or current_app._get_current_object()
    group = db.session.get(CommandGroup, group_id)
    if not group:
        raise ValueError(f"CommandGroup {group_id} not found.")

    server_id = group.server_id
    server = db.session.get(Server, server_id)
    task_id = f"grp_{int(time.time())}_{secrets.token_hex(6)}"
    vars_dict = variables or {}

    # Prepare item configs and step audit structures
    items_config = []
    executed_cmds = []
    initial_steps = []
    for g_item in group.items:
        c_item = g_item.command_item
        if not c_item:
            continue
        item_sub_cmd = substitute_variables(c_item.command_content, vars_dict)
        files_for_step = list(uploaded_files or [])
        if c_item.command_type == "upload":
            try:
                import os
                upload_dir = os.path.join(app.instance_path, "command_uploads", str(c_item.id))
                if os.path.isdir(upload_dir):
                    for fname in os.listdir(upload_dir):
                        fpath = os.path.join(upload_dir, fname)
                        if os.path.isfile(fpath):
                            with open(fpath, "rb") as fp:
                                files_for_step.append((fname, fp.read()))
            except Exception as e:
                logger.warning(f"Failed to read pre-uploaded files for item {c_item.id}: {e}")

        items_config.append({
            "item_id": c_item.id,
            "name": c_item.name,
            "command_type": c_item.command_type,
            "command": item_sub_cmd,
            "files": files_for_step,
        })
        executed_cmds.append(f"# {c_item.name}\n{item_sub_cmd}")
        initial_steps.append({
            "item_id": c_item.id,
            "name": c_item.name,
            "command_type": c_item.command_type,
            "status": "pending",
            "exit_code": None,
            "output": "",
        })

    server_ip = server.ip if server else "127.0.0.1"
    server_port = (server.ssh_port or 22) if server else 22
    os_type = (server.os_type or "linux") if server else "linux"
    group_name = group.name
    run_in_parallel = group.run_in_parallel
    stop_on_error = group.stop_on_error

    audit = CommandAudit(
        task_id=task_id,
        user_id=user_id,
        server_id=server_id,
        group_id=group.id,
        entry_type="group",
        title=group.name,
        command="\n\n".join(f"# {gi.command_item.name}\n{gi.command_item.command_content}" for gi in group.items if gi.command_item),
        executed_command="\n\n".join(executed_cmds),
        status=COMMAND_STATUS_PENDING,
        client_ip=client_ip,
        created_at=datetime.now(timezone.utc),
    )
    audit.steps_data = initial_steps
    db.session.add(audit)
    db.session.commit()
    audit_id = audit.id

    def _worker():
        with app.app_context():
            a = db.session.get(CommandAudit, audit_id)
            if not a:
                return
            a.status = COMMAND_STATUS_RUNNING
            a.start_time = datetime.now(timezone.utc)
            db.session.commit()

            cred = get_effective_credential(server_id, user_id)
            if not cred:
                a.status = COMMAND_STATUS_FAILED
                a.end_time = datetime.now(timezone.utc)
                a.exit_code = 1
                a.full_output = f"[{group_name}] Error: No valid SSH credentials found for this server."
                db.session.commit()
                return

            async def _on_step_change(idx: int, item: dict, status: str, exit_code: Optional[int], output: str):
                with app.app_context():
                    cur = db.session.get(CommandAudit, audit_id)
                    if not cur:
                        return
                    cur_steps = cur.steps_data
                    if 0 <= idx < len(cur_steps):
                        cur_steps[idx]["status"] = status
                        if exit_code is not None:
                            cur_steps[idx]["exit_code"] = exit_code
                        if output:
                            cur_steps[idx]["output"] = output
                        cur.steps_data = cur_steps
                        db.session.commit()

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                if run_in_parallel:
                    runner_coro = execute_parallel_command_group(
                        ip=server_ip,
                        port=server_port,
                        username=cred.username,
                        password=cred.password,
                        private_key=cred.private_key,
                        group_name=group_name,
                        items=items_config,
                        stop_on_error=stop_on_error,
                        os_type=os_type,
                        on_step_change=_on_step_change,
                    )
                else:
                    runner_coro = execute_sequential_command_group(
                        ip=server_ip,
                        port=server_port,
                        username=cred.username,
                        password=cred.password,
                        private_key=cred.private_key,
                        group_name=group_name,
                        items=items_config,
                        stop_on_error=stop_on_error,
                        os_type=os_type,
                        on_step_change=_on_step_change,
                    )
                exit_code, output, pid = loop.run_until_complete(runner_coro)
            finally:
                loop.close()

            a.status = COMMAND_STATUS_COMPLETED if exit_code == 0 else COMMAND_STATUS_FAILED
            a.end_time = datetime.now(timezone.utc)
            a.exit_code = exit_code
            a.full_output = output
            a.remote_pid = pid
            db.session.commit()

    th = threading.Thread(target=_worker, name=f"GroupRunner-{task_id}", daemon=True)
    with _threads_lock:
        _active_threads[task_id] = th
    th.start()
    return audit


# ─── Process Termination Helper ──────────────────────────────────────────────

def terminate_running_audit(audit_id: int, app=None) -> bool:
    """Terminates an in-flight command audit and remote process."""
    app = app or current_app._get_current_object()
    with app.app_context():
        audit = db.session.get(CommandAudit, audit_id)
        if not audit or audit.status not in (COMMAND_STATUS_PENDING, COMMAND_STATUS_RUNNING):
            return False

        server = db.session.get(Server, audit.server_id)
        cred = get_effective_credential(audit.server_id, audit.user_id)

        # Terminate remote PID if recorded
        if audit.remote_pid and server and cred:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    terminate_remote_pid(
                        ip=server.ip,
                        port=server.ssh_port or 22,
                        username=cred.username,
                        password=cred.password,
                        private_key=cred.private_key,
                        pid=audit.remote_pid,
                        os_type=server.os_type,
                    )
                )
            finally:
                loop.close()

        audit.status = COMMAND_STATUS_TERMINATED
        audit.end_time = datetime.now(timezone.utc)
        audit.full_output += "\n[Execution Terminated by User]"
        db.session.commit()
        return True


# ─── Server Health Probing ───────────────────────────────────────────────────

def probe_server_health_sync(server_id: int, app=None) -> Tuple[bool, str]:
    """Tests connection to a server and updates OS telemetry."""
    app = app or current_app._get_current_object()
    with app.app_context():
        server = db.session.get(Server, server_id)
        if not server:
            return False, "Server not found."

        cred = get_effective_credential(server_id)
        if not cred:
            server.status = "failed"
            server.last_checked = datetime.now(timezone.utc)
            db.session.commit()
            return False, "No credentials configured."

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ok, msg, telemetry = loop.run_until_complete(
                test_ssh_connection(
                    ip=server.ip,
                    port=server.ssh_port or 22,
                    username=cred.username,
                    password=cred.password,
                    private_key=cred.private_key,
                )
            )
        finally:
            loop.close()

        server.status = "available" if ok else "failed"
        server.last_checked = datetime.now(timezone.utc)
        if ok:
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
            if "k8s_pods" in telemetry:
                server.k8s_pods = telemetry.get("k8s_pods") or ""
            if "docker_compose_content" in telemetry:
                server.docker_compose_content = telemetry.get("docker_compose_content") or ""

        db.session.commit()
        return ok, msg

