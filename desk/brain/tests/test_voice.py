"""Part B voice rules: B1 enforcement, the B2 examples must pass, jargon and
number dumps must fail, and the stream gate keeps the detail fence hidden."""

from pathlib import Path

from desk_brain.agent.voice import (
    StreamGate,
    check_spoken,
    load_voice,
    parse_reply,
    sentences,
    wants_detail,
)

VOICE = load_voice(Path(__file__).resolve().parents[2] / "voice.yaml")

B2_EXAMPLES = [
    "No. Someone's soaking up the selling right at this level — sellers keep hitting it and price "
    "isn't moving, which usually means they lose. If it breaks under and keeps going with real "
    "selling behind it, then it's a short.",
    "Leaning short. We opened under yesterday's range and every push up has faded with sellers "
    "stepping in. Above the overnight high I'd drop that.",
    "Hold. You're up eight and the buying that got you in is still there. Your stop's fine where "
    "it is; I'd only worry if we lose 142.",
    "Not yet. There's buying, but it's not the size we've actually proven makes money. If it picks "
    "up hard in the next couple of minutes without price running away from you, then yes.",
    "No. You're already at your second trade for the day and adding here is chasing. Take what the "
    "trade gives you.",
    "Can't call it — the flow feed is behind by about ten seconds. Ask me again in a moment.",
    "Probably, for now. There's a big resting order that keeps refilling every time sellers hit it. "
    "If that order disappears or price starts trading through it with volume, it's done.",
]


def test_b2_examples_pass():
    for ex in B2_EXAMPLES:
        assert check_spoken(ex, VOICE) == [], f"B2 example flagged: {ex!r}"


def test_jargon_rejected():
    v = check_spoken("Delta is positive and CVD is rising, impulse at Q90 with POC below.", VOICE)
    joined = " ".join(v).lower()
    assert "delta" in joined and "cvd" in joined and "impulse" in joined
    assert any("Q90" in x for x in v)


def test_factor_labels_rejected():
    assert check_spoken("Short it — that's a validated factor.", VOICE)
    assert check_spoken("Short it, but the level part is discretionary.", VOICE)


def test_number_dump_rejected():
    v = check_spoken("Short 20150, stop 20165, target 20120.", VOICE)
    assert any("number" in x for x in v)


def test_one_actionable_number_ok():
    assert check_spoken("Hold. I'd only worry if we lose 20142.", VOICE) == []


def test_too_many_sentences_rejected():
    text = "No. " * 6
    assert any("sentences" in x for x in check_spoken(text, VOICE))


def test_hedging_rejected_unless_two_sided():
    assert check_spoken("It depends on many factors here.", VOICE)
    assert check_spoken("Two-sided — no edge either way right now; above the overnight high I'd lean long.", VOICE) == []


def test_detail_ok_lifts_length_not_jargon():
    long_plain = "Yes. " + "The buying keeps coming and the level keeps holding. " * 4
    assert check_spoken(long_plain, VOICE, detail_ok=True) == []
    assert check_spoken("The delta says yes.", VOICE, detail_ok=True)  # jargon still banned


def test_wants_detail():
    assert wants_detail("why?")
    assert wants_detail("show me the numbers")
    assert not wants_detail("short here?")


def test_parse_reply_splits_detail():
    text = 'Hold. The buying is still there.\n\n```detail\n{"verdict": "hold", "flip": "x"}\n```\n'
    spoken, detail = parse_reply(text)
    assert spoken == "Hold. The buying is still there."
    assert detail == {"verdict": "hold", "flip": "x"}
    spoken2, detail2 = parse_reply("Just words, no block.")
    assert spoken2 == "Just words, no block." and detail2 is None


def test_sentences_ignores_decimals():
    assert len(sentences("Stop at 20150.5 is fine. Hold.")) == 2


def test_stream_gate_hides_fence():
    gate = StreamGate()
    out = gate.feed("Hold. The buying is still there.\n\n``")
    out += gate.feed("`detail\n{\"verdict\":")
    out += gate.feed(' "hold"}\n```')
    out += gate.flush()
    assert "```" not in out and "verdict" not in out
    assert out.startswith("Hold.")


def test_stream_gate_flush_without_fence():
    gate = StreamGate()
    out = gate.feed("No. ")
    out += gate.feed("Flat tape.")
    out += gate.flush()
    assert out == "No. Flat tape."
