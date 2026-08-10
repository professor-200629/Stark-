"""
Turning a triage brief into something a person can listen to.

Text written for the eye reads badly out loud. Incident IDs become "eye en see
dash one one three one", multi-clause runbook steps run past the point where a
listener has stopped following, and a bulleted list has no audible structure.

`speakable()` produces a short spoken briefing with the same content and the
same citations, ordered the way an on-call engineer would want to hear it:
what this is, what fixed it last time, what not to waste time on, how long it
took. Roughly 20 seconds at normal speaking rate.
"""

from __future__ import annotations

import re
from typing import Any

_INC_RE = re.compile(r"\bINC-(\d{3,6})\b")
_MONTHS = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}


#: See agent.normalise_dashes — an LLM writing "INC\u20111131" would otherwise be
#: read aloud character by character.
_DASHES = re.compile(r"[\u2010-\u2015\u2212]")


def _say_ids(text: str) -> str:
    """INC-1131 -> incident 1131, so a speech synthesiser says something sensible."""
    return _INC_RE.sub(lambda m: f"incident {m.group(1)}", _DASHES.sub("-", text or ""))


def _first_clause(text: str, limit: int = 150) -> str:
    """
    Runbook steps are comma-spliced instructions. Speak the first actionable
    clause only — a listener loses the thread long before the third comma.
    """
    text = (text or "").strip()
    for sep in (". ", "; ", ", then ", ", and "):
        if sep not in text:
            continue
        head = text.split(sep)[0]
        if 25 < len(head) <= limit:
            return head.rstrip(".;, ")
    if len(text) <= limit:
        return text.rstrip(".;, ")
    # Hard cut, but land on a word boundary rather than mid-syllable.
    cut = text[:limit]
    return cut[: cut.rfind(" ")].rstrip(".;, ") if " " in cut else cut


def _when(iso: str | None) -> str:
    if not iso or len(iso) < 7:
        return ""
    month = _MONTHS.get(iso[5:7], "")
    return f" from {month}" if month else ""


def speakable(brief: dict[str, Any]) -> str:
    """Compose the spoken version of a triage brief."""
    if not brief.get("memory_grounded"):
        parts = [
            "I have no precedent for this one.",
            "Nothing in memory resembles this alert, so I'm treating it as novel rather than "
            "guessing at a match.",
        ]
        actions = brief.get("first_actions") or []
        if actions:
            parts.append(f"My suggestion: {_first_clause(actions[0].get('step', ''))}.")
        parts.append("Once you've resolved it, record the outcome and I'll know it next time.")
        return _say_ids(" ".join(parts))

    recalled = brief.get("recalled_incidents") or []
    similar = brief.get("similar_incidents") or []
    count = len(similar) or len(recalled)
    parts: list[str] = []

    top_id = ""
    top_when = ""
    if recalled:
        top_id = recalled[0].get("incident_id", "")
        top_when = _when(recalled[0].get("opened_at"))
    elif similar:
        top_id = similar[0].get("incident_id", "")

    plural = "incident" if count == 1 else "incidents"
    if top_id:
        parts.append(
            f"I found {count} relevant {plural}. The strongest precedent is "
            f"{_say_ids(top_id)}{top_when}."
        )
    else:
        parts.append(f"I found {count} relevant {plural}.")

    causes = brief.get("likely_root_causes") or []
    if causes:
        parts.append(f"Most likely cause: {_first_clause(causes[0].get('cause', ''), 115)}.")

    fix = next(
        (a for a in (brief.get("first_actions") or []) if a.get("source_incident")),
        None,
    ) or (brief.get("first_actions") or [None])[0]
    if fix:
        step = _first_clause(fix.get("step", ""))
        src = fix.get("source_incident")
        parts.append(
            f"Its successful resolution was: {step}."
            if not src
            else f"What resolved {_say_ids(src)} was: {step}."
        )

    warnings = brief.get("do_not_do") or []
    if warnings:
        w = warnings[0]
        src_id = w.get("source_incident") or ""
        where = f" during {src_id}" if src_id else ""
        parts.append(
            f"One thing not to repeat{where}: {_first_clause(w.get('action', ''))}. "
            "That was a dead end."
        )

    mttr = brief.get("estimated_mttr_minutes")
    if mttr:
        parts.append(f"Similar incidents took about {int(mttr)} minutes to resolve.")

    # Normalise every incident ID at the end, wherever it came from.
    return _say_ids(" ".join(parts))


#: Spoken commands the UI understands after the wake word.
VOICE_COMMANDS = {
    "investigate": "Run the memory-grounded triage on the current alert",
    "compare": "Run both briefs, memory off and memory on",
    "why": "Read the likely root causes and their supporting incidents",
    "what should I avoid": "Read the do-not-do list",
    "next alert": "Move to the next demo alert",
}
