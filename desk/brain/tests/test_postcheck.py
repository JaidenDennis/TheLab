"""Output rules under adversarial responses (spec §21)."""

from pathlib import Path

from desk_brain.agent.postcheck import Classification, apply_rules
from desk_brain.factors import load_factors

FACTORS = load_factors(Path(__file__).resolve().parents[2] / "factors.yaml")

OPINION = Classification(True, "level", "short", 0.7, 20150.0, "short lean")
NONE = Classification(False, "none", None, None, None, None)


def test_opinion_without_tools_is_rewritten_to_no_view():
    out = apply_rules("Short it here, easy fade.", OPINION, tools_used=[], any_stale=False, factors=FACTORS)
    assert out.rewritten
    assert "no view" in out.text
    assert not out.classification.has_opinion


def test_opinion_with_tools_stands():
    out = apply_rules("Verdict: NO-GO — flow at Q71, needs Q90+.", OPINION, ["flow"], False, FACTORS)
    assert "NO-GO" in out.text


def test_stale_flag_surfaces_first_line():
    out = apply_rules("Verdict: short lean.", OPINION, ["flow"], True, FACTORS)
    assert out.text.splitlines()[0].startswith("⚠️ STALE")


def test_order_verbs_rewritten_to_suggestions():
    out = apply_rules("Looks weak. I'll set a stop at 20165 for you.", NONE, ["flow"], False, FACTORS)
    assert "I'll set" not in out.text
    assert "suggestion — set a stop at 20165" in out.text


def test_discretionary_factors_get_labeled():
    text = "Verdict: short lean — you're at a 4H swing high per the level hierarchy and AMD phase looks distributive."
    out = apply_rules(text, OPINION, ["levels"], False, FACTORS)
    assert "discretionary" in out.text.lower()


def test_clean_answer_untouched():
    text = "Verdict: NO-GO (lean) — flow not confirming. discretionary: at 4H swing high."
    out = apply_rules(text, OPINION, ["flow", "levels"], False, FACTORS)
    assert out.text == text
    assert not out.rewritten
