"""The buddy's agent loop (spec §14).

Manual tool loop over the Messages API — manual because every turn ends in a
post-check that may rewrite the reply, opinions are logged with the exact tool
snapshot, and text deltas stream out over SSE while tools run in between.

Context assembly order (spec): system prompt (mandate + output rules + factor
legend + active facts, cache-stable) -> per-turn context block (observations
relevant to the question, today's plan + frozen day read, checklist entries,
chat summary) -> last 20 turns -> the question. Tool results accumulate inside
the turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from ..config import Settings
from ..factors import Factor, factor_legend
from ..tools import ToolContext, anthropic_tools, run_tool
from . import postcheck, voice
from .memory import Memory

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
MAX_TOKENS = 8000
MAX_VOICE_RETRIES = 2

MANDATE = """You are the trading buddy on Jay's desk — a grounded second set of eyes on NQ futures. You talk like a sharp friend sitting next to him, not like a report.

Hard rules:
- You are READ-ONLY. You never place, modify, or cancel orders, and you never claim to. You may suggest; Jay acts.
- Any directional or go/no-go opinion must rest on tool results you pulled THIS turn. No tools, no view.
- Never compute flow, levels, or scorecard values yourself — call the tools; level_read, day_read and manage_read are deterministic code.
- If a tool returns stale data, your first line is plainly: "Data's a few seconds behind — take this loosely."
- If a tool fails, name what you couldn't see rather than papering over it.
- Jay's declared rules: max 2 trades/day, conviction floor 8 (stamped, not blocked), stop >= 15 pts, 15:55 ET shutoff.
- When his question contradicts the session plan, say so in one clause.
- If Jay is IN a trade, the answer is about HIS trade, not the abstract market.
- Sweeps are context, never a reason to enter (tested negative — SFB); "flow confirms the sweep" is not a thing you say.

Voice (enforced by a post-check; violations get your reply regenerated):
- ONE short paragraph. Default <= 3 sentences, <= 60 words. Hard cap 4 sentences. No headers, no bullets, no tables.
- Verdict first: "No." / "Yes, but..." / "Not yet." / "Hold." / "Two-sided today." Then the single strongest reason in plain words; a second reason is one clause, never a list.
- At most ONE number, and only when it's actionable (a price, a point distance, a time). No percentages, no ratios, no "Q71".
- Plain English only. NEVER say: delta, CVD, Q-rank, POC, VAH/VAL, VWAP, imbalance, footprint, gamma, sigma, MAE, MFE, impulse, aggressor, percentile, expectancy, liquidity, divergence. Say what they MEAN (translation table below).
- When the read leans on untested framework, one plain clause: "...that's the setup reading, not something we've proven." Validated factors need no caveat. Never write "validated" or "discretionary" in the spoken text.
- No hedging ("it depends", "could go either way", "many factors") unless the honest answer is two-sided — then: "Two-sided — no edge either way right now" plus what would tip it.
- No coaching tone: no "remember to...", no "great question", no encouragement, no risk lectures unless the governor is nearly out.

