"""GOWA process lifecycle manager — configuration, PID history, startup, and teardown."""

import atexit
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("mizalla.gowa_manager")

PID_HISTORY_FILENAME = "gowa_pids.json"
MAX_PID_HISTORY = 5

_gowa_process: subprocess.Popen | None = None
_last_instance_path: Path | None = None
_shutdown_registered: bool = False
_lock = threading.Lock()


def get_pid_file_path(instance_path: str | Path) -> Path:
    """Return the absolute path to the gowa_pids.json file."""
    return Path(instance_path) / PID_HISTORY_FILENAME


def load_pid_history(instance_path: str | Path) -> dict[str, Any]:
    """Read the recorded GOWA PIDs from instance/gowa_pids.json.

    Returns a dict with 'current_pid' (int or None) and 'history' (list of ints).
    """
    file_path = get_pid_file_path(instance_path)
    default_state = {"current_pid": None, "history": []}
    if not file_path.is_file():
        return default_state

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_state
        current_pid = data.get("current_pid")
        history = data.get("history", [])
        if not isinstance(history, list):
            history = []
        # Filter integers
        clean_history = [int(p) for p in history if isinstance(p, (int, str)) and str(p).isdigit()]
        clean_current = int(current_pid) if current_pid and str(current_pid).isdigit() else None
        return {
            "current_pid": clean_current,
            "history": clean_history[:MAX_PID_HISTORY],
        }
    except Exception as exc:
        logger.warning("Could not read PID history from %s: %s", file_path, exc)
        return default_state


