"""AsyncSSH Agentless Remote Server Engine and Orchestrator.

Provides:
- Native AsyncSSH connection management supporting password and private-key authentication.
- Automated server OS detection, telemetry gathering, and health checks.
- Cross-platform line-ending normalization (Linux vs Windows).
- Dynamic variable placeholder replacement ({{ Variable }}) with deduplicated Set aggregation.
- Single persistent SSH session execution for sequential Command Groups (preserving cd and env state).
- SFTP file upload to the current working directory.
- Local PyWA command execution.
- Remote process termination via kill -9 (Linux) or taskkill /F /PID /T (Windows).
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import asyncssh

logger = logging.getLogger("mizalla.asyncssh")

# Regex to find {{ Variable-Name }} tokens
VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_\-]+)\s*\}\}")


# ─── Variable Extraction & Line Ending Helpers ───────────────────────────────

def extract_variable_names(command_text: str) -> Set[str]:
    """Finds all unique {{ Variable-Name }} placeholders in a command string."""
    if not command_text:
        return set()
    return set(VARIABLE_PATTERN.findall(command_text))


def substitute_variables(command_text: str, variables: Dict[str, Any]) -> str:
    """Substitutes {{ Variable-Name }} with user-supplied values.
    
    If a variable is not provided, leaves it unchanged or replaces with empty string.
    """
    if not command_text:
        return ""

    def replacer(match):
        key = match.group(1).strip()
        if key in variables:
            val = variables[key]
            # Convert boolean to lower string (true/false) or 1/0
            if isinstance(val, bool):
                return "true" if val else "false"
            return str(val)
        return match.group(0)

    return VARIABLE_PATTERN.sub(replacer, command_text)


def normalize_line_endings(command_text: str, os_type: str = "linux") -> str:
    """Normalizes line endings for cross-platform execution.
    
    - Linux (Ubuntu/RedHat): CRLF -> LF (\\n)
    - Windows: LF -> CRLF (\\r\\n)
    """
    if not command_text:
        return ""
    # Standardize to LF first
    unified = command_text.replace("\r\n", "\n").replace("\r", "\n")
    if os_type.lower() == "windows":
        return unified.replace("\n", "\r\n")
    return unified


# ─── AsyncSSH Connection Helpers ─────────────────────────────────────────────

def _build_client_options(username: str, password: str = "", private_key: str = "") -> dict:
    """Builds kwargs for asyncssh.connect()."""
    opts: dict = {
        "username": username or "root",
        "known_hosts": None,  # Accept server host key
        "connect_timeout": 7.0,
    }
    if password:
        opts["password"] = password

    if private_key:
        try:
            key = asyncssh.import_private_key(private_key, passphrase=password or None)
            opts["client_keys"] = [key]
        except Exception as e:
            logger.warning(f"Could not load SSH private key: {e}")
            if not password:
                raise

    return opts


async def test_ssh_connection(
    ip: str,
    port: int,
    username: str,
    password: str = "",
    private_key: str = "",
) -> Tuple[bool, str, dict]:
    """Tests SSH connectivity and gathers OS telemetry."""
    telemetry = {
        "os_type": "linux",
        "os_version": "",
        "packages_summary": "",
    }
    opts = _build_client_options(username, password, private_key)

    try:
        async with asyncssh.connect(ip, port=port or 22, **opts) as conn:
            # Probe OS type and version
            # First check Linux
            res_linux = await conn.run("uname -s", check=False, timeout=5.0)
            if res_linux.exit_status == 0 and "Linux" in res_linux.stdout:
                telemetry["os_type"] = "linux"
                # Get distro version
                os_rel = await conn.run("cat /etc/os-release 2>/dev/null", check=False, timeout=5.0)
                if os_rel.exit_status == 0:
                    for line in os_rel.stdout.splitlines():
                        if line.startswith("PRETTY_NAME="):
                            telemetry["os_version"] = line.split("=", 1)[1].strip('"\'')
                            break

                # Count packages
                dpkg_res = await conn.run("dpkg -l 2>/dev/null | grep '^ii' | wc -l", check=False, timeout=5.0)
                if dpkg_res.exit_status == 0 and dpkg_res.stdout.strip().isdigit():
                    telemetry["packages_summary"] = f"{dpkg_res.stdout.strip()} dpkg packages installed"
                else:
                    rpm_res = await conn.run("rpm -qa 2>/dev/null | wc -l", check=False, timeout=5.0)
                    if rpm_res.exit_status == 0 and rpm_res.stdout.strip().isdigit():
                        telemetry["packages_summary"] = f"{rpm_res.stdout.strip()} rpm packages installed"
                    else:
                        telemetry["packages_summary"] = "Standard Linux packages"

                return True, "SSH connection successful (Linux).", telemetry

            # Otherwise probe Windows
            res_win = await conn.run("powershell -Command \"[System.Environment]::OSVersion.VersionString\"", check=False, timeout=5.0)
            if res_win.exit_status == 0 and res_win.stdout.strip():
                telemetry["os_type"] = "windows"
                telemetry["os_version"] = res_win.stdout.strip()
                telemetry["packages_summary"] = "Windows OpenSSH Server"
                return True, "SSH connection successful (Windows).", telemetry

            # Default fallback
            telemetry["os_type"] = "linux"
            telemetry["os_version"] = "Generic POSIX / Linux"
            return True, "SSH connection successful.", telemetry

    except (asyncssh.PermissionDenied, asyncssh.KeyImportError) as e:
        return False, f"Authentication failed: {e}", telemetry
    except (asyncio.TimeoutError, TimeoutError):
        return False, f"Connection timed out connecting to {ip}:{port}.", telemetry
    except Exception as e:
        return False, f"Connection failed: {e}", telemetry


# ─── Remote Process Termination ──────────────────────────────────────────────

async def terminate_remote_pid(
    ip: str,
    port: int,
    username: str,
    password: str,
    private_key: str,
    pid: int,
    os_type: str = "linux",
) -> bool:
    """Terminates a remote process tree by PID via SSH."""
    if not pid:
        return False
    opts = _build_client_options(username, password, private_key)
    try:
        async with asyncssh.connect(ip, port=port or 22, **opts) as conn:
            if os_type.lower() == "windows":
                cmd = f"taskkill /F /PID {pid} /T"
            else:
                cmd = f"kill -9 {pid} 2>/dev/null || pkill -P {pid} 2>/dev/null || true"
            res = await conn.run(cmd, check=False, timeout=5.0)
            return res.exit_status == 0
    except Exception as e:
        logger.warning(f"Failed to terminate remote PID {pid}: {e}")
        return False


# ─── Local PyWA Subprocess Runner ─────────────────────────────────────────────

async def run_local_command_async(command: str) -> Tuple[int, str]:
    """Runs a local command on the PyWA server, capturing combined stdout/stderr."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace") if stdout else ""
        return proc.returncode or 0, output
    except Exception as e:
        return 1, f"Local execution failed: {e}"


