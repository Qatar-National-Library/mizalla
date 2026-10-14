# Global AI Agent Directives

## Permissions & Access Rights
The agent has full access and permission to execute:
1. **PowerShell**: Powershell commands and `.ps1` scripts.
2. **CLI & Command Line**: System commands and `.bat` scripts.
3. **Python Runtime**: Python CLI, commands, and `.py` scripts.
4. **Package Managers**: `pip`, `npm`, `pnpm`, and `node` scripts.
5. **Git**: All git operations.
6. **Filesystem**: Create, delete, move, and edit any files and directories.

## Universal Icon Standard
- Use **Lucide icons** (`data-lucide`) across all projects wherever icons are needed, unless specifically directed otherwise.
- Green (`var(--green-dark, #103e30)`) on light backgrounds; White (`#ffffff`) on green or dark backgrounds.

## Python Package Management Standard
- If working with Python code, **ALWAYS use `requirements.txt`** for package management (`pip install -r requirements.txt`). Do not use alternative package managers unless explicitly instructed.

## Python Virtual Environment Lifecycle Standard
- **Always use a Virtual Environment**: Always use a virtual environment to develop, test, write code, or run Python applications and scripts.
- **Creation when Missing**: If a virtual environment is not available, create one immediately (default name = `venv`, e.g., `python -m venv venv`).
- **Drop & Recreate on Package Changes**: If Python packages change (new ones added, version changed, package list edited, etc.) as you develop/write code, drop the virtual environment and recreate it cleanly from scratch before reinstalling packages (`pip install -r requirements.txt`).

