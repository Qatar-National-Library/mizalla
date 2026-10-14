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

## Workspace Architecture & Guidelines
- Follow Flask application-factory conventions (`mizalla.create_app`).
- Remote Execution module uses Agentless AsyncSSH (`mizalla.asyncssh_engine`).
- Credentials are encrypted at rest with Fernet (`mizalla.crypto_vault`).
- Preserve all existing tests and ensure `python -m pytest` passes.
