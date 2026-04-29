"""
Visibility scoping for Teams.

Agents and viewers see only voicemails routed to teams they belong to (plus
unrouted voicemails — these would otherwise become invisible). Supervisors
and admins see everything.

Use `scope_voicemails(query, user)` on any SQLAlchemy `Voicemail` query before
.paginate()/.all() to apply the rule. Use `can_view_voicemail(vm, user)` for
single-row checks (detail page, audio download, etc).
"""
from typing import Iterable
from app.models.voicemail import Voicemail


def is_unrestricted(user) -> bool:
    """Users who see every voicemail regardless of team membership."""
    return getattr(user, "is_admin", False) or getattr(user, "is_supervisor", False)


def user_team_ids(user) -> list:
    if not user or not getattr(user, "is_authenticated", False):
        return []
    try:
        return [t.id for t in (user.teams or [])]
    except Exception:
        return []


def scope_voicemails(query, user):
    """
    Restrict a Voicemail query to voicemails the user is allowed to see.

    Rules:
      - admins/supervisors: no restriction
      - everyone else: voicemails on a team they belong to OR with no team yet
        (so an unrouted call doesn't disappear into the void)
    """
    if is_unrestricted(user):
        return query
    team_ids = user_team_ids(user)
    if not team_ids:
        # User belongs to no teams — they only ever see unrouted voicemails.
        return query.filter(Voicemail.team_id.is_(None))
    return query.filter(
        (Voicemail.team_id.is_(None)) | (Voicemail.team_id.in_(team_ids))
    )


def can_view_voicemail(vm: Voicemail, user) -> bool:
    if is_unrestricted(user):
        return True
    if vm.team_id is None:
        return True
    return vm.team_id in user_team_ids(user)
