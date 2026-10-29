# Plan: GOWA Lifecycle Management (Configuration, PID History & Auto-Lifecycle)

## Goal
Implement automated lifecycle management for the GOWA (WhatsApp Messaging App) service within Mizalla, controlled via three environment/app configuration variables:
- `GOWA_EXECUTABLE`: Path to the GOWA executable or shell script (default: `/gowa/gowa.exe`).
- `GOWA_PORT`: Port on which GOWA should run and listen (default: `5000`).
- `MANAGE_GOWA`: Boolean flag (`True` / `False`, default: `False`). When enabled, Mizalla automatically starts GOWA on `GOWA_PORT`, tracks the last 5 PIDs in a persistent state file, cleans up / restarts any previously matching GOWA processes on startup, and shuts down the process when Mizalla exits.

---

## Architecture & Design Decisions

### 1. Configuration & Defaults
- **Variables in `.env` / `app.config`**:
  - `MANAGE_GOWA`: Parsed as boolean (e.g., `os.environ.get("MANAGE_GOWA", "false").lower() in ("true", "1", "yes")`).
  - `GOWA_EXECUTABLE`: Defaults to `/gowa/gowa.exe`. Supports both absolute and relative paths (e.g., `./gowa/gowa.exe`, `C:\gowa\gowa.exe`, or `/gowa/run.sh`).
  - `GOWA_PORT`: Defaults to `5000`. Parsed as `int`.
- **Base URL Synchronization**:
  - In `mizalla/gowa.py`, `get_mcp_base_url()` currently defaults to `http://127.0.0.1:3000`. When `MANAGE_GOWA` is enabled, the default base URL automatically matches `http://127.0.0.1:{GOWA_PORT}` unless an explicit override is set in `AppSetting`.

