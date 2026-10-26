# Global AI Agent Directives

## Permissions & Unconditional Execution Authority
The agent / Antigravity / Gemini CLI has full, unconditional authority and must NEVER ask repeatedly for permission to execute:
1. **Scripts, Commands & Terminals**: Full authority to run scripts and commands in **PowerShell**, **Command Line (CMD/batch)**, and **bash**. Do NOT ask repeatedly.
2. **Filesystem Operations**:
   - **Current Project Folder**: Full authority to edit, delete, update, and create files under the *current project* folder. Do NOT ask repeatedly.
   - **Global Settings Folder**: Full access to the global settings and configuration folder (`C:\Users\uahmad\.gemini` and its subdirectories). Do NOT ask repeatedly.
   - **User-Granted Folders**: Full authority over any other folder the user gives explicit access to.
   - **External Folder Boundary**: If access is needed to any folder besides the current project, global settings, or explicitly granted folders, ask the user, but that access will strictly be for that *project* only.
3. **Python Runtime & Tools**: Full authority to run **python**, **venv**, **pip**, and **pipx** scripts and commands. Do NOT ask repeatedly.
4. **Node & Package Managers**: Full authority to run **node**, **npm**, **pnpm**, and **npx** scripts and commands. Do NOT ask repeatedly.
5. **Git**: All git operations.

## Universal Icon Standard
- Use **Lucide icons** (`data-lucide`) across all projects wherever icons are needed, unless specifically directed otherwise.
- Green (`var(--green-dark, #103e30)`) on light backgrounds; White (`#ffffff`) on green or dark backgrounds.

## Universal Password Field Standard ("Click eye in field to view password")
- Across all projects and templates, wherever a **password field** is rendered:
  - Always use an in-field eye toggle button allowing users to view/hide their password.
  - Wrap the input in a `.password-input-wrapper` container with right-padding on the input so text does not overlap the icon.
  - Position the toggle button (`.password-toggle-btn`) inside the right edge of the input.
  - Use Lucide icons: `<i data-lucide="eye" class="icon-green"></i>` when obscured, swapping to `data-lucide="eye-off"` when revealed.
  - Provide delegated click handlers and auto-enhancement (`initPasswordToggles()`) in client-side scripts to wire up static and dynamic password fields.

## Python Package Management Standard
- If working with Python code, **ALWAYS use `requirements.txt`** for package management (`pip install -r requirements.txt`). Do not use alternative package managers unless explicitly instructed.

## Python Virtual Environment Lifecycle Standard
- **Always use a Virtual Environment**: Always use a virtual environment to develop, test, write code, or run Python applications and scripts.
- **Creation when Missing**: If a virtual environment is not available, create one immediately (default name = `venv`, e.g., `python -m venv venv`).
- **Drop & Recreate on Package Changes**: If Python packages change (new ones added, version changed, package list edited, etc.) as you develop/write code, drop the virtual environment and recreate it cleanly from scratch before reinstalling packages (`pip install -r requirements.txt`).

## Mandatory Pre-Development Planning & Superpowers Workflow Standard
- **ALWAYS Follow Globally**: These planning directives apply to all projects and tasks without exception:
  1. **Use Superpowers Before Development**: Always activate and use the `superpowers` skills (`using-superpowers`, `writing-plans`, etc.) to plan out thoroughly before any development or code change.
  2. **Clarifications on Ambiguity**: If you encounter any ambiguity or need clarifications, proactively ask the user for clarification before making assumptions or proceeding.
  3. **Plan File Storage**: Prepare and save the detailed plan as `/plans/<plan name>.md` (e.g., `plans/<feature_or_task_name>.md`).
  4. **Explicit Approval Gate**: Always ask the user for approval to start development before starting implementation. Never begin coding without confirmation.
