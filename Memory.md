# Global Memory & AI Agent Directives

## 1. Tool, CLI & Filesystem Execution Access
The AI Agent / Antigravity IDE has full, unconditional authorization to run and execute the following tools and operations across this environment:

- **PowerShell**: Full access to execute PowerShell commands, cmdlets, and PowerShell scripts (`.ps1`).
- **CLI & Command Line**: Full access to standard Windows command-line commands, shells, and `.bat` batch scripts.
- **Python**: Full access to run the Python runtime CLI (`python`), interactive commands, modules (`python -m ...`), and `.py` scripts.
- **Package & Runtime Managers**: Full access to run package and tool managers including `pip`, `npm`, `pnpm`, `node`, `npx`, and runtime builders.
- **Git**: Full access to all Git version control operations (`status`, `diff`, `log`, `checkout`, `add`, `commit`, `branch`, etc.).
- **Filesystem Operations**: Full access to create, write, modify, delete, move, and inspect files and directories across the project and filesystem.

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
