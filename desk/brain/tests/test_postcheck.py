"""Output rules under adversarial responses (spec §21, voice rules per Part B)."""

from pathlib import Path

from desk_brain.agent.postcheck import Classification, apply_rules
from desk_brain.factors import load_factors

FACTORS = load_factors(Path(__file__).resolve().parents[2] / "factors.yaml")

OPINION = Classification(True, "level", "short", 0.7, 20150.0, "short lean")
NONE = Classification(False, "none", None, None, None, None)


def test_opinion_without_tools_is_rewritten_to_no_view():
    out = apply_rules("Short it here, easy fade.", OPINION, tools_used=[], any_stale=False, factors=FACTORS)
    assert out.rewritten
    assert "can't see the flow" in out.text.lower()
    assert not out.classification.has_opinion


def test_opinion_with_tools_stands():
    out = apply_rules("No — the buying isn't the size we've proven works.", OPINION, ["flow"], False, FACTORS)
    assert out.text.startswith("No")


def test_stale_flag_surfaces_first_line_plainly():
    out = apply_rules("Leaning short.", OPINION, ["flow"], True, FACTORS)
    assert out.text.splitlines()[0].startswith("Data's a few seconds behind")


def test_order_verbs_rewritten_to_suggestions():
    out = apply_rules("Looks weak. I'll set a stop at 20165 for you.", NONE, ["flow"], False, FACTORS)
    assert "I'll set" not in out.text
    assert "suggestion — set a stop at 20165" in out.text


def test_framework_clause_appended_plainly():
    text = "Short — you're at a 4H swing high per the level hierarchy and AMD phase looks distributive."
    out = apply_rules(text, OPINION, ["levels"], False, FACTORS)
    assert "setup reading" in out.text.lower()
    assert "discretionary" not in out.text.lower()  # no jargon labels in spoken text


def test_clean_answer_untouched():
    text = "No. Sellers keep leaning on this level and it keeps holding — that's the setup reading, not something we've proven."
    out = apply_rules(text, OPINION, ["flow", "levels"], False, FACTORS)
    assert out.text == text
    assert not out.rewritten