# ─── Single Item Remote Runner ────────────────────────────────────────────────

async def run_single_remote_command(
    ip: str,
    port: int,
    username: str,
    password: str,
    private_key: str,
    command: str,
    os_type: str = "linux",
) -> Tuple[int, str, Optional[int]]:
    """Runs a single remote command, returning (exit_code, full_output, remote_pid)."""
    opts = _build_client_options(username, password, private_key)
    clean_cmd = normalize_line_endings(command, os_type)

    try:
        async with asyncssh.connect(ip, port=port or 22, **opts) as conn:
            # On Linux wrap with echo $$ to capture the PID
            if os_type.lower() != "windows":
                wrapped = f"echo MIZALLA_PID:$$; exec {clean_cmd}"
            else:
                wrapped = clean_cmd

            res = await conn.run(wrapped, check=False)
            output = res.stdout or ""
            if res.stderr:
                output += ("\n" if output else "") + res.stderr

            pid = None
            if "MIZALLA_PID:" in output:
                lines = output.splitlines()
                clean_lines = []
                for line in lines:
                    if line.startswith("MIZALLA_PID:"):
                        try:
                            pid = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            pass
                    else:
                        clean_lines.append(line)
                output = "\n".join(clean_lines)

            return res.exit_status or 0, output, pid
    except Exception as e:
        return 1, f"SSH Command Execution Error: {e}", None


# ─── SFTP File Upload Helper ──────────────────────────────────────────────────