### 2. PID Tracking & History Persistence
- **Storage Location**: `instance/gowa_pids.json` (inside Flask's instance directory, kept out of git tracking).
- **History Structure**:
  ```json
  {
    "current_pid": 12345,
    "history": [12345, 12200, 11950, 10500, 9800]
  }
  ```
- **Cap**: Maximum of 5 PIDs stored. When a new PID is added, the list retains the newest 5.

### 3. Startup & Process Clean-up ("On Self Start")
- **Flask Reloader Guard**:
  - In development (`flask run --debug`), Werkzeug uses a two-process architecture.
  - Startup logic will only trigger when running in the primary worker (`WERKZEUG_RUN_MAIN == "true"` in reloader mode, or when `WERKZEUG_RUN_MAIN` is unset in non-reloader / production mode).
- **Orphan Process Cleanup**:
  - On startup, read the recorded PIDs from `instance/gowa_pids.json`.
  - For each PID in the last 5 PIDs:
    - Check if the process is currently active.
    - If active, verify that it corresponds to the GOWA executable or is listening on `GOWA_PORT`.
    - Terminate it gracefully (`SIGTERM`), wait up to 3 seconds, and fallback to forceful termination (`SIGKILL` or `taskkill /F /PID`) if still running.
- **Port Availability Verification**:
  - Check whether `GOWA_PORT` is still bound. If bound by a lingering process, log an alert.
- **Spawning GOWA**:
  - Determine execution strategy:
    - If file extension is `.sh`:
      - On Windows: invoke via `sh` or `bash` if available, or direct command.
      - On Unix: invoke with `sh` or execute directly if executable bit is set.
    - If `.exe` or native binary: invoke directly.
  - Arguments & Environment:
    - Pass `PORT=str(GOWA_PORT)` in process environment.
    - Pass `--port {GOWA_PORT}` in command-line arguments.
  - Standard Output / Error:
    - Redirect stdout and stderr to `instance/gowa.log` (rotating or appending) to keep Flask output clean and allow debugging if GOWA crashes.
  - Record the newly spawned PID into `instance/gowa_pids.json`.

### 4. Application Teardown ("On Application Close")
- Register shutdown handlers using:
  - `atexit.register(stop_gowa)`
  - Signal handlers for `SIGINT` and `SIGTERM`
- Shutdown logic:
  - Send `SIGTERM` to the active GOWA process.
  - Wait briefly for graceful exit.
  - If still active after timeout, issue forceful termination.
  - Update `instance/gowa_pids.json` marking `current_pid` as `null`.

### 5. Dedicated Module: `mizalla/gowa_manager.py`
To avoid bloating `__init__.py` or mixing process management into the HTTP client `gowa.py`, create a dedicated lifecycle manager module `mizalla/gowa_manager.py` exposing:
- `start_gowa(app)`
- `stop_gowa()`
- `get_gowa_process_status()` -> dict with `running`, `pid`, `port`, `executable`
- `cleanup_old_pids(instance_path)`

---

## Detailed Implementation Tasks

### Task 1: Create `mizalla/gowa_manager.py`
- [x] Implement PID storage helpers: `load_pid_history(instance_path)`, `save_pid_history(instance_path, pid)`.
- [x] Implement process safety checks: `is_pid_running(pid)`, `terminate_pid(pid, timeout=3)`.
- [x] Implement `start_gowa_service(app)`:
  - Check `MANAGE_GOWA` config.
  - Clean up matching PIDs from last 5 PIDs in history.
  - Build `Popen` command and environment.
  - Spawn process, save PID to history.
  - Register `atexit` and `signal` hooks.
- [x] Implement `stop_gowa_service()`.
- [x] Implement `get_gowa_process_status()`.

### Task 2: Configure Flask App Integration (`mizalla/__init__.py`)
- [x] In `create_app()`:
  - Load config defaults:
    - `GOWA_EXECUTABLE = os.environ.get("GOWA_EXECUTABLE", "/gowa/gowa.exe").strip()`
    - `GOWA_PORT = int(os.environ.get("GOWA_PORT", "5000"))`
    - `MANAGE_GOWA = os.environ.get("MANAGE_GOWA", "false").lower() in ("true", "1", "yes")`
  - In app initialization (after instance path is ensured):
    - Check reloader guard (`os.environ.get("WERKZEUG_RUN_MAIN") in (None, "true")`).
    - If `app.config["MANAGE_GOWA"]`, invoke `gowa_manager.start_gowa_service(app)`.

### Task 3: Align GOWA Client Defaults (`mizalla/gowa.py`)
- [x] Update `get_mcp_base_url()`:
  - If `current_app` is available and `MANAGE_GOWA` is True, default to `http://127.0.0.1:{GOWA_PORT}` when `gowa_mcp_base_url` is not customized in `AppSetting`.

### Task 4: Documentation & `.env.example` / Admin UI Visibility
- [x] Document the new variables in `.env`.
- [x] Show GOWA manager status card on the Admin WhatsApp devices view (`whatsapp_devices.html`).

### Task 5: Comprehensive Unit & Lifecycle Tests
- [x] Create `tests/test_gowa_lifecycle.py`:
  - Test PID history file reads, writes, and caps at 5 PIDs.
  - Test termination of mock running PIDs from history on self-start.
  - Test start command construction for both `.exe` and `.sh` scripts.
  - Test port argument and environment variable propagation (`PORT=5000`).
  - Test graceful shutdown handler execution on application exit.
  - Test Werkzeug reloader guard logic (no double-spawn).

---

## Verification Plan
1. **Automated Unit & Integration Tests**:
   - [x] Run `pytest tests/test_gowa_lifecycle.py` and ensure 100% pass rate (13/13 passed).
   - [x] Run existing suite `pytest tests/test_whatsapp_devices.py` and `pytest tests/test_gowa_health.py` to ensure zero regressions (24/24 passed).
   - [x] Full test suite executed with all 654 tests passing (0 failures).
2. **Process Lifecycle Verification**:
   - [x] Verify creation and rotation of `instance/gowa_pids.json` (FIFO capped at 5 entries).
   - [x] Clean termination and teardown hook registration.
