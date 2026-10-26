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
import json
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

# Regex to find @@ Variable-Name @@ tokens
VARIABLE_PATTERN = re.compile(r"@@\s*([a-zA-Z0-9_\-]+)\s*@@")


# ─── Variable Extraction & Line Ending Helpers ───────────────────────────────

def extract_variable_names(command_text: str) -> Set[str]:
    """Finds all unique @@ Variable-Name @@ placeholders in a command string."""
    if not command_text:
        return set()
    return set(VARIABLE_PATTERN.findall(command_text))


def substitute_variables(command_text: str, variables: Dict[str, Any]) -> str:
    """Substitutes @@ Variable-Name @@ with user-supplied values.
    
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

def _build_client_options(username: str, password: str = "", private_key: str = "", timeout: Optional[float] = None) -> dict:
    """Builds kwargs for asyncssh.connect()."""
    opts: dict = {
        "username": username or "root",
        "known_hosts": None,  # Accept server host key
        "connect_timeout": float(timeout) if timeout is not None else 7.0,
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


# ─── OS Telemetry Script Loader ──────────────────────────────────────────────

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "automate" / "scripts"


def load_os_script(os_name: str) -> Optional[Dict[str, List[str]]]:
    """
    Loads telemetry script from /automate/scripts/<os>.txt.
    Commands to be executed in parallel are separated by comment lines (# ...).
    Returns a dict mapping section_tag -> list of command strings.
    """
    os_clean = (os_name or "").lower().strip()
    candidates = [
        SCRIPTS_DIR / f"{os_clean}.txt",
        Path.cwd() / "automate" / "scripts" / f"{os_clean}.txt",
        Path(f"/automate/scripts/{os_clean}.txt"),
    ]
    if os_clean in ["darwin", "macos"]:
        candidates.append(SCRIPTS_DIR / "mac.txt")
    elif os_clean in ["centos", "rocky", "almalinux", "fedora"]:
        candidates.append(SCRIPTS_DIR / "rhel.txt")
    elif os_clean in ["debian", "linux"]:
        candidates.append(SCRIPTS_DIR / "ubuntu.txt")

    script_file = None
    for p in candidates:
        if p.exists() and p.is_file():
            script_file = p
            break

    if not script_file:
        logger.warning(f"Telemetry script for '{os_name}' not found in {SCRIPTS_DIR}")
        return None

    try:
        content = script_file.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Error reading script file {script_file}: {e}")
        return None

    sections: Dict[str, List[str]] = {}
    current_tag: Optional[str] = None
    current_cmds: List[str] = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # Comment line marks parallel section tag: e.g. '# os_version' or '# ram'
            raw_tag = line.lstrip("#").strip().lower()
            if not raw_tag:
                continue
            # sanitize tag into an identifier
            tag = re.sub(r"[^a-z0-9_]+", "_", raw_tag).strip("_")
            if not tag:
                continue

            if current_tag and current_cmds:
                sections[current_tag] = current_cmds
            current_tag = tag
            current_cmds = []
        else:
            if current_tag:
                current_cmds.append(line)

    if current_tag and current_cmds:
        sections[current_tag] = current_cmds

    return sections


# ─── Parallel Telemetry Probes ───────────────────────────────────────────────

async def _probe_os(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Determine what OS is running on the Server (Ubuntu/RHEL/Windows/Mac) and version."""
    if cmds:
        out_parts = []
        for cmd in cmds:
            res = await conn.run(cmd, check=False, timeout=5.0)
            if res.stdout:
                out_parts.append(res.stdout)
        out = "\n".join(out_parts).strip()

        if os_type == "mac" or "Darwin" in out:
            m = re.search(r"(\d+\.\d+[\.\d\w\-]*)", out)
            ver_str = f"macOS {m.group(1)}" if m else "macOS"
            return {
                "os_type": "mac",
                "operating_system": ver_str,
                "os_version": ver_str,
                "packages_summary": "Homebrew / macOS packages",
            }
        elif os_type == "windows" or "Windows" in out or "Microsoft" in out:
            win_ver = out.splitlines()[0] if out else "Windows"
            return {
                "os_type": "windows",
                "operating_system": f"Windows ({win_ver})",
                "os_version": win_ver,
                "packages_summary": "Windows OpenSSH Server",
            }
        else:
            rel_text = out.lower()
            name_val = ""
            ver_val = ""
            pretty = ""
            for line in out.splitlines():
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip('"\'')
                elif line.startswith("NAME="):
                    name_val = line.split("=", 1)[1].strip('"\'')
                elif line.startswith("VERSION="):
                    ver_val = line.split("=", 1)[1].strip('"\'')

            if not pretty and name_val:
                pretty = f"{name_val} {ver_val}".strip()

            if "ubuntu" in rel_text:
                canonical_os = pretty or "Ubuntu Linux"
                os_distro = "ubuntu"
            elif any(k in rel_text for k in ["rhel", "red hat", "centos", "rocky", "almalinux", "fedora"]):
                canonical_os = pretty or "RHEL"
                os_distro = "rhel"
            elif "debian" in rel_text:
                canonical_os = pretty or "Debian"
                os_distro = "ubuntu"
            else:
                canonical_os = pretty or "Linux"
                os_distro = "linux"

            return {
                "os_type": os_distro,
                "operating_system": canonical_os,
                "os_version": canonical_os,
            }
    res_linux = await conn.run("uname -s", check=False, timeout=5.0)
    out = res_linux.stdout.strip()
    if "Darwin" in out:
        sw = await conn.run("sw_vers -productVersion 2>/dev/null", check=False, timeout=5.0)
        ver = sw.stdout.strip()
        ver_str = f"macOS {ver}" if ver else "macOS"
        return {
            "os_type": "mac",
            "operating_system": ver_str,
            "os_version": ver_str,
        }
    elif "Linux" in out:
        rel = await conn.run("cat /etc/os-release 2>/dev/null", check=False, timeout=5.0)
        rel_out = rel.stdout
        rel_text = rel_out.lower()
        pretty = ""
        name_val = ""
        ver_val = ""
        for line in rel_out.splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=", 1)[1].strip('"\'')
            elif line.startswith("NAME="):
                name_val = line.split("=", 1)[1].strip('"\'')
            elif line.startswith("VERSION="):
                ver_val = line.split("=", 1)[1].strip('"\'')

        if not pretty and name_val:
            pretty = f"{name_val} {ver_val}".strip()

        if "ubuntu" in rel_text:
            canonical_os = pretty or "Ubuntu Linux"
            os_distro = "ubuntu"
        elif any(k in rel_text for k in ["rhel", "red hat", "centos", "rocky", "almalinux", "fedora"]):
            canonical_os = pretty or "RHEL"
            os_distro = "rhel"
        elif "debian" in rel_text:
            canonical_os = pretty or "Debian"
            os_distro = "ubuntu"
        else:
            canonical_os = pretty or "Linux"
            os_distro = "linux"

        return {
            "os_type": os_distro,
            "operating_system": canonical_os,
            "os_version": canonical_os,
        }
    else:
        # Check Windows via PowerShell
        res_win = await conn.run('powershell -Command "[System.Environment]::OSVersion.VersionString"', check=False, timeout=5.0)
        if res_win.exit_status == 0 and res_win.stdout.strip():
            win_ver = res_win.stdout.strip()
            return {
                "os_type": "windows",
                "operating_system": f"Windows ({win_ver})",
                "os_version": win_ver,
                "packages_summary": "Windows OpenSSH Server",
            }
        return {
            "os_type": "linux",
            "operating_system": "Generic Linux",
            "os_version": "Generic Linux",
            "packages_summary": "Standard packages",
        }


