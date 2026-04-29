"""
Voicemail → Team auto-routing.

Called from the ingestion pipeline once when the voicemail is first saved
(matches recipient/sender/phone rules) and again after transcription
completes (gives keyword rules a chance to fire).

Rules are evaluated lowest-priority-number first; the first match wins.
A voicemail with `team_locked=True` (manually overridden) is never re-routed.
"""
import logging
import re
from typing import Optional

from app import db
from app.models.team import RoutingRule

logger = logging.getLogger(__name__)


def _domain_of(addr: str) -> str:
    """Return the lowercased domain portion of an email address.
    Tolerates display-name forms like '"Bob" <bob@x.com>'."""
    if not addr:
        return ""
    # Strip display name + angle brackets if present
    m = re.search(r"<([^>]+)>", addr)
    raw = (m.group(1) if m else addr).strip().lower()
    if "@" in raw:
        return raw.split("@", 1)[1]
    return ""


def _email_of(addr: str) -> str:
    """Return just the bare email address (lowercased), stripped of display name."""
    if not addr:
        return ""
    m = re.search(r"<([^>]+)>", addr)
    return (m.group(1) if m else addr).strip().lower()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _matches(rule: RoutingRule, vm) -> bool:
    """True if `rule` matches `vm`. Pure function — no DB writes."""
    pattern = (rule.pattern or "").strip()
    if not pattern:
        return False

    kind = rule.kind

    if kind == "recipient_email":
        return _email_of(vm.recipient) == pattern.lower()

    if kind == "recipient_domain":
        return _domain_of(vm.recipient) == pattern.lstrip("@").lower()

    if kind == "sender_email":
        return _email_of(vm.sender) == pattern.lower()

    if kind == "sender_domain":
        return _domain_of(vm.sender) == pattern.lstrip("@").lower()

    if kind == "keyword":
        text = ""
        if getattr(vm, "transcript", None) and vm.transcript and vm.transcript.text:
            text = vm.transcript.text
        # Also peek at the subject so a rule can fire pre-transcription.
        if vm.subject:
            text = f"{text} {vm.subject}"
        return pattern.lower() in text.lower()

    if kind == "caller_phone":
        try:
            phone = vm.caller_info.get("phone") or ""
        except Exception:
            phone = ""
        target = _digits(pattern)
        return bool(target) and target in _digits(phone)

    return False


def route_voicemail(vm, *, commit: bool = True) -> Optional[int]:
    """
    Apply routing rules to `vm`. Returns the team_id assigned (or already set),
    or None if no rule matched.

    Skips rerouting when vm.team_locked is True (manual override).
    Only writes to vm.team_id if it's currently empty *or* would change to a
    new match — i.e. once routed by a rule, a new matching rule with higher
    priority can take over until a human locks it.
    """
    if getattr(vm, "team_locked", False):
        return vm.team_id

    rules = (
        RoutingRule.query
        .filter_by(is_active=True)
        .order_by(RoutingRule.priority.asc(), RoutingRule.id.asc())
        .all()
    )

    for rule in rules:
        try:
            if _matches(rule, vm):
                if vm.team_id != rule.team_id:
                    vm.team_id = rule.team_id
                    if commit:
                        db.session.commit()
                    logger.info(
                        "Routed voicemail id=%s to team_id=%s via rule id=%s (%s='%s')",
                        vm.id, rule.team_id, rule.id, rule.kind, rule.pattern,
                    )
                return rule.team_id
        except Exception as e:
            logger.warning("Routing rule id=%s failed to evaluate: %s", rule.id, e)
            continue

    return vm.team_id  # may be None or a previously-set value