def save_pid_history(
    instance_path: str | Path,
    pid: int | None,
    mark_current: bool = True,
) -> None:
    """Record a PID into instance/gowa_pids.json, keeping at most MAX_PID_HISTORY entries."""
    file_path = get_pid_file_path(instance_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    state = load_pid_history(instance_path)
    history: list[int] = state.get("history", [])

    if pid is not None and pid > 0:
        # Prepend new pid, remove duplicates, cap at MAX_PID_HISTORY
        new_history = [pid] + [p for p in history if p != pid]
        history = new_history[:MAX_PID_HISTORY]
        if mark_current:
            state["current_pid"] = pid
    elif mark_current:
        state["current_pid"] = None

    state["history"] = history

    temp_path = file_path.with_suffix(".tmp")
    try:
        temp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        temp_path.replace(file_path)
    except Exception as exc:
        logger.warning("Failed to save PID history to %s: %s", file_path, exc)
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


def clear_current_pid(instance_path: str | Path) -> None:
    """Mark current_pid as None while retaining PID history."""
    save_pid_history(instance_path, pid=None, mark_current=True)


def is_pid_running(pid: int | None) -> bool:
    """Check whether a process with the given PID is currently active."""
    if pid is None or pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            # 0x1000 = PROCESS_QUERY_LIMITED_INFORMATION
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            exit_code = wintypes.DWORD()
            success = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            # 259 = STILL_ACTIVE
            return bool(success and exit_code.value == 259)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def is_port_bound(port: int, host: str = "127.0.0.1") -> bool:
    """Check if the specified TCP port is currently open and bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def terminate_pid(pid: int | None, timeout: float = 3.0) -> bool:
    """Terminate a process by PID gracefully, falling back to forceful kill if needed."""
    if pid is None or not is_pid_running(pid):
        return True

    logger.info("Terminating process PID %s (timeout: %ss)...", pid, timeout)
    try:
        if sys.platform == "win32":
            # Graceful attempt with taskkill
            subprocess.run(
                ["taskkill", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=5,
            )
            start_time = time.time()
            while time.time() - start_time < timeout:
                if not is_pid_running(pid):
                    return True
                time.sleep(0.15)

            # Forceful attempt
            if is_pid_running(pid):
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
        else:
            os.kill(pid, signal.SIGTERM)
            start_time = time.time()
            while time.time() - start_time < timeout:
                if not is_pid_running(pid):
                    return True
                time.sleep(0.15)

            if is_pid_running(pid):
                os.kill(pid, signal.SIGKILL)
    except Exception as exc:
        logger.warning("Error during termination of PID %s: %s", pid, exc)

    return not is_pid_running(pid)


def cleanup_old_pids(instance_path: str | Path) -> list[int]:
    """Clean up any active processes recorded in the PID history."""
    state = load_pid_history(instance_path)
    terminated: list[int] = []
    history: list[int] = state.get("history", [])

    for pid in history:
        if is_pid_running(pid):
            logger.info("Found active orphaned GOWA process from history: PID %s. Terminating...", pid)
            if terminate_pid(pid):
                terminated.append(pid)

    return terminated


def _resolve_executable_path(executable: str, root_path: Path | None = None) -> Path:
    """Resolve executable path, handling relative paths and root-relative paths cleanly."""
    clean = executable.strip()
    p = Path(clean)
    if p.is_absolute() and p.exists():
        return p

    # If it starts with / or \, try resolving against root_path if provided
    if (clean.startswith("/") or clean.startswith("\\")) and root_path:
        rel = clean.lstrip("/\\")
        candidate = root_path / rel
        if candidate.exists():
            return candidate

    if root_path:
        candidate = root_path / clean
        if candidate.exists():
            return candidate

    # Return as Path object for caller
    return p


def build_command_line(executable_path: Path | str, port: int) -> list[str]:
    """Build the command list to spawn GOWA.

    GOWA (go-whatsapp-web-multidevice) uses Cobra CLI with the 'rest' subcommand
    to launch the REST and MCP HTTP daemon. Running without 'rest' prints the
    help message and exits immediately.
    """
    p_str = str(executable_path)
    is_shell_script = p_str.lower().endswith(".sh")

    if is_shell_script:
        return ["sh", p_str, "--port", str(port)]

    file_name = Path(p_str).name.lower()
    if "gowa" in file_name:
        return [p_str, "rest", "--port", str(port)]

    return [p_str, "--port", str(port)]



def register_shutdown_hooks(instance_path: Path) -> None:
    """Register atexit and signal handlers once for the current process."""
    global _shutdown_registered, _last_instance_path
    if _shutdown_registered:
        return
    _shutdown_registered = True
    _last_instance_path = instance_path

    def _shutdown_handler(*args: Any) -> None:
        if _gowa_process is not None:
            stop_gowa_service(_last_instance_path)

    atexit.register(_shutdown_handler)

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                prev_handler = signal.getsignal(sig)

                def _signal_wrapper(signum: int, frame: Any, prev=prev_handler) -> None:
                    _shutdown_handler()
                    if callable(prev) and prev not in (signal.SIG_IGN, signal.SIG_DFL):
                        prev(signum, frame)
                    elif prev == signal.SIG_DFL:
                        sys.exit(0)

                signal.signal(sig, _signal_wrapper)
            except (ValueError, AttributeError):
                # Signals cannot be hooked in certain embedded environments or worker threads
                pass


def start_gowa_service(app) -> bool:
    """Spawn the GOWA service if MANAGE_GOWA is enabled, after terminating old instances.

    Returns True if started or already running, False otherwise.
    """
    global _gowa_process, _last_instance_path
    with _lock:
        if not app.config.get("MANAGE_GOWA"):
            return False

        instance_path = Path(app.instance_path)
        _last_instance_path = instance_path
        instance_path.mkdir(parents=True, exist_ok=True)

        # Step 1: Cleanup old PIDs recorded in PID history
        cleanup_old_pids(instance_path)

        # Step 2: Register shutdown handlers
        register_shutdown_hooks(instance_path)

        executable_setting = app.config.get("GOWA_EXECUTABLE", "/gowa/gowa.exe")
        port = int(app.config.get("GOWA_PORT", 5000))
        root_path = Path(app.root_path).parent

        resolved_exec = _resolve_executable_path(executable_setting, root_path=root_path)

        # If executable doesn't exist on disk, log a clear warning and don't fail boot
        if not resolved_exec.exists():
            logger.warning(
                "GOWA executable '%s' was not found (resolved to: %s). GOWA was not started.",
                executable_setting,
                resolved_exec,
            )
            clear_current_pid(instance_path)
            return False

        # Step 3: Build command line and environment
        cmd = build_command_line(resolved_exec, port)
        env = os.environ.copy()
        env["PORT"] = str(port)

        log_file_path = instance_path / "gowa.log"
        try:
            log_file = open(log_file_path, "a", encoding="utf-8")
        except Exception:
            log_file = open(log_file_path, "a")

        try:
            logger.info("Spawning GOWA service: %s (PORT=%s)...", " ".join(cmd), port)
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(resolved_exec.parent if resolved_exec.parent.exists() else root_path),
            )
            _gowa_process = proc
            save_pid_history(instance_path, proc.pid, mark_current=True)
            logger.info("GOWA service started successfully with PID %s.", proc.pid)
            return True
        except Exception as exc:
            logger.error("Failed to spawn GOWA service: %s", exc)
            clear_current_pid(instance_path)
            return False


def stop_gowa_service(instance_path: str | Path | None = None) -> None:
    """Terminate the managed GOWA process and clear current PID tracking."""
    global _gowa_process, _last_instance_path
    with _lock:
        target_path = instance_path or _last_instance_path

        if _gowa_process is not None:
            pid = _gowa_process.pid
            try:
                if _gowa_process.poll() is None:
                    logger.info("Stopping GOWA service process PID %s...", pid)
                    terminate_pid(pid, timeout=2.0)
                    try:
                        _gowa_process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
            except Exception as exc:
                logger.warning("Error stopping GOWA subprocess: %s", exc)
            finally:
                _gowa_process = None

        if target_path:
            state = load_pid_history(target_path)
            curr = state.get("current_pid")
            if curr and is_pid_running(curr):
                terminate_pid(curr, timeout=2.0)
            clear_current_pid(target_path)


def get_gowa_process_status(app=None) -> dict[str, Any]:
    """Retrieve the current management and execution status of GOWA."""
    global _gowa_process, _last_instance_path
    from flask import current_app

    effective_app = app or (current_app if current_app else None)
    if not effective_app:
        return {
            "managed": False,
            "running": False,
            "pid": None,
            "port": 5000,
            "executable": "/gowa/gowa.exe",
            "log_file": "",
            "history": [],
        }

    managed = bool(effective_app.config.get("MANAGE_GOWA", False))
    port = int(effective_app.config.get("GOWA_PORT", 5000))
    executable = str(effective_app.config.get("GOWA_EXECUTABLE", "/gowa/gowa.exe"))
    instance_path = Path(effective_app.instance_path)
    state = load_pid_history(instance_path)

    current_pid = state.get("current_pid")
    running = is_pid_running(current_pid)

    # If recorded current_pid is no longer alive, check _gowa_process
    if not running and _gowa_process is not None:
        running = _gowa_process.poll() is None
        if running:
            current_pid = _gowa_process.pid

    log_path = instance_path / "gowa.log"

    return {
        "managed": managed,
        "running": running,
        "pid": current_pid if running else None,
        "port": port,
        "executable": executable,
        "log_file": str(log_path),
        "history": state.get("history", []),
    }
