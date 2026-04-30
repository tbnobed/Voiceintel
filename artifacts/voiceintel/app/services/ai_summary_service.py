"""
Per-voicemail AI summary service.

Sends the transcript to Phi-3 mini via Ollama and returns a structured payload
(summary, intent, action items, suggested response). Designed to be called
from the pipeline AFTER transcription completes — failure here must NEVER
block a voicemail from being persisted/displayed.

Output is parsed from a labeled-text format (`SUMMARY:`, `INTENT:`,
`ACTION ITEMS:`, `SUGGESTED RESPONSE:`) rather than JSON, because Phi-3 mini
is markedly more reliable with prose than with strict JSON. The parser is
tolerant of trailing whitespace, optional `**bold**` markers, and missing
sections.
"""
import os
import re
import time
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Hard cap on transcript chars sent to the model. ~4000 chars ≈ ~1000 tokens
# of input which keeps round-trip well under the 15-second budget on GPU.
MAX_TRANSCRIPT_CHARS = 4000

# Generation budget: leaves headroom for slow first-token latency.
GEN_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_TOKENS  = 350


def _build_prompt(transcript: str, caller_name: Optional[str], caller_phone: Optional[str]) -> str:
    """Build the Phi-3 prompt. Caller name/phone are passed in so the model
    can reference the caller naturally and write a usable suggested reply."""
    truncated = transcript.strip()
    truncated_note = ""
    if len(truncated) > MAX_TRANSCRIPT_CHARS:
        truncated = truncated[:MAX_TRANSCRIPT_CHARS]
        truncated_note = "\n[Transcript truncated for length.]"

    caller_label = caller_name or "the caller"
    contact_line = ""
    if caller_phone:
        contact_line = f"Caller phone: {caller_phone}\n"

    return (
        "<|user|>\n"
        "You are a voicemail analyst. Read the transcript below and respond "
        "in EXACTLY this format — do not add any other sections, headings, "
        "or commentary.\n\n"
        "SUMMARY: One or two sentences in plain English describing what "
        f"{caller_label} said and why they called.\n"
        "INTENT: A short phrase (5-10 words) capturing the caller's primary goal.\n"
        "ACTION ITEMS:\n"
        "- A concrete next step the team should take.\n"
        "- (Add a second bullet only if there is a clearly distinct second action.)\n"
        f"SUGGESTED RESPONSE: A 2-3 sentence draft that the team could use to reply to {caller_label}. "
        "Be warm and professional; never invent facts the caller didn't mention.\n\n"
        f"{contact_line}"
        f"Transcript:\n\"\"\"\n{truncated}\n\"\"\"{truncated_note}\n"
        "<|end|>\n<|assistant|>"
    )


# Regexes are tolerant of optional bold markers and trailing punctuation/colons.
_RE_SUMMARY  = re.compile(r"(?:\*\*\s*)?SUMMARY\s*(?:\*\*)?\s*:\s*(.+?)(?=\n\s*(?:\*\*\s*)?(?:INTENT|ACTION ITEMS|SUGGESTED RESPONSE)\s*(?:\*\*)?\s*:|\Z)", re.IGNORECASE | re.DOTALL)
_RE_INTENT   = re.compile(r"(?:\*\*\s*)?INTENT\s*(?:\*\*)?\s*:\s*(.+?)(?=\n\s*(?:\*\*\s*)?(?:ACTION ITEMS|SUGGESTED RESPONSE|SUMMARY)\s*(?:\*\*)?\s*:|\Z)", re.IGNORECASE | re.DOTALL)
_RE_ACTIONS  = re.compile(r"(?:\*\*\s*)?ACTION ITEMS\s*(?:\*\*)?\s*:\s*(.+?)(?=\n\s*(?:\*\*\s*)?(?:SUGGESTED RESPONSE|SUMMARY|INTENT)\s*(?:\*\*)?\s*:|\Z)", re.IGNORECASE | re.DOTALL)
_RE_RESPONSE = re.compile(r"(?:\*\*\s*)?SUGGESTED RESPONSE\s*(?:\*\*)?\s*:\s*(.+?)\Z", re.IGNORECASE | re.DOTALL)
# Match bullet rows like  "- foo" or "* foo" or "1. foo".
_RE_BULLET   = re.compile(r"^\s*(?:[-*•]|\d+\.)\s*(.+?)\s*$", re.MULTILINE)


