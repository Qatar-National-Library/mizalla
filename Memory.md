# Global Memory & AI Agent Directives

## 1. Tool, CLI & Filesystem Execution Authority
The AI Agent / Antigravity / Gemini CLI has full, unconditional authorization and must NEVER ask repeatedly for permission:
- **Scripts, Commands & Terminals**: Full authority to run scripts and commands in **PowerShell**, **Command Line (CMD/batch)**, and **bash**. Do NOT ask repeatedly.
- **Filesystem Operations**:
  - **Current Project Folder**: Full authority to edit, delete, update, and create files under the *current project* folder. Do NOT ask repeatedly.
  - **Global Settings Folder**: Full access to global settings and configuration folder (`C:\Users\uahmad\.gemini` and subdirectories). Do NOT ask repeatedly.
  - **User-Granted Folders**: Full authority over any other folder the user gives explicit access to.
  - **External Folder Boundary**: If access is needed to any folder besides the current project, global settings, or explicitly granted folders, ask the user, but that access will strictly be for that *project* only.
- **Python / venv / pip / pipx**: Full authority to run **python**, **venv**, **pip**, and **pipx** scripts and commands. Do NOT ask repeatedly.
- **Node / npm / pnpm / npx**: Full authority to run **node**, **npm**, **pnpm**, and **npx** scripts and commands. Do NOT ask repeatedly.
- **Git**: Full access to all Git version control operations (`status`, `diff`, `log`, `checkout`, `add`, `commit`, `branch`, etc.).
---

## 2. Universal Icon & UI Standards
- **Lucide Icons**: We use **Lucide icons** across all projects wherever icons are needed, unless otherwise specifically directed by the user.
- **Script Tag & CDN**:
  ```html
  <script src="https://unpkg.com/lucide@latest/dist/umd/lucide.js"></script>
  ```
- **Initialization**:
  ```html
  <script>
    if (window.lucide) {
      lucide.createIcons();
    }
  </script>
  ```
- **Usage Syntax**: `<i data-lucide="icon-name"></i>`
- **Icon Color Rules**:
  - **Light Backgrounds**: Icons must match the primary brand green of the header background (`var(--green-dark, #103e30)` / `stroke: #103e30`).
  - **Green or Dark Backgrounds**: Icons must be pure white (`#ffffff` / `stroke: #ffffff !important`).
  - Dynamic JavaScript UI changes that inject or modify icons must re-invoke `lucide.createIcons()`.

---

## 3. Project Architecture Notes
- Base framework: **Flask** application-factory (`mizalla.create_app`).
- Remote Server Orchestration: Agentless AsyncSSH execution engine (`mizalla/asyncssh_engine.py`, `mizalla/remote_exec_client.py`, `mizalla/remote_exec.py`). No remote agents.
- Database: SQLAlchemy models with encrypted credentials via cryptography/Fernet (`mizalla/crypto_vault.py`).
- Testing: `python -m pytest` with comprehensive unit and integration test coverage.

---

## 4. Python Package Management Standard
- When working with Python code, **ALWAYS use `requirements.txt`** for package management.
- Maintain and update all project dependencies in `requirements.txt`.
- Install packages via `pip install -r requirements.txt` (or `pip install <package>` and record in `requirements.txt`). Do not introduce or switch to alternative managers (Poetry, Pipenv, Flit, etc.) unless explicitly instructed by the user.

---

## 5. Python Virtual Environment Lifecycle Standard
- **Always use a Virtual Environment**: Always use a virtual environment to develop, test, write code, or run Python applications and scripts.
- **Creation when Missing**: If a virtual environment is not available, create one immediately (default name = `venv`, e.g., `python -m venv venv`).
- **Drop & Recreate on Package Changes**: If Python packages change at any point (new packages added, versions changed, package list edited, or `requirements.txt` updated) during development, drop/delete the existing virtual environment and recreate it cleanly from scratch before reinstalling packages (`pip install -r requirements.txt`).

---

## 6. Mandatory Pre-Development Planning & Superpowers Workflow Standard
- **ALWAYS Follow Globally**: These planning directives apply to all projects and tasks without exception:
  1. **Use Superpowers Before Development**: Always activate and use the `superpowers` skills (`using-superpowers`, `writing-plans`, etc.) to plan out thoroughly before any development or code change.
  2. **Clarifications on Ambiguity**: If you encounter any ambiguity or need clarifications, proactively ask the user for clarification before making assumptions or proceeding.
  3. **Plan File Storage**: Prepare and save the detailed plan as `/plans/<plan name>.md` (e.g., `plans/<feature_or_task_name>.md`).
  4. **Explicit Approval Gate**: Always ask the user for approval to start development before starting implementation. Never begin coding without confirmation.