async def upload_files_sftp(
    conn: asyncssh.SSHClientConnection,
    remote_cwd: str,
    files: List[Tuple[str, bytes]],
) -> Tuple[int, str]:
    """Uploads in-memory files directly to remote_cwd using SFTP."""
    logs = []
    try:
        async with conn.start_sftp_client() as sftp:
            # Resolve remote directory path
            target_dir = remote_cwd.strip() or "."
            for filename, file_bytes in files:
                remote_path = f"{target_dir}/{filename}" if target_dir != "." else filename
                async with sftp.open(remote_path, "wb") as remote_file:
                    await remote_file.write(file_bytes)
                logs.append(f"Uploaded file '{filename}' ({len(file_bytes)} bytes) to '{remote_path}'")
        return 0, "\n".join(logs)
    except Exception as e:
        return 1, f"SFTP Upload Error: {e}"


# ─── Persistent Session Sequential Group Runner ──────────────────────────────

async def execute_sequential_command_group(
    ip: str,
    port: int,
    username: str,
    password: str,
    private_key: str,
    group_name: str,
    items: List[dict],
    stop_on_error: bool = True,
    os_type: str = "linux",
    on_step_change: Optional[Callable[[int, dict, str, Optional[int], str], Awaitable[None]]] = None,
) -> Tuple[int, str, Optional[int]]:
    """Runs a sequence of CommandItems in a SINGLE persistent SSH session.
    
    Each item's output is tagged with '[Group Name] [Item Name]'.
    Directory changes (cd) and environment changes persist across items.
    """
    opts = _build_client_options(username, password, private_key)
    overall_exit_code = 0
    full_log_chunks = []
    last_pid = None

    has_remote_or_upload = any(item.get("command_type", "remote") in ("remote", "upload") for item in items)
    if not has_remote_or_upload:
        # All items are local; execute directly without connecting to remote SSH
        for idx, item in enumerate(items):
            item_name = item.get("name", "Unnamed Step")
            item_cmd = item.get("command", "")
            prefix = f"[{group_name}] [{item_name}]"
            if on_step_change:
                await on_step_change(idx, item, "running", None, "")
            exit_code, out = await run_local_command_async(item_cmd)
            out_text = out.strip()
            formatted_out = "\n".join(f"{prefix} {line}" for line in out.splitlines()) if out else f"{prefix} (Done)"
            full_log_chunks.append(formatted_out)
            if on_step_change:
                await on_step_change(idx, item, "completed" if exit_code == 0 else "failed", exit_code, out_text)
            if exit_code != 0:
                overall_exit_code = exit_code
                if stop_on_error:
                    full_log_chunks.append(f"{prefix} Execution halted due to error in local command.")
                    break
        return overall_exit_code, "\n".join(full_log_chunks), None

    try:
        async with asyncssh.connect(ip, port=port or 22, **opts) as conn:
            # Track current remote working directory across sequential commands
            current_cwd = "."

            for idx, item in enumerate(items):
                item_name = item.get("name", "Unnamed Step")
                item_type = item.get("command_type", "remote")
                item_cmd = item.get("command", "")
                prefix = f"[{group_name}] [{item_name}]"

                if on_step_change:
                    await on_step_change(idx, item, "running", None, "")

                if item_type == "local":
                    # Local execution on PyWA server
                    exit_code, out = await run_local_command_async(item_cmd)
                    out_text = out.strip()
                    formatted_out = "\n".join(f"{prefix} {line}" for line in out.splitlines()) if out else f"{prefix} (Done)"
                    full_log_chunks.append(formatted_out)
                    if on_step_change:
                        await on_step_change(idx, item, "completed" if exit_code == 0 else "failed", exit_code, out_text)
                    if exit_code != 0:
                        overall_exit_code = exit_code
                        if stop_on_error:
                            full_log_chunks.append(f"{prefix} Execution halted due to error in local command.")
                            break
                    continue

                elif item_type == "upload":
                    # Upload files to current_cwd
                    files_to_upload = item.get("files", [])  # list of (filename, bytes)
                    exit_code, out = await upload_files_sftp(conn, current_cwd, files_to_upload)
                    out_text = out.strip()
                    formatted_out = "\n".join(f"{prefix} {line}" for line in out.splitlines())
                    full_log_chunks.append(formatted_out)
                    if on_step_change:
                        await on_step_change(idx, item, "completed" if exit_code == 0 else "failed", exit_code, out_text)
                    if exit_code != 0:
                        overall_exit_code = exit_code
                        if stop_on_error:
                            full_log_chunks.append(f"{prefix} Execution halted due to upload failure.")
                            break
                    continue

                else:
                    # Remote shell command
                    # Chain with cd into current_cwd, run command, and capture new cwd + exit code
                    clean_cmd = normalize_line_endings(item_cmd, os_type)
                    if os_type.lower() == "windows":
                        wrapper = f"cd {current_cwd}; {clean_cmd}; $lastexit = $LASTEXITCODE; $pwd = (Get-Location).Path; Write-Output \"__MIZALLA_CWD__:$pwd\"; exit $lastexit"
                    else:
                        wrapper = f"cd {shlex.quote(current_cwd)} 2>/dev/null || true; {clean_cmd}; _RET=$?; echo '__MIZALLA_CWD__:'$(pwd); exit $_RET"

                    res = await conn.run(wrapper, check=False)
                    raw_out = res.stdout or ""
                    if res.stderr:
                        raw_out += ("\n" if raw_out else "") + res.stderr

                    # Parse out __MIZALLA_CWD__
                    step_lines = []
                    for line in raw_out.splitlines():
                        if line.startswith("__MIZALLA_CWD__:"):
                            current_cwd = line.split(":", 1)[1].strip() or current_cwd
                        else:
                            step_lines.append(line)

                    out_text = "\n".join(step_lines).strip()
                    if out_text:
                        formatted = "\n".join(f"{prefix} {l}" for l in out_text.splitlines())
                    else:
                        formatted = f"{prefix} (Executed without output, exit code {res.exit_status})"
                    full_log_chunks.append(formatted)

                    step_ec = res.exit_status or 0
                    if on_step_change:
                        await on_step_change(idx, item, "completed" if step_ec == 0 else "failed", step_ec, out_text)

                    if step_ec != 0:
                        overall_exit_code = step_ec
                        if stop_on_error:
                            full_log_chunks.append(f"{prefix} Step exited with code {overall_exit_code}. Execution halted.")
                            break

        return overall_exit_code, "\n".join(full_log_chunks), last_pid

    except Exception as e:
        full_log_chunks.append(f"[{group_name}] SSH Session Error: {e}")
        return 1, "\n".join(full_log_chunks), None