def _parse_response(text: str) -> dict:
    """Extract the four labeled sections from the model output. Missing
    sections come back as empty strings/lists rather than None so the UI
    can render a single uniform shape."""
    text = (text or "").strip()
    out = {"summary": "", "intent": "", "action_items": [], "suggested_response": ""}

    if m := _RE_SUMMARY.search(text):
        out["summary"] = _clean(m.group(1))
    if m := _RE_INTENT.search(text):
        out["intent"] = _clean(m.group(1))
    if m := _RE_ACTIONS.search(text):
        block = m.group(1)
        items = [b.strip() for b in _RE_BULLET.findall(block) if b.strip()]
        # Filter out the model echoing the prompt's "(Add a second bullet only if…)" hint.
        items = [i for i in items if not i.lower().startswith("(add a second")]
        out["action_items"] = items[:5]  # safety cap
    if m := _RE_RESPONSE.search(text):
        out["suggested_response"] = _clean(m.group(1))
    return out


def _clean(s: str) -> str:
    """Strip whitespace and surrounding bold/quote markers the model sometimes adds."""
    s = s.strip()
    # Drop leading/trailing ** markdown bold
    s = re.sub(r"^\*\*\s*|\s*\*\*$", "", s)
    # Drop wrapping quotes
    s = s.strip().strip('"').strip("'").strip()
    return s


def generate_summary(
    transcript_text: str,
    caller_name: Optional[str] = None,
    caller_phone: Optional[str] = None,
) -> dict:
    """
    Call Phi-3 mini and return a dict with the structured fields plus
    bookkeeping (status, error_message, duration_ms). Never raises — any
    failure is surfaced via the `status` and `error_message` fields so the
    caller can decide whether to persist or skip.
    """
    started_ms = int(time.time() * 1000)

    if not transcript_text or not transcript_text.strip():
        return {
            "status": "skipped",
            "summary": "", "intent": "", "action_items": [], "suggested_response": "",
            "error_message": "No transcript text to summarise.",
            "duration_ms": 0,
        }

    prompt = _build_prompt(transcript_text, caller_name, caller_phone)
    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

    try:
        from openai import OpenAI
        client = OpenAI(base_url=ollama_url, api_key="ollama", timeout=GEN_TIMEOUT_SECONDS)

        resp = client.chat.completions.create(
            model="phi3:mini",
            max_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.3,  # low — we want consistent structured output
            messages=[{"role": "user", "content": prompt}],
            extra_body={"keep_alive": -1},
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("Empty response from model")

        parsed = _parse_response(text)
        # If we didn't even get a summary, treat as a parsing failure.
        if not parsed["summary"]:
            raise RuntimeError(f"Could not parse SUMMARY from model output: {text[:200]!r}")

        return {
            "status": "success",
            **parsed,
            "error_message": None,
            "duration_ms": int(time.time() * 1000) - started_ms,
        }

    except Exception as e:
        err_type = type(e).__name__
        err_msg  = str(e).splitlines()[0][:300] if str(e) else "unknown"
        logger.warning(f"AI summary generation failed: {err_type}: {err_msg}")
        return {
            "status": "error",
            "summary": "", "intent": "", "action_items": [], "suggested_response": "",
            "error_message": f"{err_type}: {err_msg}",
            "duration_ms": int(time.time() * 1000) - started_ms,
        }


def generate_and_store(voicemail) -> dict:
    """
    Convenience wrapper: pull the transcript + caller info off a Voicemail,
    call generate_summary, and persist the result onto its Insight row.
    Caller is responsible for committing the session.

    Returns the same dict generate_summary returned, for logging.
    """
    from app import db
    from app.models.voicemail import Insight

    transcript_text = (
        voicemail.transcript.text
        if voicemail.transcript and voicemail.transcript.text
        else ""
    )
    ci = voicemail.caller_info or {}
    result = generate_summary(
        transcript_text,
        caller_name=ci.get("caller_name"),
        caller_phone=ci.get("phone"),
    )

    insight = voicemail.insights
    if insight is None:
        # No NLP insight row yet — create a bare one so we have something to attach to.
        # The unique(voicemail_id) constraint means a concurrent caller could
        # win this insert; re-fetch on IntegrityError so we update their row
        # rather than 500ing.
        from sqlalchemy.exc import IntegrityError
        insight = Insight(voicemail_id=voicemail.id)
        db.session.add(insight)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            insight = Insight.query.filter_by(voicemail_id=voicemail.id).one()

    # Defensive trim — even though ai_intent is now TEXT, keep it short so the
    # UI stays readable. Phi-3 sometimes ignores the "one sentence" instruction
    # and dumps action items into the intent field.
    intent_val = result.get("intent") or None
    if intent_val and len(intent_val) > 500:
        intent_val = intent_val[:497].rstrip() + "…"

    insight.ai_summary            = result.get("summary") or None
    insight.ai_intent             = intent_val
    insight.ai_action_items       = result.get("action_items") or None
    insight.ai_suggested_response = result.get("suggested_response") or None
    insight.ai_status             = result.get("status")
    insight.ai_error              = result.get("error_message")
    insight.ai_duration_ms        = result.get("duration_ms")
    insight.ai_generated_at       = datetime.utcnow()

    return result
