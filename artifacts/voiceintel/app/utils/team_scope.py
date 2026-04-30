"""
Visibility scoping for Teams.

Only admins see every voicemail. Supervisors, agents, and viewers see
voicemails routed to teams they belong to, plus unrouted voicemails (these
would otherwise become invisible).

Use `scope_voicemails(query, user)` on any SQLAlchemy `Voicemail` query before
.paginate()/.all() to apply the rule. Use `can_view_voicemail(vm, user)` for
single-row checks (detail page, audio download, etc).

Soft-delete: by default both helpers exclude voicemails that have been moved
to the admin-only Deleted folder (deleted_at IS NOT NULL). Pass
`include_deleted=True` to include them — this is only ever used by the admin
Deleted-folder routes (list / restore / permanent purge).
"""
from app.models.voicemail import Voicemail


def is_unrestricted(user) -> bool:
    """Users who see every voicemail regardless of team membership."""
    return getattr(user, "is_admin", False)


def user_team_ids(user) -> list:
    if not user or not getattr(user, "is_authenticated", False):
        return []
    try:
        return [t.id for t in (user.teams or [])]
    except Exception:
        return []


def scope_voicemails(query, user, *, include_deleted=False):
    """
    Restrict a Voicemail query to voicemails the user is allowed to see.

    Rules:
      - admins: no team restriction
      - everyone else (supervisor, agent, viewer): voicemails on a team they
        belong to OR with no team yet (so an unrouted call doesn't disappear
        into the void)

    Soft-deleted voicemails are filtered out unless `include_deleted=True`.
    Even admins do NOT see deleted rows in normal pages — they show up only
    in the dedicated /voicemails/deleted view.
    """
    if not include_deleted:
        query = query.filter(Voicemail.deleted_at.is_(None))
    if is_unrestricted(user):
        return query
    team_ids = user_team_ids(user)
    if not team_ids:
        # User belongs to no teams — they only ever see unrouted voicemails.
        return query.filter(Voicemail.team_id.is_(None))
    return query.filter(
        (Voicemail.team_id.is_(None)) | (Voicemail.team_id.in_(team_ids))
    )


def can_view_voicemail(vm: Voicemail, user, *, include_deleted=False) -> bool:
    # Soft-deleted voicemails are invisible everywhere except the admin-only
    # Deleted-folder flows that opt in with include_deleted=True.
    if not include_deleted and vm.deleted_at is not None:
        return False
    if is_unrestricted(user):
        return True
    if vm.team_id is None:
        return True
    return vm.team_id in user_team_ids(user)