def _parse_linux_ram(out: str) -> Optional[Dict[str, Any]]:
    """Parses /proc/meminfo or free command output into normalized RAM metrics."""
    if not out:
        return None

    # 1. First attempt: /proc/meminfo lines
    tot_kb, avail_kb = 0.0, 0.0
    free_kb, buffers_kb, cached_kb, sreclaimable_kb = 0.0, 0.0, 0.0, 0.0
    has_avail = False

    for line in out.splitlines():
        line_s = line.strip()
        if line_s.startswith("MemTotal:"):
            nums = re.findall(r"\d+", line_s)
            if nums:
                tot_kb = float(nums[0])
        elif line_s.startswith("MemAvailable:"):
            nums = re.findall(r"\d+", line_s)
            if nums:
                avail_kb = float(nums[0])
                has_avail = True
        elif line_s.startswith("MemFree:"):
            nums = re.findall(r"\d+", line_s)
            if nums:
                free_kb = float(nums[0])
        elif line_s.startswith("Buffers:"):
            nums = re.findall(r"\d+", line_s)
            if nums:
                buffers_kb = float(nums[0])
        elif line_s.startswith("Cached:"):
            nums = re.findall(r"\d+", line_s)
            if nums:
                cached_kb = float(nums[0])
        elif line_s.startswith("SReclaimable:"):
            nums = re.findall(r"\d+", line_s)
            if nums:
                sreclaimable_kb = float(nums[0])

    if not has_avail and (free_kb or buffers_kb or cached_kb):
        avail_kb = free_kb + buffers_kb + cached_kb + sreclaimable_kb

    if tot_kb > 0:
        used_kb = max(0.0, tot_kb - avail_kb)
        tot_gb = tot_kb / (1024 * 1024)
        used_gb = used_kb / (1024 * 1024)
        pct = round((used_gb / tot_gb) * 100, 1) if tot_gb > 0 else 0.0
        return {
            "ram_total": f"{tot_gb:.1f} GB",
            "ram_used": f"{used_gb:.1f} GB",
            "ram_percent": pct,
        }

    # 2. Second attempt: free command output (free -b, free -m, free -k)
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[0].lower().startswith("mem"):
            try:
                total_val = float(parts[1])
                if total_val > 0:
                    if total_val > 100_000_000:  # bytes from free -b
                        tot_gb = total_val / (1024**3)
                        if len(parts) >= 7 and parts[6].replace(".", "", 1).isdigit():
                            avail_gb = float(parts[6]) / (1024**3)
                            used_gb = max(0.0, tot_gb - avail_gb)
                        else:
                            used_gb = float(parts[2]) / (1024**3)
                    elif total_val > 100_000:  # KB from free -k
                        tot_gb = total_val / (1024 * 1024)
                        if len(parts) >= 7 and parts[6].replace(".", "", 1).isdigit():
                            avail_gb = float(parts[6]) / (1024 * 1024)
                            used_gb = max(0.0, tot_gb - avail_gb)
                        else:
                            used_gb = float(parts[2]) / (1024 * 1024)
                    else:  # MB from free -m
                        tot_gb = total_val / 1024
                        if len(parts) >= 7 and parts[6].replace(".", "", 1).isdigit():
                            avail_gb = float(parts[6]) / 1024
                            used_gb = max(0.0, tot_gb - avail_gb)
                        else:
                            used_gb = float(parts[2]) / 1024

                    pct = round((used_gb / tot_gb) * 100, 1) if tot_gb > 0 else 0.0
                    return {
                        "ram_total": f"{tot_gb:.1f} GB",
                        "ram_used": f"{used_gb:.1f} GB",
                        "ram_percent": pct,
                    }
            except Exception:
                pass

    return None


