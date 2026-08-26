"""Loads desk/factors.yaml — the single source of truth for factor tags.

Tags change only by editing that file (spec §13). The agent's system prompt,
the deterministic reads, and the replay harness all consume this loader, so
a tag edit propagates everywhere or nowhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Factor:
    key: str
    name: str
    tag: str  # "validated" | "discretionary"
    study: str | None = None
    note: str | None = None


def load_factors(path: Path) -> dict[str, Factor]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, Factor] = {}
    for f in doc.get("factors", []):
        factor = Factor(
            key=f["key"],
            name=f["name"],
            tag=f["tag"],
            study=f.get("study"),
            note=f.get("note"),
        )
        if factor.tag not in ("validated", "discretionary"):
            raise ValueError(f"factor {factor.key}: bad tag {factor.tag!r}")
        out[factor.key] = factor
    return out


def factor_legend(factors: dict[str, Factor]) -> str:
    """Human-readable legend for the system prompt."""
    lines = ["VALIDATED (tested edge — cite the study):"]
    for f in factors.values():
        if f.tag == "validated":
            lines.append(f"- {f.name} [{f.study}]" + (f" — {f.note}" if f.note else ""))
    lines.append("DISCRETIONARY (framework, not tested edge — must be labeled as such):")
    for f in factors.values():
        if f.tag == "discretionary":
            lines.append(f"- {f.name}")
    return "\n".join(lines)
