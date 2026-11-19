# Update run.bat and run.sh to Use Gunicorn

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Update `run.bat` and `run.sh` to run the Mizalla Flask application using Gunicorn, accepting command-line options for `--threads` (or `-t`, defaulting to 1) and `--port` (or `-p`, defaulting to 8080).

**Architecture:** 
- In `run.sh` and `run.bat`, parse command-line arguments to accept `--threads` / `-t` (default `1`) and `--port` / `-p` (default `8080`), with support for positional overrides as fallback (`<port>` `<threads>`).
- Invoke Gunicorn using `"mizalla:create_app()"` bound to `0.0.0.0:<PORT>` with `--threads <THREADS>` via the virtual environment's Python (`python -m gunicorn`).
- On Windows (`run.bat`), call `python -m gunicorn --bind 0.0.0.0:%PORT% --threads %THREADS% "mizalla:create_app()"`.

**Tech Stack:** Bash, Batch / Windows CMD, Gunicorn, Flask WSGI Factory (`mizalla:create_app()`).

---

## Global Constraints
- Default port must be `8080`.
- Default threads must be `1` (single thread).
- Must support named flags (`--port` / `-p`, `--threads` / `-t`) as well as positional syntax (`run.sh [port] [threads]`).
- Preserve virtual environment auto-detection (`venv` and `.venv`).
- Adhere strictly to the workspace guidelines.

---

## Proposed Changes

### 1. `run.sh`
- Parse arguments for `--port` / `-p`, `--threads` / `-t`, or positional arguments.
- Default `PORT=8080`, `THREADS=1`.
- Execute:
  ```bash
  exec "$PYTHON_CMD" -m gunicorn \
      --bind "0.0.0.0:$PORT" \
      --threads "$THREADS" \
      "mizalla:create_app()"
  ```

### 2. `run.bat`
- Parse arguments for `--port` / `-p`, `--threads` / `-t`, or positional arguments `%1`, `%2`.
- Default `PORT=8080`, `THREADS=1`.
- Execute:
  ```cmd
  python -m gunicorn --bind 0.0.0.0:%PORT% --threads %THREADS% "mizalla:create_app()"
  ```

---

## Implementation Tasks

- [ ] **Task 1: Update `run.sh`**
  - Implement command line parsing for port and threads with defaults (8080 and 1).
  - Use `gunicorn` with `"mizalla:create_app()"`, `--bind 0.0.0.0:$PORT`, and `--threads $THREADS`.

- [ ] **Task 2: Update `run.bat`**
  - Implement command line parsing in Batch for port and threads with defaults (8080 and 1).
  - Execute `python -m gunicorn --bind 0.0.0.0:%PORT% --threads %THREADS% "mizalla:create_app()"`.

- [ ] **Task 3: Verification & Execution Testing**
  - Validate syntax and parameter handling for both scripts.