def _parse_linux_disk(out: str) -> Optional[Dict[str, Any]]:
    """Parses POSIX or standard df output into normalized disk metrics."""
    if not out:
        return None
    for line in out.splitlines():
        line_str = line.strip()
        if not line_str or line_str.lower().startswith("filesystem") or line_str.startswith("#"):
            continue
        parts = line_str.split()
        # Case A: Wrapped line (e.g. from long RHEL LVM device name without -P):
        # Line 1: /dev/mapper/rhel-root
        # Line 2: 52403200 10485760 41917440 21% /
        if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit():
            try:
                tot_kb = float(parts[0])
                used_kb = float(parts[1])
                tot_gb = tot_kb / (1024 * 1024)
                used_gb = used_kb / (1024 * 1024)
                pct = round((used_gb / tot_gb) * 100, 1) if tot_gb > 0 else 0.0
                return {
                    "disk_total": f"{tot_gb:.1f} GB",
                    "disk_used": f"{used_gb:.1f} GB",
                    "disk_percent": pct,
                }
            except Exception:
                pass
        # Case B: Standard unwrapped line (e.g. df -Pk /):
        # /dev/mapper/rhel-root 52403200 10485760 41917440 21% /
        elif len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit():
            try:
                tot_kb = float(parts[1])
                used_kb = float(parts[2])
                tot_gb = tot_kb / (1024 * 1024)
                used_gb = used_kb / (1024 * 1024)
                pct = round((used_gb / tot_gb) * 100, 1) if tot_gb > 0 else 0.0
                return {
                    "disk_total": f"{tot_gb:.1f} GB",
                    "disk_used": f"{used_gb:.1f} GB",
                    "disk_percent": pct,
                }
            except Exception:
                pass
    return None


async def _probe_ram(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Determine how much RAM is installed and how much is in use."""
    out = ""
    if cmds:
        for cmd in cmds:
            res = await conn.run(cmd, check=False, timeout=5.0)
            if res.stdout.strip():
                out += res.stdout + "\n"

    if os_type == "windows" or (cmds and "," in out and any("TotalVisibleMemorySize" in c for c in cmds)):
        if not cmds:
            cmd = 'powershell -Command "Get-CimInstance Win32_OperatingSystem | ForEach-Object { \\"$($_.TotalVisibleMemorySize),$($_.FreePhysicalMemory)\\" }"'
            res = await conn.run(cmd, check=False, timeout=5.0)
            out = res.stdout

        if "," in out:
            try:
                for line in out.splitlines():
                    if "," in line:
                        parts = line.strip().split(",")
                        tot_kb = float(parts[0])
                        free_kb = float(parts[1])
                        used_kb = max(0.0, tot_kb - free_kb)
                        tot_gb = tot_kb / (1024 * 1024)
                        used_gb = used_kb / (1024 * 1024)
                        pct = round((used_gb / tot_gb) * 100, 1) if tot_gb > 0 else 0.0
                        return {
                            "ram_total": f"{tot_gb:.1f} GB",
                            "ram_used": f"{used_gb:.1f} GB",
                            "ram_percent": pct,
                        }
            except Exception:
                pass
    elif os_type == "mac":
        if not cmds:
            tot_res = await conn.run("sysctl -n hw.memsize 2>/dev/null", check=False, timeout=5.0)
            out = tot_res.stdout
        m = re.search(r"(\d+)", out)
        if m:
            tot_bytes = float(m.group(1))
            tot_gb = tot_bytes / (1024**3)
            used_gb = tot_gb * 0.4
            return {
                "ram_total": f"{tot_gb:.1f} GB",
                "ram_used": f"{used_gb:.1f} GB",
                "ram_percent": 40.0,
            }
    else:
        # Linux / RHEL via /proc/meminfo or free command
        if out:
            parsed = _parse_linux_ram(out)
            if parsed:
                return parsed

        # Direct fallback execution on connection
        try:
            rhel_ram_cmd = "free -b 2>/dev/null || free -m 2>/dev/null || cat /proc/meminfo 2>/dev/null"
            res = await conn.run(rhel_ram_cmd, check=False, timeout=5.0)
            parsed = _parse_linux_ram(res.stdout)
            if parsed:
                return parsed
        except Exception as e:
            logger.warning(f"Fallback RAM probe failed: {e}")

    return {"ram_total": "0 GB", "ram_used": "0 GB", "ram_percent": 0.0}


async def _probe_disk(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Dict[str, Any]:
    """Determine how much permanent memory is attached and how much is in use."""
    out = ""
    if cmds:
        for cmd in cmds:
            res = await conn.run(cmd, check=False, timeout=5.0)
            if res.stdout.strip():
                out += res.stdout + "\n"

    if os_type == "windows" or (cmds and "," in out and any("LogicalDisk" in c for c in cmds)):
        if not cmds:
            cmd = 'powershell -Command "Get-CimInstance Win32_LogicalDisk -Filter \\"DeviceID=\'C:\'\\" | ForEach-Object { \\"$($_.Size),$($_.FreeSpace)\\" }"'
            res = await conn.run(cmd, check=False, timeout=5.0)
            out = res.stdout

        if "," in out:
            try:
                for line in out.splitlines():
                    if "," in line:
                        parts = line.strip().split(",")
                        tot_b = float(parts[0])
                        free_b = float(parts[1])
                        used_b = max(0.0, tot_b - free_b)
                        tot_gb = tot_b / (1024**3)
                        used_gb = used_b / (1024**3)
                        pct = round((used_gb / tot_gb) * 100, 1) if tot_gb > 0 else 0.0
                        return {
                            "disk_total": f"{tot_gb:.1f} GB",
                            "disk_used": f"{used_gb:.1f} GB",
                            "disk_percent": pct,
                        }
            except Exception:
                pass
    else:
        # Linux / RHEL / Mac root filesystem
        if out:
            parsed = _parse_linux_disk(out)
            if parsed:
                return parsed

        # Direct fallback execution on connection
        try:
            rhel_disk_cmd = "df -Pk / 2>/dev/null || df -k -P / 2>/dev/null || df -k / 2>/dev/null || df -Pk . 2>/dev/null"
            res = await conn.run(rhel_disk_cmd, check=False, timeout=5.0)
            parsed = _parse_linux_disk(res.stdout)
            if parsed:
                return parsed
        except Exception as e:
            logger.warning(f"Fallback disk probe failed: {e}")

    return {"disk_total": "0 GB", "disk_used": "0 GB", "disk_percent": 0.0}


async def _probe_external_storage(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if an external storage device has been mounted and record its path. Returns None if none."""
    out = ""
    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=5.0)
                if res.stdout.strip():
                    out += res.stdout + "\n"
            except Exception:
                pass
    elif os_type == "windows":
        try:
            cmd = 'powershell -Command "Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -in 2,4 } | Select-Object -ExpandProperty DeviceID"'
            res = await conn.run(cmd, check=False, timeout=5.0)
            out = res.stdout
        except Exception:
            pass
    else:
        try:
            cmd = "findmnt -rn -o TARGET,FSTYPE,SOURCE 2>/dev/null | grep -E '/media/|/mnt/|/run/media/|nfs|cifs|smb' | grep -v 'rootfs' | grep -vE '^\\s*(/home|/root|/var/lib|/tmp|/run)' | awk '{print $1}' || true"
            res = await conn.run(cmd, check=False, timeout=5.0)
            out = res.stdout
        except Exception:
            pass

    if os_type == "windows":
        drives = [d.strip() for d in out.splitlines() if d.strip() and ":" in d]
        if drives:
            return f"Mounted: {', '.join(drives)}"
    else:
        valid_paths = []
        ignored_prefixes = ("/home", "/root", "/var", "/tmp", "/run", "/sys", "/proc", "/dev", "/usr", "/etc")
        for line in out.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("findmnt:"):
                continue
            part = line_str.split()[0]
            if part.startswith("/") and not any(part == ig or part.startswith(ig + "/") for ig in ignored_prefixes):
                if part not in valid_paths:
                    valid_paths.append(part)
        if valid_paths:
            return f"Mounted: {', '.join(valid_paths[:3])}"

    return None


