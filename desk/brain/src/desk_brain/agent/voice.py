"""Voice rules (addendum Part B): the buddy talks like a sharp friend, not a
report. This module loads desk/voice.yaml and enforces B1 on every spoken
reply and ping: length, verdict-first shape is prompted (not checkable),
plain English (forbidden jargon), at most one number, no hedging.

The model emits the spoken paragraph followed by a fenced ```detail JSON
block; StreamGate keeps the fence out of the live delta stream and
parse_reply() splits the two after the turn."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DETAIL_FENCE = re.compile(r"```(?:detail|json)?\s*\n(.*?)```", re.S)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_NUMBER = re.compile(r"\d[\d,.:]*")
_WORD = re.compile(r"[A-Za-z0-9'’\-]+")
_DETAIL_ASK = re.compile(r"\b(why|details?|show me|numbers|break(?: it)? down|explain|walk me through)\b", re.I)


@dataclass(frozen=True)
class VoiceConfig:
    max_sentences: int
    hard_max_sentences: int
    max_words: int
    max_numbers: int
    forbidden_terms: list[str]
    forbidden_patterns: list[str]
    hedging_phrases: list[str]
    fallback: str
    stale_line: str
    offline_line: str
    framework_clause: str
    translation: dict[str, str] = field(default_factory=dict)


def load_voice(path: Path) -> VoiceConfig:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    limits = doc.get("limits") or {}
    return VoiceConfig(
        max_sentences=int(limits.get("max_sentences", 3)),
        hard_max_sentences=int(limits.get("hard_max_sentences", 4)),
        max_words=int(limits.get("max_words", 60)),
        max_numbers=int(limits.get("max_numbers", 1)),
        forbidden_terms=list(doc.get("forbidden_terms") or []),
        forbidden_patterns=list(doc.get("forbidden_patterns") or []),
        hedging_phrases=list(doc.get("hedging_phrases") or []),
        fallback=doc.get("fallback", "Can't give a clean answer on that — ask again."),
        stale_line=doc.get("stale_line", "Data's a few seconds behind — take this loosely."),
        offline_line=doc.get("offline_line", "I can't see the flow right now, so no call."),
        framework_clause=doc.get("framework_clause", "that's the setup reading, not something we've proven"),
        translation=dict(doc.get("translation") or {}),
    )


def wants_detail(question: str) -> bool:
    """B1.12 — 'why', 'details', 'show me', 'numbers' unlock the structured
    block; everything else gets the plain paragraph."""
    return bool(_DETAIL_ASK.search(question))


def parse_reply(text: str) -> tuple[str, dict[str, Any] | None]:
    """Split a model reply into (spoken, detail). The detail block is the
    first fenced JSON block; anything outside it is the spoken paragraph."""
    detail: dict[str, Any] | None = None
    m = _DETAIL_FENCE.search(text)
    if m:
        try:
            parsed = json.loads(m.group(1))
            detail = parsed if isinstance(parsed, dict) else {"detail": parsed}
        except json.JSONDecodeError:
            detail = None
    spoken = _DETAIL_FENCE.sub("", text).strip()
    return spoken, detail


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]


def check_spoken(text: str, voice: VoiceConfig, detail_ok: bool = False) -> list[str]:
    """B1 violations, empty when the reply is clean. `detail_ok` lifts the
    length caps (the user asked for detail) but never the jargon rules —
    even the structured answer's prose stays plain."""
    violations: list[str] = []
    stripped = text.strip()
    if not stripped:
        return ["empty reply"]

    if not detail_ok:
        n_sent = len(sentences(stripped))
        if n_sent > voice.hard_max_sentences:
            violations.append(f"{n_sent} sentences (hard cap {voice.hard_max_sentences})")
        n_words = len(_WORD.findall(stripped))
        if n_words > voice.max_words * 1.2:  # small grace over the target
            violations.append(f"{n_words} words (cap {voice.max_words})")
        numbers = _NUMBER.findall(stripped)
        if len(numbers) > voice.max_numbers:
            violations.append(f"{len(numbers)} numbers ({', '.join(numbers[:4])}) — at most {voice.max_numbers}")

    lowered = stripped.lower()
    for term in voice.forbidden_terms:
        if re.search(rf"\b{re.escape(term)}\b", stripped, re.I):
            violations.append(f"jargon: {term!r}")
    for pat in voice.forbidden_patterns:
        m = re.search(pat, stripped)
        if m:
            violations.append(f"jargon pattern: {m.group(0)!r}")
    if "two-sided" not in lowered:
        for phrase in voice.hedging_phrases:
            if phrase.lower() in lowered:
                violations.append(f"hedging: {phrase!r}")
    return violations


def correction_prompt(violations: list[str], voice: VoiceConfig) -> str:
    return (
        "Your spoken reply broke the voice rules: "
        + "; ".join(violations)
        + f". Rewrite it: at most {voice.max_sentences} sentences and {voice.max_words} words, "
        f"verdict first, plain English (no trading jargon — say what the thing means instead), "
        f"at most {voice.max_numbers} number and only if it's actionable. "
        "Keep the same verdict and reasoning. Reply with ONLY the corrected paragraph, then the "
        "```detail block unchanged."
    )


class StreamGate:
    """Keeps the ```detail fence (and everything after it) out of the live
    delta stream, holding back a small tail so a fence split across deltas
    can't leak."""

    MARKER = "```"

    def __init__(self) -> None:
        self._buffer = ""
        self._suppressing = False

    def feed(self, delta: str) -> str:
        if self._suppressing:
            return ""
        self._buffer += delta
        idx = self._buffer.find(self.MARKER)
        if idx != -1:
            out = self._buffer[:idx]
            self._buffer = ""
            self._suppressing = True
            return out
        # emit all but a tail that could be the start of a split marker
        keep = len(self.MARKER) - 1
        if len(self._buffer) <= keep:
            return ""
        out = self._buffer[:-keep]
        self._buffer = self._buffer[-keep:]
        return out

    def flush(self) -> str:
        if self._suppressing:
            return ""
        out, self._buffer = self._buffer, ""
        return out
