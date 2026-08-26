"""Output post-check (spec §14): the four rules, enforced on every response.

1. A directional / go-no-go opinion must cite >=1 tool result from this turn.
2. Discretionary factors are labeled as such.
3. A stale flag on any used tool is surfaced in the first line.
4. No order verbs as actions — the buddy suggests, it never claims to act.

Classification is regex-gated: only responses that look opinion-shaped go to
the cheap model, so most turns cost nothing extra.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic

from ..factors import Factor

log = logging.getLogger(__name__)

_OPINION_HINT = re.compile(
    r"\b(long|short|buy|sell|fade|go[- ]?no[- ]?go|no[- ]go|lean|bias|flip|take (it|the trade)|"
    r"skip (it|this)|enter|exit|flatten|hold|add|trim|stop to|verdict)\b",
    re.I,
)

# "I'll set a stop", "I've moved your target", "let me cancel..."
_ACT_CLAIM = re.compile(
    r"\b(I'?ll|I will|I'?ve|I have|let me)\s+(set|place|move|cancel|adjust|modify|close|flatten|"
    r"submit|enter|exit|put in|trail|tighten|widen)\b[^.\n]*",
    re.I,
)

CLASSIFIER_SYSTEM = (
    "You classify a trading assistant's reply. Output ONLY a JSON object, nothing else:\n"
    '{"has_opinion": bool, "type": "level"|"day"|"manage"|"none", '
    '"direction": "long"|"short"|"flat"|"no-go"|null, "confidence": float|null, '
    '"price": float|null, "verdict": "<one short line>"|null}\n'
    "has_opinion is true only for a directional or go/no-go view on the market or on "
    "managing a position — not for factual reports, journal stats, or descriptions. "
    "type: level = about entering/fading a specific price; day = session directional bias; "
    "manage = what to do with the open position. confidence in [0,1] if the reply states or "
    "clearly implies one, else null. price = the price the opinion is about, if any."
)


@dataclass
class Classification:
    has_opinion: bool
    type: str  # level | day | manage | none
    direction: str | None
    confidence: float | None
    price: float | None
    verdict: str | None


@dataclass
class CheckedResponse:
    text: str
    classification: Classification
    rewritten: bool


async def classify(client: AsyncAnthropic, model: str, question: str, reply: str) -> Classification:
    none = Classification(False, "none", None, None, None, None)
    if not _OPINION_HINT.search(reply):
        return none
    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=256,
            system=CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": f"Question:\n{question[:1000]}\n\nReply:\n{reply[:3000]}"}],
        )
        raw = next((b.text for b in resp.content if b.type == "text"), "")
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return none
        d = json.loads(m.group(0))
        typ = d.get("type") if d.get("type") in ("level", "day", "manage") else "none"
        has = bool(d.get("has_opinion")) and typ != "none"
        return Classification(
            has_opinion=has,
            type=typ if has else "none",
            direction=d.get("direction"),
            confidence=d.get("confidence"),
            price=d.get("price"),
            verdict=d.get("verdict"),
        )
    except Exception:  # noqa: BLE001 — classifier failure must not break chat
        log.exception("opinion classifier failed")
        return none


def apply_rules(
    reply: str,
    classification: Classification,
    tools_used: list[str],
    any_stale: bool,
    factors: dict[str, Factor],
) -> CheckedResponse:
    text = reply
    rewritten = False

    # Rule 4 — never claim to act on orders. Rewrite claims into suggestions:
    # "I'll set a stop at 20165" -> "suggestion — set a stop at 20165".
    def _suggest(m: re.Match[str]) -> str:
        full = m.group(0)
        idx = full.lower().find(m.group(2).lower())
        return "suggestion — " + full[idx:]

    if _ACT_CLAIM.search(text):
        text = _ACT_CLAIM.sub(_suggest, text)
        rewritten = True

    # Rule 1 — an opinion with zero tool results this turn is not allowed to stand.
    if classification.has_opinion and not tools_used:
        text = (
            "I can't see the tape right now (no tool data reached me this turn), so no view. "
            "Ask again and I'll pull live state first."
        )
        rewritten = True
        classification = Classification(False, "none", None, None, None, None)
        return CheckedResponse(text=text, classification=classification, rewritten=rewritten)

    # Rule 2 — discretionary factors named without the label get labeled.
    if classification.has_opinion and "discretionary" not in text.lower():
        mentioned = [
            f.name
            for f in factors.values()
            if f.tag == "discretionary" and re.search(re.escape(f.name.split(" (")[0]), text, re.I)
        ]
        if mentioned:
            text += f"\n\n(discretionary framework, not tested edge: {', '.join(mentioned)})"
            rewritten = True

    # Rule 3 — stale data is the first line, not a footnote.
    if any_stale and not text.lstrip().lower().startswith(("⚠", "stale", "warning")):
        text = "⚠️ STALE — engine heartbeat >5s old; everything below is from the last snapshot.\n" + text
        rewritten = True

    return CheckedResponse(text=text, classification=classification, rewritten=rewritten)
