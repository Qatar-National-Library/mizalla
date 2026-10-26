"""Tests ensuring Server, CommandGroup, CommandItem, and Member deletions cascade cleanly without SQLAlchemy IntegrityErrors."""

from datetime import datetime, timezone
import pytest
from mizalla.extensions import db
from mizalla.feature_access import sync_member_features
from mizalla.models import (
    AppFeatureAccess,
    CommandAudit,
    CommandGroup,
    CommandGroupItem,
    CommandGroupPermission,
    CommandItem,
    CommandItemPermission,
    CommandVariable,
    FEATURE_REMOTE_EXEC,
    Member,
    Server,
    ServerAccess,
    ServerCredential,
)


def _make_member(email: str, is_admin: bool = False):
    m = Member(name=email.split("@")[0], email=email, designation="Eng", is_admin=is_admin)
    m.set_password("pass1234")
    db.session.add(m)
    db.session.commit()
    return m


def _make_server(name: str = "Test Node", ip: str = "10.0.0.1"):
    s = Server(name=name, ip=ip, operating_system="Linux", status="available")
    db.session.add(s)
    db.session.commit()
    return s


def test_server_deletion_cascades_cleanly(app):
    """Deleting a server cascades its credentials, access grants, command groups, and audits."""
    with app.app_context():
        admin = _make_member("admin_del_srv@example.com", is_admin=True)
        server = _make_server("Cascade Srv", "10.10.10.1")

        # 1. Add credentials
        cred = ServerCredential(server_id=server.id, user_id=admin.id, username="root", is_default=True)
        # 2. Add access grant
        access = ServerAccess(server_id=server.id, member_id=admin.id, can_view=True, can_edit=True)
        # 3. Add command item pointing to server
        item = CommandItem(server_id=server.id, name="Srv Item", command_type="remote", command_content="uptime", created_by_id=admin.id)
        # 4. Add command group
        grp = CommandGroup(server_id=server.id, name="Srv Group", created_by_id=admin.id)
        db.session.add_all([cred, access, item, grp])
        db.session.commit()

        # 5. Add audit
        audit = CommandAudit(
            task_id="audit_srv_del",
            user_id=admin.id,
            server_id=server.id,
            group_id=grp.id,
            command_item_id=item.id,
            command="uptime",
            entry_type="group"
        )
        db.session.add(audit)
        db.session.commit()

        server_id = server.id
        item_id = item.id
        grp_id = grp.id
        audit_id = audit.id

        # Delete server
        db.session.delete(server)
        db.session.commit()

        assert db.session.get(Server, server_id) is None
        assert db.session.get(CommandGroup, grp_id) is None
        assert db.session.get(CommandAudit, audit_id) is None
        assert db.session.get(ServerCredential, cred.id) is None
        assert db.session.get(ServerAccess, access.id) is None
        # Item was server-independent, so it should be preserved with server_id = None
        preserved_item = db.session.get(CommandItem, item_id)
        assert preserved_item is not None
        assert preserved_item.server_id is None


def test_command_group_deletion_cascades_cleanly(app):
    """Deleting a command group cascades group items, permissions, and decouples audits."""
    with app.app_context():
        admin = _make_member("admin_del_grp@example.com", is_admin=True)
        user = _make_member("user_del_grp@example.com", is_admin=False)
        server = _make_server("Group Srv", "10.10.10.2")

        item = CommandItem(name="Group Item", command_type="remote", command_content="echo 1", created_by_id=admin.id)
        grp = CommandGroup(server_id=server.id, name="My Pipeline", created_by_id=admin.id)
        db.session.add_all([item, grp])
        db.session.commit()

        gi = CommandGroupItem(group_id=grp.id, command_item_id=item.id, sequence_order=1)
        perm = CommandGroupPermission(group_id=grp.id, user_id=user.id, can_view=True, can_run=True)
        audit = CommandAudit(
            task_id="audit_grp_del",
            user_id=admin.id,
            server_id=server.id,
            group_id=grp.id,
            command_item_id=item.id,
            command="echo 1",
            entry_type="group"
        )
        db.session.add_all([gi, perm, audit])
        db.session.commit()

        grp_id = grp.id
        gi_id = gi.id
        perm_id = perm.id
        audit_id = audit.id
        item_id = item.id

        # Delete group
        db.session.delete(grp)
        db.session.commit()

        assert db.session.get(CommandGroup, grp_id) is None
        assert db.session.get(CommandGroupItem, gi_id) is None
        assert db.session.get(CommandGroupPermission, perm_id) is None
        # The command item itself is unaffected
        assert db.session.get(CommandItem, item_id) is not None
        # The audit history is preserved with group_id set to None
        preserved_audit = db.session.get(CommandAudit, audit_id)
        assert preserved_audit is not None
        assert preserved_audit.group_id is None


