from sqlalchemy import Column, MetaData, Table, inspect
from sqlalchemy.schema import CreateTable

from .extensions import db
from .models import ProjectMembership
from .role_meta import MEMBERSHIP_ROLES


SQLITE_ADDITIONS = {
    "app_setting": {
        "key": "VARCHAR(80) UNIQUE",
        "value": "TEXT NOT NULL DEFAULT ''",
        "description": "VARCHAR(255) NOT NULL DEFAULT ''",
        "updated_at": "DATETIME",
    },
    "member": {
        "api_key": "VARCHAR(64)",
        "can_bulk_email": "BOOLEAN NOT NULL DEFAULT 0",
        "is_pm": "BOOLEAN NOT NULL DEFAULT 0",
    },
    "project_membership": {
        "team_roles": "VARCHAR(300) NOT NULL DEFAULT ''",
    },
    "project": {
        "scope": "TEXT NOT NULL DEFAULT ''",
        "estimated_completion_date": "DATE",
        "actual_start_date": "DATE",
        "actual_completion_date": "DATE",
    },
    "task": {
        "source_type": "VARCHAR(20) NOT NULL DEFAULT 'Project Plan'",
        "meeting_id": "INTEGER REFERENCES meeting(id) ON DELETE SET NULL",
        "jira_ticket_id": "VARCHAR(80)",
        "highlights": "VARCHAR(300) NOT NULL DEFAULT ''",
        "highlighted_on": "DATE",
        "is_milestone": "BOOLEAN NOT NULL DEFAULT 0",
        "milestone_achieved_on": "DATE",
        "task_order": "INTEGER NOT NULL DEFAULT 0",
    },
    "meeting": {
        "meeting_start_at": "DATETIME",
        "meeting_utc_offset_minutes": "INTEGER",
        "external_attendees": "TEXT NOT NULL DEFAULT ''",
    },
    "server": {
        "auth_token": "VARCHAR(64)",
        "agent_status": "VARCHAR(30) NOT NULL DEFAULT 'offline'",
        "agent_last_seen": "DATETIME",
        "connection_attempts": "INTEGER NOT NULL DEFAULT 0",
        "max_connection_attempts": "INTEGER NOT NULL DEFAULT 5",
        "connection_message": "VARCHAR(255) DEFAULT ''",
        "os_type": "VARCHAR(20) NOT NULL DEFAULT 'linux'",
        "os_version": "VARCHAR(120) NOT NULL DEFAULT ''",
        "admin_notes": "TEXT NOT NULL DEFAULT ''",
        "ssh_port": "INTEGER NOT NULL DEFAULT 22",
        "status": "VARCHAR(20) NOT NULL DEFAULT 'available'",
        "last_checked": "DATETIME",
    },
    "command_audit": {
        "client_ip": "VARCHAR(45) DEFAULT ''",
        "entry_type": "VARCHAR(20) NOT NULL DEFAULT 'item'",
        "title": "VARCHAR(180) NOT NULL DEFAULT ''",
        "executed_command": "TEXT NOT NULL DEFAULT ''",
        "remote_pid": "INTEGER",
        "group_id": "INTEGER",
        "command_item_id": "INTEGER",
        "step_results": "TEXT NOT NULL DEFAULT ''",
    },
}


#: Scratch name for the rebuilt table. The old table is copied into it, then it
#: is renamed over the original, which is the only way SQLite lets a CHECK
#: constraint change.
_REBUILD_NAME = "project_membership__role_upgrade"


def _membership_constraint_is_current(sql: str) -> bool:
    """True when the stored table definition already permits every known role.

    Checked by looking for each role in the DDL rather than by parsing it: the
    constraint is generated from ``MEMBERSHIP_ROLES``, so a new role in the tuple
    that is missing from the table means the two have drifted and the table is
    stale.
    """
    return all(f"'{role}'" in sql for role in MEMBERSHIP_ROLES)


def _staged_membership_table():
    """The model's ``project_membership`` table, renamed for the rebuild.

    Copied into a throwaway MetaData rather than renamed in place so nothing
    about the live model changes. The foreign keys point at ``project`` and
    ``member``, which are not in that throwaway MetaData, so stand-ins carrying
    the same name and column type are added - the DDL only needs to name them,
    and creating them is not attempted.
    """
    source = ProjectMembership.__table__
    metadata = MetaData()
    for column in source.columns:
        for fk in column.foreign_keys:
            if fk.column.table.name not in metadata.tables:
                Table(
                    fk.column.table.name,
                    metadata,
                    Column(fk.column.name, fk.column.type, primary_key=True),
                )
    staged = source.to_metadata(metadata)
    staged.name = _REBUILD_NAME
    return staged


def rebuild_membership_role_constraint(connection) -> bool:
    """Copy project_membership into a table whose CHECK knows the current roles.

    SQLite cannot ALTER a CHECK constraint, and a Delegated PM cannot be stored
    until the constraint names that role, so an existing database has to have its
    table rebuilt. The replacement is generated from the model rather than
    hand-written, so its columns cannot drift from it either.

    Returns True when a rebuild happened.
    """
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'project_membership'"
    ).first()
    if row is None or _membership_constraint_is_current(row[0] or ""):
        return False

    staged = _staged_membership_table()
    columns = ", ".join(f'"{column.name}"' for column in ProjectMembership.__table__.columns)

    # Nothing references project_membership, so dropping and renaming it cannot
    # orphan a foreign key. PRAGMA foreign_keys cannot be changed inside a
    # transaction, so it is deliberately left alone here.
    connection.exec_driver_sql(str(CreateTable(staged).compile(connection.engine)))
    connection.exec_driver_sql(
        f'INSERT INTO "{_REBUILD_NAME}" ({columns}) SELECT {columns} FROM "project_membership"'
    )
    connection.exec_driver_sql('DROP TABLE "project_membership"')
    connection.exec_driver_sql(f'ALTER TABLE "{_REBUILD_NAME}" RENAME TO "project_membership"')
    return True


def upgrade_schema() -> bool:
    """Bring an existing SQLite database up to the current schema.

    Returns True when the project_membership table had to be rebuilt, so the
    caller can log that the change was not just additive.
    """
    if db.engine.dialect.name != "sqlite":
        raise RuntimeError("The built-in additive schema upgrade currently supports SQLite only.")

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    with db.engine.begin() as connection:
        for table_name, additions in SQLITE_ADDITIONS.items():
            if table_name not in existing_tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, definition in additions.items():
                if column_name not in existing:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}'
                    )
        # Runs after the column additions above, so the copy picks up any column
        # that was just added and the rebuilt table already carries it.
        return rebuild_membership_role_constraint(connection)