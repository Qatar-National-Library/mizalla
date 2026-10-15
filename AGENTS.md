# Antigravity IDE & AI Agent Directives

## Execution & Tool Access Permissions
The AI Agent is authorized with full access to run:
- **PowerShell**: Commands and `.ps1` scripts without restrictions.
- **CLI / Command Line**: Windows CMD commands and `.bat` batch scripts.
- **Python Runtime**: Python CLI, Python commands, `-m` modules, and `.py` scripts.
- **Package & Dependency Managers**: `pip`, `npm`, `pnpm`, `node`, `npx`, and development servers.
- **Git**: All Git operations and version control workflows.
- **Filesystem Access**: Create, edit, delete, and manage files and folders on the filesystem.

---

## Universal Icon Standard
- Across all projects, **Lucide icons** (`lucide.createIcons()`) must be used wherever icons are needed, unless explicitly directed otherwise.
- **Color Standards**:
  - Light Background: Header green color (`var(--green-dark, #103e30)`).
  - Green or Dark Background: White (`#ffffff`).
- Usage: `<i data-lucide="icon-name"></i>`
- Script: `<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.js"></script>`

---

## Universal Password Field Standard ("Click eye in field to view password")
- Across all projects and templates, wherever a **password field** is rendered:
  - Always use an in-field eye toggle button allowing users to view/hide their password.
  - Wrap the input in a `.password-input-wrapper` container with right-padding on the input so text does not overlap the icon.
  - Position the toggle button (`.password-toggle-btn`) inside the right edge of the input.
  - Use Lucide icons: `<i data-lucide="eye" class="icon-green"></i>` when obscured, swapping to `data-lucide="eye-off"` when revealed.
  - Provide delegated click handlers and auto-enhancement (`initPasswordToggles()`) in client-side scripts to wire up static and dynamic password fields.

---

## Workspace Architecture & Guidelines
- Follow Flask application-factory conventions (`mizalla.create_app`).
- Remote Execution module uses Agentless AsyncSSH (`mizalla.asyncssh_engine`).
- Credentials are encrypted at rest with Fernet (`mizalla.crypto_vault`).
- Preserve all existing tests and ensure `python -m pytest` passes.
- **Python Package Management**: If we are working with Python code, **ALWAYS use `requirements.txt`** for package management (`pip install -r requirements.txt`). Do not use alternative managers unless explicitly instructed.
- **Python Virtual Environment Lifecycle**:
  - Always use a virtual environment to develop, test, write code, or run Python applications and scripts.
  - If a virtual environment is not available, create one immediately (default name = `venv`, e.g., `python -m venv venv`).
  - If Python packages change (new ones added, version changed, package list edited, etc.) as you develop/write code, drop the virtual environment and recreate it cleanly from scratch before reinstalling packages (`pip install -r requirements.txt`).