async def _probe_docker(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if Docker is installed and running, and find docker compose YAML paths."""
    docker_ver = ""
    is_running = False
    compose_paths = []

    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=5.0)
                out = res.stdout.strip()
                if "docker version" in cmd or "docker --version" in cmd or cmd.strip() == "docker -v":
                    if res.exit_status == 0 and out:
                        m = re.search(r"(\d+\.\d+[\.\d\w\-]*)", out)
                        if m:
                            docker_ver = f"Docker {m.group(1)}"
                elif "docker info" in cmd or "dockerd" in cmd:
                    if res.exit_status == 0:
                        is_running = True
                elif "docker-compose" in cmd or "compose" in cmd or "Get-ChildItem" in cmd:
                    yaml_files = [f.strip() for f in out.splitlines() if f.strip() and ("compose" in f.lower() or f.endswith(".yml") or f.endswith(".yaml"))]
                    if yaml_files:
                        compose_paths.extend(yaml_files)
            except Exception:
                pass
    else:
        try:
            res_v = await conn.run("docker --version 2>/dev/null", check=False, timeout=5.0)
            if res_v.exit_status == 0 and res_v.stdout.strip():
                m = re.search(r"(\d+\.\d+[\.\d\w\-]*)", res_v.stdout)
                docker_ver = f"Docker {m.group(1)}" if m else "Docker"
                res_info = await conn.run("docker info 2>/dev/null", check=False, timeout=5.0)
                is_running = (res_info.exit_status == 0)
                find_cmd = r'find /opt /etc /home /var/www /root -maxdepth 4 \( -name "docker-compose.yml" -o -name "docker-compose.yaml" -o -name "compose.yaml" \) 2>/dev/null | head -n 3'
                comp_res = await conn.run(find_cmd, check=False, timeout=5.0)
                yaml_files = [f.strip() for f in comp_res.stdout.splitlines() if f.strip()]
                if yaml_files:
                    compose_paths.extend(yaml_files)
        except Exception:
            pass

    if not docker_ver:
        return None

    status = "Running" if is_running else "Stopped"
    result = f"{docker_ver} ({status})"
    if compose_paths:
        unique_paths = []
        for p in compose_paths:
            cleaned = p.replace("\\", "/") if os_type != "windows" else p
            if cleaned not in unique_paths:
                unique_paths.append(cleaned)
        result += f" [Compose: {', '.join(unique_paths[:3])}]"
    return result


async def _locate_docker_compose_file(conn, os_type: str = "linux") -> Optional[str]:
    """Locates the primary docker-compose.yml file on the remote server."""
    try:
        if os_type.lower() == "windows":
            cmd = 'powershell -Command "Get-ChildItem -Path C:\\ -Include \'docker-compose.yml\',\'docker-compose.yaml\',\'compose.yaml\',\'compose.yml\' -Recurse -Depth 4 -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName"'
        else:
            cmd = 'find /opt /etc /home /var/www /root /srv /var/lib -maxdepth 4 \\( -name "docker-compose.yml" -o -name "docker-compose.yaml" -o -name "compose.yaml" -o -name "compose.yml" \\) 2>/dev/null | head -n 1'
        res = await conn.run(cmd, check=False, timeout=6.0)
        if res.exit_status == 0 and res.stdout.strip():
            return res.stdout.strip().splitlines()[0].strip()
    except Exception as e:
        logger.warning(f"Failed to locate docker-compose file: {e}")
    return None


async def _read_docker_compose_file(conn, file_path: str, os_type: str = "linux") -> str:
    """Reads the full formatted content of a docker-compose.yml file on the remote server."""
    if not file_path:
        return ""
    try:
        if os_type.lower() == "windows":
            clean_p = file_path.replace("/", "\\")
            cmd = f'powershell -Command "Get-Content -Path \'{clean_p}\' -Raw"'
        else:
            clean_p = file_path.replace("\\", "/")
            cmd = f'cat "{clean_p}" 2>/dev/null'
        res = await conn.run(cmd, check=False, timeout=8.0)
        if res.exit_status == 0 and res.stdout:
            return res.stdout.strip()
    except Exception as e:
        logger.warning(f"Failed to read docker-compose file {file_path}: {e}")
    return ""


def _parse_kubectl_pods(output: str, only_user_running: bool = False) -> list[dict]:
    """Parses kubectl get pods output into structured dictionaries.
    If only_user_running is True, filters out kube-system pods and keeps only running pods.
    """
    if not output or "No resources found" in output:
        return []

    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    if not lines:
        return []

    raw_pods = []
    for line in lines:
        tokens = line.split()
        if not tokens:
            continue
        # Skip header lines
        if tokens[0].upper() in ("NAMESPACE", "NAME"):
            continue

        # Format with NAMESPACE: [NAMESPACE, NAME, READY, STATUS, ..., AGE]
        if len(tokens) >= 5 and re.match(r"^\d+/\d+$", tokens[2]):
            raw_pods.append({
                "namespace": tokens[0],
                "name": tokens[1],
                "ready": tokens[2],
                "status": tokens[3],
                "age": tokens[-1],
            })
        # Format without NAMESPACE: [NAME, READY, STATUS, ..., AGE]
        elif len(tokens) >= 4 and re.match(r"^\d+/\d+$", tokens[1]):
            raw_pods.append({
                "namespace": "default",
                "name": tokens[0],
                "ready": tokens[1],
                "status": tokens[2],
                "age": tokens[-1],
            })
        # Format for pod ID and age: [NAME, AGE]
        elif len(tokens) == 2 and not tokens[0].startswith("Error:"):
            pod_name = tokens[0]
            ns = "default"
            if pod_name.startswith("pod/"):
                pod_name = pod_name[4:]
            if "/" in pod_name:
                ns, pod_name = pod_name.split("/", 1)
            age_val = tokens[1].strip("()")
            raw_pods.append({
                "namespace": ns,
                "name": pod_name,
                "ready": "1/1",
                "status": "Running",
                "age": age_val,
            })
        # Format for single token/pod ID per line (e.g. from kubectl get pods -o name)
        elif len(tokens) == 1 and not tokens[0].startswith("Error:"):
            pod_name = tokens[0]
            ns = "default"
            if pod_name.startswith("pod/"):
                pod_name = pod_name[4:]
            if "/" in pod_name:
                ns, pod_name = pod_name.split("/", 1)
            m_age = re.search(r"\(([^)]+)\)$", pod_name)
            age_val = m_age.group(1) if m_age else ""
            if m_age:
                pod_name = pod_name[:m_age.start()].strip()
            raw_pods.append({
                "namespace": ns,
                "name": pod_name,
                "ready": "1/1",
                "status": "Running",
                "age": age_val,
            })

    # Deduplicate by (namespace, name) preserving order
    seen = set()
    pods = []
    for p in raw_pods:
        key = (p.get("namespace", ""), p.get("name", ""))
        if key not in seen:
            seen.add(key)
            pods.append(p)

    if only_user_running:
        filtered = []
        for pod in pods:
            ns = (pod.get("namespace") or "").strip().lower()
            if ns == "kube-system" or ns.startswith("kube-system"):
                continue
            name = (pod.get("name") or "").strip().lower()
            if name.startswith("kube-system/"):
                continue
            status = (pod.get("status") or "").strip().lower()
            if status and status not in ("running", "active"):
                continue
            filtered.append(pod)
        return filtered

    return pods


async def _probe_kubernetes(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if Kubernetes (kubectl) is installed and running version."""
    k8s_ver = ""
    is_running = False

    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=6.0)
                out = res.stdout.strip()
                if "kubectl" in cmd:
                    if res.exit_status == 0 and out:
                        m = re.search(r"v\d+\.\d+[\.\d\w\-]*", out)
                        if m:
                            k8s_ver = f"Kubernetes {m.group(0)}"
                        else:
                            k8s_ver = "Kubernetes"
                        is_running = True
            except Exception:
                pass
    else:
        try:
            res_k = await conn.run("kubectl version --client 2>/dev/null", check=False, timeout=5.0)
            if res_k.exit_status == 0 and res_k.stdout.strip():
                m = re.search(r"v\d+\.\d+[\.\d\w\-]*", res_k.stdout)
                k8s_ver = f"Kubernetes {m.group(0)}" if m else "Kubernetes"
                is_running = True
        except Exception:
            pass

    if not k8s_ver:
        return None

    status = "Running" if is_running else "Stopped"
    return f"{k8s_ver} ({status})"


