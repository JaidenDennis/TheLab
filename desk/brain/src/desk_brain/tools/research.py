"""Research tool: lexical index over the repo's study write-ups.

Chunks every root-level study .md on its ## sections at startup and scores
queries by keyword overlap. Deterministic and dependency-free on purpose —
findings must quote the documents, not a model's memory of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ToolContext, tool

# Root-level studies; HANDOFF and declarations included — they carry verdicts.
_EXCLUDE = {"README.md"}

_WORD = re.compile(r"[a-z0-9][a-z0-9\-\+]{1,}")

_STOP = frozenset(
    "the a an and or of to in on for with is are was were it this that at by from as be not no"
    " what which how when where does do did done than then over under per".split()
)


@dataclass
class Chunk:
    study: str
    heading: str
    text: str
    words: frozenset[str]


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in _STOP)


def build_index(repo_root: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(repo_root.glob("*.md")):
        if path.name in _EXCLUDE:
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        study = path.stem
        sections = re.split(r"^(## .+)$", body, flags=re.MULTILINE)
        head = sections[0]
        first_heading = head.splitlines()[0].lstrip("# ").strip() if head.strip() else study
        pieces: list[tuple[str, str]] = [(first_heading, head)]
        for i in range(1, len(sections) - 1, 2):
            pieces.append((sections[i].lstrip("# ").strip(), sections[i + 1]))
        for heading, text in pieces:
            text = text.strip()
            if len(text) < 80:
                continue
            chunks.append(Chunk(study=study, heading=heading, text=text, words=_tokenize(f"{study} {heading} {text}")))
    return chunks


def search_index(chunks: list[Chunk], query: str, k: int = 4) -> list[dict[str, Any]]:
    q = _tokenize(query)
    if not q:
        return []
    scored: list[tuple[float, Chunk]] = []
    for c in chunks:
        overlap = len(q & c.words)
        if overlap == 0:
            continue
        score = overlap / len(q)
        # verdict-ish sections outrank background when scores tie
        if re.search(r"verdict|result|finding|conclusion|deliverable", c.heading, re.I):
            score += 0.25
        scored.append((score, c))
    scored.sort(key=lambda t: -t[0])
    out = []
    for score, c in scored[:k]:
        excerpt = c.text if len(c.text) <= 2500 else c.text[:2500] + "\n…[truncated]"
        out.append({"study": c.study, "section": c.heading, "score": round(score, 3), "excerpt": excerpt})
    return out


_INDEX: list[Chunk] | None = None


@tool(
    "research",
    {
        "description": (
            "Search the repo's study write-ups (MAE-1, TFR, SFB, SDD, PFR-1, FRB-1, FMB-1, "
            "ICT-1, VPC, SSX-V3, NAIM, GFM, …) for findings, verdicts, and key numbers. "
            "Quote what it returns; do not answer study questions from memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 2}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
)
async def research(ctx: ToolContext, args: dict[str, Any]) -> Any:
    global _INDEX
    if _INDEX is None:
        _INDEX = build_index(ctx.settings.repo_root)
    hits = search_index(_INDEX, args["query"])
    return {"hits": hits, "indexed_studies": len({c.study for c in _INDEX})}