def test_command_item_deletion_cascades_cleanly(app):
    """Deleting a command item cleans up variables, permissions, group item links, and decouples audits."""
    with app.app_context():
        admin = _make_member("admin_del_item@example.com", is_admin=True)
        user = _make_member("user_del_item@example.com", is_admin=False)
        server = _make_server("Item Srv", "10.10.10.3")

        item = CommandItem(name="Target Item", command_type="remote", command_content="echo @@ Name @@", created_by_id=admin.id)
        grp = CommandGroup(server_id=server.id, name="Parent Group", created_by_id=admin.id)
        db.session.add_all([item, grp])
        db.session.commit()

        var = CommandVariable(command_item_id=item.id, name="Name", default_value="World")
        perm = CommandItemPermission(command_item_id=item.id, user_id=user.id, can_view=True, can_run=True)
        gi = CommandGroupItem(group_id=grp.id, command_item_id=item.id, sequence_order=1)
        audit = CommandAudit(
            task_id="audit_item_del",
            user_id=admin.id,
            server_id=server.id,
            command_item_id=item.id,
            command="echo World",
            entry_type="item"
        )
        db.session.add_all([var, perm, gi, audit])
        db.session.commit()

        item_id = item.id
        var_id = var.id
        perm_id = perm.id
        gi_id = gi.id
        audit_id = audit.id
        grp_id = grp.id

        # Delete item
        db.session.delete(item)
        db.session.commit()

        assert db.session.get(CommandItem, item_id) is None
        assert db.session.get(CommandVariable, var_id) is None
        assert db.session.get(CommandItemPermission, perm_id) is None
        assert db.session.get(CommandGroupItem, gi_id) is None
        # Parent group is preserved
        assert db.session.get(CommandGroup, grp_id) is not None
        # Audit history is preserved with command_item_id set to None
        preserved_audit = db.session.get(CommandAudit, audit_id)
        assert preserved_audit is not None
        assert preserved_audit.command_item_id is None


def test_member_deletion_cascades_cleanly(app):
    """Deleting a member cascades access grants, feature access, credentials, created items/groups, permissions, and audits."""
    with app.app_context():
        user = _make_member("user_del_all@example.com", is_admin=False)
        server = _make_server("Member Srv", "10.10.10.4")

        access = ServerAccess(server_id=server.id, member_id=user.id, can_view=True, can_edit=False)
        feat = AppFeatureAccess(feature_key="remote_exec", member_id=user.id)
        cred = ServerCredential(server_id=server.id, user_id=user.id, username="deployer")
        item = CommandItem(name="User's Item", command_type="remote", command_content="whoami", created_by_id=user.id)
        grp = CommandGroup(server_id=server.id, name="User's Group", created_by_id=user.id)
        db.session.add_all([access, feat, cred, item, grp])
        db.session.commit()

        gi = CommandGroupItem(group_id=grp.id, command_item_id=item.id, sequence_order=1)
        item_perm = CommandItemPermission(command_item_id=item.id, user_id=user.id, can_view=True, can_run=True)
        grp_perm = CommandGroupPermission(group_id=grp.id, user_id=user.id, can_view=True, can_run=True)
        audit = CommandAudit(
            task_id="audit_user_del",
            user_id=user.id,
            server_id=server.id,
            command="whoami",
            entry_type="item"
        )
        db.session.add_all([gi, item_perm, grp_perm, audit])
        db.session.commit()

        user_id = user.id
        access_id = access.id
        feat_id = feat.id
        cred_id = cred.id
        item_id = item.id
        grp_id = grp.id
        item_perm_id = item_perm.id
        grp_perm_id = grp_perm.id
        audit_id = audit.id

        # Delete member
        db.session.delete(user)
        db.session.commit()

        assert db.session.get(Member, user_id) is None
        assert db.session.get(ServerAccess, access_id) is None
        assert db.session.get(AppFeatureAccess, feat_id) is None
        assert db.session.get(ServerCredential, cred_id) is None
        assert db.session.get(CommandItem, item_id) is None
        assert db.session.get(CommandGroup, grp_id) is None
        assert db.session.get(CommandItemPermission, item_perm_id) is None
        assert db.session.get(CommandGroupPermission, grp_perm_id) is None
        assert db.session.get(CommandAudit, audit_id) is None
