"""Per-member access grants for the app's optional services.

Access lives in :class:`AppFeatureAccess` rather than boolean columns on
``Member`` so that adding a service does not require a schema migration, and
so every service is administered the same way in the admin UI.

``Member.can_bulk_email`` predates this table. It is still read once, by
:func:`backfill_legacy_access`, to seed grants for members who already had
Bulk Emailer access, and is not consulted for gating afterwards.
"""

from .extensions import db
from .role_meta import PM_TEAM_ROLE
from .models import (
    FEATURE_BULK_EMAIL,
    FEATURE_KEYS,
    FEATURE_LABELS,
    AppFeatureAccess,
    Member,
)


def _grant(feature_key: str, member_id: int, granted_by_id: int | None = None) -> bool:
    """Create a grant if absent. Returns True when a row was added."""
    if feature_key not in FEATURE_KEYS:
        raise ValueError(f"Unknown feature key: {feature_key!r}")

    existing = db.session.scalar(
        db.select(AppFeatureAccess).filter_by(feature_key=feature_key, member_id=member_id)
    )
    if existing is not None:
        return False

    db.session.add(
        AppFeatureAccess(feature_key=feature_key, member_id=member_id, granted_by_id=granted_by_id)
    )
    return True


def has_feature_access(member, feature_key: str) -> bool:
    """True when ``member`` has been granted ``feature_key``.

    Admins always pass, so they can verify a service works before rolling it
    out, and are never locked out of their own configuration screens.
    """
    if member is None or not getattr(member, "is_authenticated", False):
        return False
    if member.is_admin:
        return True
    return (
        db.session.scalar(
            db.select(AppFeatureAccess.id).filter_by(
                feature_key=feature_key, member_id=member.id
            )
        )
        is not None
    )


def granted_member_ids(feature_key: str) -> set[int]:
    return {
        row.member_id
        for row in db.session.scalars(
            db.select(AppFeatureAccess).filter_by(feature_key=feature_key)
        )
    }


def member_feature_keys(member) -> list[str]:
    return [
        key
        for key in FEATURE_KEYS
        if has_feature_access(member, key)
    ]


def sync_member_features(member, feature_keys, granted_by_id: int | None = None) -> None:
    """Make ``member``'s grants for the service features match ``feature_keys``.

    Only the service keys in :data:`FEATURE_KEYS` are touched, so callers
    cannot accidentally grant or revoke something outside this module.
    """
    wanted = {key for key in feature_keys if key in FEATURE_KEYS}
    current = {
        row.feature_key: row
        for row in db.session.scalars(
            db.select(AppFeatureAccess).filter_by(member_id=member.id)
        )
    }

    for key, row in current.items():
        if key in FEATURE_KEYS and key not in wanted:
            db.session.delete(row)

    for key in wanted:
        _grant(key, member.id, granted_by_id)


def backfill_legacy_access() -> int:
    """Seed grants from the pre-existing ``Member.can_bulk_email`` flag.

    Idempotent: existing grants are left alone, so running it on every start
    is safe. Returns the number of rows created.
    """
    created = 0
    for member in db.session.scalars(db.select(Member).where(Member.can_bulk_email.is_(True))):
        if _grant(FEATURE_BULK_EMAIL, member.id):
            created += 1
    if created:
        db.session.commit()
    return created


def backfill_global_pm_roles() -> int:
    """Turn the global Project Manager flag into per-project authority.

    A member flagged ``is_pm`` used to be a Project Manager on every project they
    could see. Authority is per project now, so the flag no longer grants it, and
    without this the PMs in an existing database would silently lose the ability
    to assign roles on projects they already ran.

    Each flagged member gets the "Project Manager" team role added to the team
    memberships they already hold. Nothing is added to projects they are not a
    member of, because they never had authority there either - a project they
    cannot see is a 404. Returns the number of memberships updated; safe to run on
    every start.
    """
    from .models import Member, ProjectMembership

    flagged = Member.query.filter_by(is_pm=True).all()
    if not flagged:
        return 0

    wanted = PM_TEAM_ROLE.casefold()
    changed = 0
    for member in flagged:
        for membership in ProjectMembership.query.filter_by(
            member_id=member.id, role="team"
        ).all():
            if membership.has_team_role(PM_TEAM_ROLE):
                continue
            membership.set_team_roles_list(
                [*membership.get_team_roles_list(), PM_TEAM_ROLE]
            )
            changed += 1
    if changed:
        db.session.commit()
    return changed