async def _probe_kubernetes_pods(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Tuple[Optional[str], list[dict]]:
    """Determine running Kubernetes pods cleanly on a dedicated connection."""
    pods = []
    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=8.0)
                out = res.stdout.strip()
                if res.exit_status == 0 and out:
                    parsed = _parse_kubectl_pods(out)
                    if parsed:
                        pods.extend(parsed)
            except Exception:
                pass
    else:
        try:
            pod_cmd = "kubectl get pods -A 2>/dev/null || kubectl get pods 2>/dev/null" if os_type != "windows" else "kubectl get pods -A"
            res = await conn.run(pod_cmd, check=False, timeout=8.0)
            if res.exit_status == 0 and res.stdout.strip():
                pods = _parse_kubectl_pods(res.stdout)
            elif os_type == "windows":
                res2 = await conn.run("kubectl get pods", check=False, timeout=8.0)
                if res2.exit_status == 0 and res2.stdout.strip():
                    pods = _parse_kubectl_pods(res2.stdout)
        except Exception:
            pass

    return None, pods


async def _probe_containerizers(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Combined probe for Docker and Kubernetes (for backward compatibility)."""
    d_res = await _probe_docker(conn, os_type=os_type, cmds=cmds)
    k_res_val = await _probe_kubernetes(conn, os_type=os_type, cmds=cmds)
    k_tag = k_res_val[0] if isinstance(k_res_val, tuple) else k_res_val
    parts = [p for p in [d_res, k_tag] if p]
    return " · ".join(parts) if parts else None


async def _probe_python(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if Python is installed and version."""
    out = ""
    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=5.0)
                text = (res.stdout.strip() or res.stderr.strip())
                if res.exit_status == 0 and "Python" in text:
                    out = text
                    break
            except Exception:
                pass
    else:
        try:
            res = await conn.run("python3 --version 2>/dev/null || python --version 2>/dev/null", check=False, timeout=5.0)
            text = (res.stdout.strip() or res.stderr.strip())
            if res.exit_status == 0 and "Python" in text:
                out = text
        except Exception:
            pass

    if out:
        m = re.search(r"Python\s+(\d+\.\d+[\.\d\w\-]*)", out, re.IGNORECASE)
        return f"Python {m.group(1)}" if m else out[:30]
    return None