Output contract — every reply has two parts:
1. The spoken paragraph (rules above). This is all Jay hears.
2. A fenced detail block, which renders collapsed and is logged, built ONLY from tool results this turn:
```detail
{"verdict": "<one line>", "direction": "long|short|flat|no-go|two-sided", "confidence": 0.0, "factors": [{"key": "<factor key>", "tag": "validated|discretionary", "value": "<short>", "cite": "<study or null>"}], "numbers": {"<name>": 0}, "flip": "<what changes the call>"}
```
Exception: if Jay explicitly asks "why", "details", "show me", or "numbers", the structured breakdown IS the visible answer (factor by factor, tags and studies included, all the numbers) and you skip the detail fence."""


class BuddyAgent:
    def __init__(self, s: Settings, ctx: ToolContext, memory: Memory, factors: dict[str, Factor]):
        self._s = s
        self._ctx = ctx
        self._memory = memory
        self._factors = factors
        self._voice = voice.load_voice(s.voice_path)
        self._client = AsyncAnthropic(api_key=s.anthropic_api_key)

    def _system(self, facts: list[str]) -> list[dict[str, Any]]:
        facts_block = "\n".join(f"- {f}" for f in facts) if facts else "(none recorded yet)"
        translation = "\n".join(f"- instead of “{k}”: say “{v}”" for k, v in self._voice.translation.items())
        return [
            {
                "type": "text",
                "text": f"{MANDATE}\n\n## Translation table (jargon → what you say)\n{translation}\n\n"
                f"## Factor legend (from factors.yaml — the only authority on tags)\n"
                f"{factor_legend(self._factors)}\n\n## Durable facts Jay has confirmed\n{facts_block}",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def _context_block(self, question: str) -> str:
        obs = await self._memory.relevant_observations(question)
        today = await self._memory.today_context()
        chat = await self._memory.chat_session()
        parts: list[str] = []
        if today.get("session"):
            s = today["session"]
            parts.append(
                "TODAY'S SESSION PLAN: bias={htf_bias}; levels={key_levels}; hunting={hunting}; "
                "invalidation={invalidation}".format(**{k: s.get(k) for k in ("htf_bias", "key_levels", "hunting", "invalidation")})
            )
            if s.get("day_read_json"):
                parts.append(f"FROZEN PRE-OPEN DAY READ: {json.dumps(s['day_read_json'])[:1200]}")
        else:
            parts.append("TODAY'S SESSION PLAN: none written yet.")
        if today.get("checklists"):
            parts.append("CHECKLIST ENTRIES TODAY: " + json.dumps(today["checklists"])[:1200])
        if obs:
            parts.append("OBSERVATIONS ABOUT JAY (surface only if relevant):\n" + "\n".join(f"- {o}" for o in obs))
        if chat.get("summary"):
            parts.append("EARLIER TODAY (summary): " + chat["summary"])
        return "\n\n".join(parts)

    async def chat(self, question: str) -> AsyncIterator[dict[str, Any]]:
        """Yields SSE-able events: {"kind": "delta"|"tool"|"final", ...}."""
        facts = await self._memory.active_facts()
        chat_session = await self._memory.chat_session()
        history = await self._memory.recent_turns(chat_session["id"])
        context_block = await self._context_block(question)

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"[desk context — assembled by the system, not Jay]\n{context_block}"},
            {"role": "assistant", "content": "Context noted."},
        ]
        for t in history:
            messages.append({"role": t["role"], "content": t["content"] or "…"})
        messages.append({"role": "user", "content": question})

        tools_used: list[str] = []
        tool_snapshot: dict[str, Any] = {}
        any_stale = False
        reply_text = ""

        for _round in range(MAX_TOOL_ROUNDS + 1):
            # each round starts fresh: only the final round's text is the reply,
            # and the gate keeps the ```detail fence out of the live stream
            reply_text = ""
            gate = voice.StreamGate()
            async with self._client.messages.stream(
                model=self._s.agent_model,
                max_tokens=MAX_TOKENS,
                system=self._system(facts),
                messages=messages,
                tools=anthropic_tools(),
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                        reply_text += event.delta.text
                        visible = gate.feed(event.delta.text)
                        if visible:
                            yield {"kind": "delta", "text": visible}
                response = await stream.get_final_message()
            tail = gate.flush()
            if tail:
                yield {"kind": "delta", "text": tail}

            if response.stop_reason == "refusal":
                reply_text = "I can't answer that one. Ask me about the tape, the journal, or the plan."
                break
            if response.stop_reason != "tool_use":
                break

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = await run_tool(self._ctx, block.name, dict(block.input or {}))
                tools_used.append(block.name)
                tool_snapshot[f"{len(tools_used)}:{block.name}"] = result
                if result.get("ok") and result.get("stale"):
                    any_stale = True
                yield {"kind": "tool", "name": block.name, "ok": bool(result.get("ok")),
                       "stale": bool(result.get("stale", False)), "input": dict(block.input or {})}
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result, default=str)[:20000]}
                )
            messages.append({"role": "user", "content": results})
        else:
            log.warning("agent hit MAX_TOOL_ROUNDS")

        # -- Part B pipeline: split spoken/detail, voice-check, regenerate ----
        spoken, detail = voice.parse_reply(reply_text)
        classification = await postcheck.classify(self._client, self._s.classifier_model, question, spoken)

        detail_asked = voice.wants_detail(question)
        violations = [] if detail_asked else voice.check_spoken(spoken, self._voice)
        retries = 0
        while violations and retries < MAX_VOICE_RETRIES:
            retries += 1
            log.info("voice violations (try %d): %s", retries, violations)
            messages.append({"role": "assistant", "content": reply_text or "…"})
            messages.append({"role": "user", "content": voice.correction_prompt(violations, self._voice)})
            try:
                resp = await self._client.messages.create(
                    model=self._s.agent_model,
                    max_tokens=2000,
                    system=self._system(facts),
                    messages=messages,
                )
                reply_text = "".join(b.text for b in resp.content if b.type == "text")
            except Exception:  # noqa: BLE001 — regeneration failure falls through to fallback
                log.exception("voice regeneration failed")
                break
            spoken, new_detail = voice.parse_reply(reply_text)
            detail = new_detail or detail
            violations = voice.check_spoken(spoken, self._voice)

        if violations:
            spoken = self._voice.fallback
            classification = postcheck.Classification(False, "none", None, None, None, None)

        checked = postcheck.apply_rules(
            spoken, classification, tools_used, any_stale, self._factors,
            stale_line=self._voice.stale_line, offline_line=self._voice.offline_line,
            framework_clause=self._voice.framework_clause,
        )

        opinion_id = None
        if checked.classification.has_opinion:
            opinion_id = await self._log_opinion(question, checked, tool_snapshot, detail)

        await self._memory.append(chat_session["id"], "user", question)
        await self._memory.append(
            chat_session["id"], "assistant", checked.text,
            tool_calls={"tools": tools_used, "stale": any_stale, "opinion_id": opinion_id, "detail": detail},
        )
        await self._maybe_summarize(chat_session)

        yield {
            "kind": "final",
            "text": checked.text,
            "detail": detail,
            "rewritten": checked.rewritten or retries > 0,
            "tools_used": tools_used,
            "stale": any_stale,
            "opinion": checked.classification.type if checked.classification.has_opinion else None,
        }

    async def _log_opinion(
        self,
        question: str,
        checked: postcheck.CheckedResponse,
        snapshot: dict[str, Any],
        detail: dict[str, Any] | None = None,
    ) -> str | None:
        c = checked.classification
        market = snapshot_price = None
        for key, res in snapshot.items():
            if key.split(":", 1)[1] == "market_state" and res.get("ok"):
                market = res["data"]
        if c.price is not None:
            snapshot_price = c.price
        elif market:
            snapshot_price = market.get("last")

        trade_id = None
        pos = next((r for k, r in snapshot.items() if k.split(":", 1)[1] == "position" and r.get("ok")), None)
        if pos and (pos["data"].get("positions") or []):
            trade_id = await self._memory.open_trade_id()

        factors_json = {
            "direction": c.direction,
            "validated_cited": [f.key for f in self._factors.values() if f.tag == "validated" and f.name.split(" ")[0].lower() in checked.text.lower()],
            "detail": detail,  # the structured block from the same model call (addendum B5)
        }
        try:
            res = (
                await self._ctx.db.table("opinions")
                .insert(
                    {
                        "price": snapshot_price,
                        "question": question[:2000],
                        "type": c.type,
                        "verdict": (c.verdict or checked.text.splitlines()[0])[:500],
                        "confidence": c.confidence,
                        "factors_json": factors_json,
                        "tool_snapshot_json": json.loads(json.dumps(snapshot, default=str)[:100000]),
                        "trade_id": trade_id,
                    }
                )
                .execute()
            )
            return res.data[0]["id"]
        except Exception:  # noqa: BLE001
            log.exception("opinion insert failed")
            return None

    async def _maybe_summarize(self, chat_session: dict[str, Any]) -> None:
        count = await self._memory.turn_count(chat_session["id"])
        if count == 0 or count % (2 * 10) != 0:  # every ~20 rows (10 exchanges)
            return
        turns = await self._memory.recent_turns(chat_session["id"], limit=40)
        transcript = "\n".join(f"{t['role']}: {t['content'][:400]}" for t in turns)
        try:
            resp = await self._client.messages.create(
                model=self._s.classifier_model,
                max_tokens=600,
                system="Summarize this trading-desk chat in <=150 words: open questions, views given, "
                "positions discussed, anything Jay said about his state of mind. Plain prose.",
                messages=[{"role": "user", "content": transcript[:12000]}],
            )
            summary = next((b.text for b in resp.content if b.type == "text"), "")
            if summary:
                await self._memory.set_summary(chat_session["id"], summary)
        except Exception:  # noqa: BLE001
            log.exception("summary rewrite failed")