# ─── Parallel Group Runner ────────────────────────────────────────────────────

async def execute_parallel_command_group(
    ip: str,
    port: int,
    username: str,
    password: str,
    private_key: str,
    group_name: str,
    items: List[dict],
    stop_on_error: bool = False,
    os_type: str = "linux",
    on_step_change: Optional[Callable[[int, dict, str, Optional[int], str], Awaitable[None]]] = None,
) -> Tuple[int, str, Optional[int]]:
    """Runs items in a CommandGroup concurrently in parallel."""
    opts = _build_client_options(username, password, private_key)
    full_log_chunks = [""] * len(items)
    step_exit_codes = [0] * len(items)

    async def _run_item(idx: int, item: dict):
        item_name = item.get("name", f"Step {idx+1}")
        item_type = item.get("command_type", "remote")
        item_cmd = item.get("command", "")
        prefix = f"[{group_name}] [{item_name}]"

        if on_step_change:
            await on_step_change(idx, item, "running", None, "")

        if item_type == "local":
            ec, out = await run_local_command_async(item_cmd)
        elif item_type == "upload":
            files_to_upload = item.get("files", [])
            try:
                async with asyncssh.connect(ip, port=port or 22, **opts) as conn:
                    ec, out = await upload_files_sftp(conn, ".", files_to_upload)
            except Exception as e:
                ec, out = 1, f"SFTP upload error: {e}"
        else:
            try:
                clean_cmd = normalize_line_endings(item_cmd, os_type)
                async with asyncssh.connect(ip, port=port or 22, **opts) as conn:
                    res = await conn.run(clean_cmd, check=False)
                    ec = res.exit_status or 0
                    out = res.stdout or ""
                    if res.stderr:
                        out += ("\n" if out else "") + res.stderr
            except Exception as e:
                ec, out = 1, f"SSH error: {e}"

        step_exit_codes[idx] = ec
        out_text = out.strip()
        formatted = "\n".join(f"{prefix} {line}" for line in out_text.splitlines()) if out_text else f"{prefix} (Done)"
        full_log_chunks[idx] = formatted
        if on_step_change:
            await on_step_change(idx, item, "completed" if ec == 0 else "failed", ec, out_text)

    await asyncio.gather(*[_run_item(i, item) for i, item in enumerate(items)])
    overall_exit_code = max(step_exit_codes) if step_exit_codes else 0
    return overall_exit_code, "\n".join(chunk for chunk in full_log_chunks if chunk), None