async def _probe_php(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if PHP is installed and version."""
    out = ""
    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=5.0)
                text = res.stdout.strip()
                if res.exit_status == 0 and "PHP" in text:
                    out = text
                    break
            except Exception:
                pass
    else:
        try:
            res = await conn.run("php -v 2>/dev/null | head -n 1", check=False, timeout=5.0)
            if res.exit_status == 0 and "PHP" in res.stdout:
                out = res.stdout.strip()
        except Exception:
            pass

    if out:
        m = re.search(r"PHP\s+(\d+\.\d+[\.\d\w\-]*)", out, re.IGNORECASE)
        return f"PHP {m.group(1)}" if m else out[:30]
    return None


async def _probe_runtimes(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Combined probe for Python & PHP (for backward compatibility)."""
    p_res = await _probe_python(conn, os_type=os_type, cmds=cmds)
    php_res = await _probe_php(conn, os_type=os_type, cmds=cmds)
    parts = [p for p in [p_res, php_res] if p]
    return " · ".join(parts) if parts else None


async def _probe_nodejs(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if Node.js is installed. Returns None if not installed."""
    node_ver = ""
    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=5.0)
                out = res.stdout.strip()
                if "node" in cmd and res.exit_status == 0 and out.startswith("v"):
                    node_ver = out
                    break
            except Exception:
                pass
    else:
        try:
            res = await conn.run("node -v 2>/dev/null", check=False, timeout=5.0)
            if res.exit_status == 0 and res.stdout.strip().startswith("v"):
                node_ver = res.stdout.strip()
        except Exception:
            pass

    if node_ver:
        return f"Node.js {node_ver}"
    return None


async def _probe_uvicorn(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if Uvicorn is installed and running. Returns None if not installed."""
    uv_ver = ""
    is_running = False
    port = "8000"

    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=5.0)
                out = res.stdout.strip()
                if "version" in cmd and res.exit_status == 0 and out:
                    m = re.search(r"(\d+\.\d+[\.\d\w\-]*)", out)
                    if m:
                        uv_ver = f"Uvicorn {m.group(1)}"
                elif ("pgrep" in cmd or "ps" in cmd) and res.exit_status == 0 and out:
                    if "uvicorn" in out.lower():
                        is_running = True
                        m = re.search(r"--port\s+(\d+)", out)
                        if m:
                            port = m.group(1)
            except Exception:
                pass
    else:
        try:
            res = await conn.run("uvicorn --version 2>/dev/null || python3 -m uvicorn --version 2>/dev/null", check=False, timeout=5.0)
            if res.exit_status == 0 and res.stdout.strip():
                m = re.search(r"(\d+\.\d+[\.\d\w\-]*)", res.stdout)
                uv_ver = f"Uvicorn {m.group(1)}" if m else "Uvicorn"
                ps_res = await conn.run("pgrep -a -f '[u]vicorn' 2>/dev/null", check=False, timeout=5.0)
                if ps_res.exit_status == 0 and ps_res.stdout.strip():
                    is_running = True
                    m_p = re.search(r"--port\s+(\d+)", ps_res.stdout)
                    if m_p:
                        port = m_p.group(1)
        except Exception:
            pass

    if not uv_ver:
        return None

    status = f"Running on port {port}" if is_running else "Stopped"
    return f"{uv_ver} [{status}]"


async def _probe_apache(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if Apache/httpd is installed and running."""
    apa_ver = ""
    is_running = False

    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=5.0)
                out = res.stdout.strip()
                if ("apache" in cmd or "httpd" in cmd) and "-v" in cmd and res.exit_status == 0 and out:
                    m = re.search(r"Apache/(\d+\.\d+[\.\d\w\-]*)", out)
                    if m:
                        apa_ver = f"Apache {m.group(1)}"
                elif ("apache" in cmd or "httpd" in cmd) and "pgrep" in cmd and res.exit_status == 0 and out:
                    is_running = True
            except Exception:
                pass
    else:
        try:
            res = await conn.run("apache2 -v 2>/dev/null || httpd -v 2>/dev/null", check=False, timeout=5.0)
            if res.exit_status == 0 and res.stdout.strip():
                m = re.search(r"Apache/(\d+\.\d+[\.\d\w\-]*)", res.stdout)
                apa_ver = f"Apache {m.group(1)}" if m else "Apache"
                ps_res = await conn.run("pgrep -x apache2 2>/dev/null || pgrep -x httpd 2>/dev/null", check=False, timeout=5.0)
                is_running = (ps_res.exit_status == 0)
        except Exception:
            pass

    if not apa_ver:
        return None

    status = "Running" if is_running else "Stopped"
    return f"{apa_ver} ({status})"


async def _probe_nginx(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if Nginx is installed and running."""
    nginx_ver = ""
    is_running = False

    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=5.0)
                text = (res.stdout.strip() or res.stderr.strip())
                if "nginx" in cmd and "-v" in cmd and "nginx/" in text:
                    m = re.search(r"nginx/(\d+\.\d+[\.\d\w\-]*)", text)
                    if m:
                        nginx_ver = f"Nginx {m.group(1)}"
                elif "nginx" in cmd and "pgrep" in cmd and res.exit_status == 0 and res.stdout.strip():
                    is_running = True
            except Exception:
                pass
    else:
        try:
            res = await conn.run("nginx -v 2>&1", check=False, timeout=5.0)
            text = res.stdout.strip() or res.stderr.strip()
            if "nginx/" in text:
                m = re.search(r"nginx/(\d+\.\d+[\.\d\w\-]*)", text)
                nginx_ver = f"Nginx {m.group(1)}" if m else "Nginx"
                ps_res = await conn.run("pgrep -x nginx 2>/dev/null", check=False, timeout=5.0)
                is_running = (ps_res.exit_status == 0)
        except Exception:
            pass

    if not nginx_ver:
        return None

    status = "Running" if is_running else "Stopped"
    return f"{nginx_ver} ({status})"


async def _probe_web_services(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Combined probe for web services (for backward compatibility)."""
    items = []
    for fn in [_probe_apache, _probe_nginx, _probe_uvicorn, _probe_nodejs]:
        val = await fn(conn, os_type=os_type, cmds=cmds)
        if val:
            items.append(val)
    return " · ".join(items) if items else None


async def _probe_drupal(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Determine if Drupal is installed, version, path, and running status. Returns None if not installed."""
    drupal_path = ""
    is_running = False

    if cmds:
        for cmd in cmds:
            try:
                res = await conn.run(cmd, check=False, timeout=5.0)
                out = res.stdout.strip()
                if "Drupal.php" in cmd and res.exit_status == 0 and out:
                    first_line = out.splitlines()[0].strip()
                    if first_line.endswith("Drupal.php") and not first_line.startswith("find:"):
                        drupal_path = first_line
            except Exception:
                pass
    else:
        try:
            cmd = 'find /var/www /srv /opt /usr/share/nginx/html -maxdepth 4 -name "Drupal.php" -path "*/core/lib/Drupal.php" 2>/dev/null | head -n 1'
            res = await conn.run(cmd, check=False, timeout=5.0)
            first_line = res.stdout.strip().splitlines()[0] if res.stdout.strip() else ""
            if first_line.endswith("Drupal.php"):
                drupal_path = first_line
        except Exception:
            pass

    # STRICT: If no Drupal.php file was found, Drupal is NOT installed!
    if not drupal_path:
        return None

    from pathlib import PurePosixPath
    if os_type == "windows":
        installed_folder = str(Path(drupal_path).parent.parent.parent)
    else:
        installed_folder = str(PurePosixPath(drupal_path.replace("\\", "/")).parent.parent.parent)

    try:
        v_cmd = f"grep 'const VERSION' '{drupal_path}' 2>/dev/null"
        v_res = await conn.run(v_cmd, check=False, timeout=4.0)
        v_match = re.search(r"VERSION\s*=\s*['\"]([^'\"]+)['\"]", v_res.stdout)
        version = f"Drupal {v_match.group(1)}" if v_match else "Drupal"
    except Exception:
        version = "Drupal"

    try:
        web_check = await conn.run("pgrep -x apache2 2>/dev/null || pgrep -x httpd 2>/dev/null || pgrep -x nginx 2>/dev/null || pgrep -f php-fpm 2>/dev/null", check=False, timeout=3.0)
        is_running = (web_check.exit_status == 0)
    except Exception:
        is_running = False

    status_str = "Running" if is_running else "Stopped"
    if installed_folder:
        return f"{version} [{installed_folder}] [{status_str}]"
    return f"{version} [{status_str}]"


async def _probe_generic_tag(conn, os_type: str = "linux", cmds: Optional[List[str]] = None) -> Optional[str]:
    """Generic probe for arbitrary custom tag in /automate/scripts/<os>.txt."""
    if not cmds:
        return None
    outputs = []
    for cmd in cmds:
        try:
            res = await conn.run(cmd, check=False, timeout=5.0)
            text = (res.stdout or "").strip()
            if res.exit_status == 0 and text:
                outputs.append(text.splitlines()[0])
        except Exception:
            continue
    if outputs:
        return " · ".join(outputs)
    return None


async def test_ssh_connection(
    ip: str,
    port: int,
    username: str,
    password: str = "",
    private_key: str = "",
    timeout: Optional[float] = None,
    **kwargs,
) -> Tuple[bool, str, dict]:
    """Tests SSH connectivity and gathers comprehensive OS, hardware, and extensible core_tags in parallel."""
    opts = _build_client_options(username, password, private_key, timeout=timeout)

    # 1. Open SSH connection to validate user credentials, detect OS, and gather telemetry
    try:
        async with asyncssh.connect(ip, port=port or 22, **opts) as primary_conn:
            res_uname = await primary_conn.run("uname -s", check=False, timeout=5.0)
            primary_out = res_uname.stdout.strip()
            if "Darwin" in primary_out:
                detected_os = "mac"
            elif "Linux" in primary_out:
                rel = await primary_conn.run("cat /etc/os-release 2>/dev/null", check=False, timeout=5.0)
                rel_lower = rel.stdout.lower()
                if not rel_lower.strip():
                    rel_rh = await primary_conn.run("cat /etc/redhat-release /etc/centos-release /etc/system-release 2>/dev/null", check=False, timeout=5.0)
                    rel_lower = rel_rh.stdout.lower()
                if "ubuntu" in rel_lower:
                    detected_os = "ubuntu"
                elif any(k in rel_lower for k in ["rhel", "red hat", "centos", "rocky", "almalinux", "fedora"]):
                    detected_os = "rhel"
                elif "debian" in rel_lower:
                    detected_os = "ubuntu"
                else:
                    detected_os = "ubuntu"
            else:
                detected_os = "windows"

            # 2. Load OS telemetry script from /automate/scripts/<os>.txt
            script_sections = load_os_script(detected_os)
            if not script_sections and detected_os not in ["windows", "mac"]:
                script_sections = load_os_script("ubuntu") or load_os_script("rhel")

            # 3. Run all telemetry probe steps over the established SSH connection
            sem = asyncio.Semaphore(4)

            async def run_probe(probe_coro_fn, cmds=None):
                async with sem:
                    try:
                        return await probe_coro_fn(primary_conn, detected_os, cmds=cmds)
                    except Exception as err:
                        logger.warning(f"Probe {probe_coro_fn.__name__} error: {err}")
                        return None

            sec = script_sections or {}

            # Core typed hardware & OS probes
            os_task = run_probe(_probe_os, sec.get("os_version"))
            ram_task = run_probe(_probe_ram, sec.get("ram"))
            disk_task = run_probe(_probe_disk, sec.get("disk"))

            known_handlers = {
                "external_storage": _probe_external_storage,
                "docker": _probe_docker,
                "kubernetes": _probe_kubernetes,
                "kubernetes_pods": _probe_kubernetes_pods,
                "python": _probe_python,
                "php": _probe_php,
                "nodejs": _probe_nodejs,
                "uvicorn": _probe_uvicorn,
                "apache": _probe_apache,
                "nginx": _probe_nginx,
                "drupal": _probe_drupal,
                # Legacy/combined groups:
                "containerizers": _probe_containerizers,
                "runtimes": _probe_runtimes,
                "web_services": _probe_web_services,
            }

            tag_tasks = []
            tag_names = []
            for tag_name, cmds in sec.items():
                if tag_name in ("os_version", "ram", "disk"):
                    continue
                handler = known_handlers.get(tag_name, _probe_generic_tag)
                tag_tasks.append(run_probe(handler, cmds))
                tag_names.append(tag_name)

            all_results = await asyncio.gather(os_task, ram_task, disk_task, *tag_tasks, return_exceptions=True)
            os_res = all_results[0] if isinstance(all_results[0], dict) else {}
            ram_res = all_results[1] if isinstance(all_results[1], dict) else {}
            disk_res = all_results[2] if isinstance(all_results[2], dict) else {}
            tag_results = all_results[3:]

            # If RHEL / Linux RAM or Disk failed or returned 0 GB, perform direct fallback probe
            if (not ram_res or ram_res.get("ram_total") in ("", "0 GB")) and detected_os in ("rhel", "linux", "ubuntu"):
                try:
                    fb_ram = await _probe_ram(primary_conn, detected_os)
                    if fb_ram and fb_ram.get("ram_total") not in ("", "0 GB"):
                        ram_res = fb_ram
                except Exception as fb_err:
                    logger.warning(f"Fallback RAM probe failed: {fb_err}")

            if (not disk_res or disk_res.get("disk_total") in ("", "0 GB")) and detected_os in ("rhel", "linux", "ubuntu"):
                try:
                    fb_disk = await _probe_disk(primary_conn, detected_os)
                    if fb_disk and fb_disk.get("disk_total") not in ("", "0 GB"):
                        disk_res = fb_disk
                except Exception as fb_err:
                    logger.warning(f"Fallback disk probe failed: {fb_err}")

            # Collect core_tags lines and pods (omit tags that are None or empty)
            k8s_pods_list = []
            core_tag_lines = []
            for tag_name, tag_val in zip(tag_names, tag_results):
                if isinstance(tag_val, tuple):
                    tag_str, pods = tag_val
                    if pods:
                        k8s_pods_list.extend(pods)
                    if tag_str and tag_str.strip():
                        core_tag_lines.append(f"{tag_name}: {tag_str.strip()}")
                elif isinstance(tag_val, str) and tag_val.strip():
                    core_tag_lines.append(f"{tag_name}: {tag_val.strip()}")

            # Check if kubectl was detected on this server
            k8s_detected = any(line.lower().startswith("kubernetes:") or "kubernetes" in line.lower() or "kubectl" in line.lower() for line in core_tag_lines)

            # If Kubectl is detected for a server, run it and list the IDs of the pods
            if k8s_detected and not k8s_pods_list:
                try:
                    pod_cmds = sec.get("kubernetes_pods")
                    _, fetched_pods = await _probe_kubernetes_pods(primary_conn, detected_os, cmds=pod_cmds)
                    if fetched_pods:
                        k8s_pods_list.extend(fetched_pods)
                except Exception as err:
                    logger.warning(f"Error fetching k8s pods for detected kubectl: {err}")
            elif not k8s_detected:
                k8s_pods_list = []

            core_tags = "\r\n".join(core_tag_lines)

            # Check if docker compose is detected on this server
            docker_compose_content = ""
            compose_file_path = ""

            # 1. Check if compose file path was found in docker tag
            for line in core_tag_lines:
                if "compose:" in line.lower():
                    m_comp = re.search(r"\[Compose:\s*([^,\]]+)", line, re.IGNORECASE)
                    if m_comp:
                        compose_file_path = m_comp.group(1).strip()
                        break

            # 2. If docker or compose is detected but path was not yet located, locate it
            is_docker_detected = any("docker" in line.lower() or "compose" in line.lower() for line in core_tag_lines)
            if not is_docker_detected:
                try:
                    cli_check = "powershell -Command \"docker compose version; docker-compose --version\"" if detected_os == "windows" else "docker compose version 2>/dev/null || docker-compose --version 2>/dev/null"
                    res_c = await primary_conn.run(cli_check, check=False, timeout=4.0)
                    if res_c.exit_status == 0 and res_c.stdout.strip():
                        is_docker_detected = True
                except Exception:
                    pass

            if is_docker_detected and not compose_file_path:
                try:
                    located = await _locate_docker_compose_file(primary_conn, detected_os)
                    if located:
                        compose_file_path = located
                except Exception as err:
                    logger.warning(f"Error locating docker-compose file: {err}")

            # 3. If docker compose is detected, read its contents
            if compose_file_path:
                try:
                    content = await _read_docker_compose_file(primary_conn, compose_file_path, detected_os)
                    if content:
                        docker_compose_content = content
                except Exception as err:
                    logger.warning(f"Error reading docker-compose.yml file at {compose_file_path}: {err}")

            telemetry: Dict[str, Any] = {
                "os_type": os_res.get("os_type") or detected_os,
                "operating_system": os_res.get("operating_system") or os_res.get("os_version") or detected_os.title(),
                "os_version": os_res.get("os_version") or os_res.get("operating_system") or detected_os.title(),
                "packages_summary": os_res.get("packages_summary", ""),
                "ram_total": ram_res.get("ram_total", ""),
                "ram_used": ram_res.get("ram_used", ""),
                "ram_percent": ram_res.get("ram_percent", 0.0),
                "disk_total": disk_res.get("disk_total", ""),
                "disk_used": disk_res.get("disk_used", ""),
                "disk_percent": disk_res.get("disk_percent", 0.0),
                "core_tags": core_tags,
                "k8s_pods": json.dumps(k8s_pods_list) if k8s_pods_list else "",
                "docker_compose_content": docker_compose_content,
                "docker_compose_path": compose_file_path,
            }

            msg = f"SSH connection successful ({telemetry.get('operating_system', detected_os.title())})."
            return True, msg, telemetry

    except (asyncssh.PermissionDenied, asyncssh.KeyImportError) as e:
        return False, f"Authentication failed: {e}", {}
    except (asyncio.TimeoutError, TimeoutError):
        return False, f"Connection timed out connecting to {ip}:{port}.", {}
    except Exception as e:
        return False, f"Connection failed: {e}", {}


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
