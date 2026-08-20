"""
Pipeline-level integration coverage: Agent 4 (shortlisting) feeding directly
into Agent 5 (MVP readiness + matchmaking) against a small realistic pool —
the actual handoff main_app.py wires together, which no prior test exercised
end-to-end (test_agent5.py only ever fed it hand-built "already shortlisted"
lists). Groq is mocked to fail throughout: that's the one path deterministic
enough to assert exact output against, and it's the real degraded-mode path
the fallback logic in both agents exists for — if the LLM is down, the
pipeline must still produce a correct, sensibly-ranked result, not just "not
crash."
"""
import agents.agent4 as agent4
import agents.agent5 as agent5


def _idea(idea_id, domain, feasibility, roadmap_steps):
    return {
        "id": idea_id,
        "title": f"Idea {idea_id}",
        "domain": domain,
        "problem": "A real problem worth solving.",
        "solution": "A real, specific solution.",
        "base_scores": {"potential": 70, "feasibility": feasibility, "market_fit": 65},
        "roadmap": {"steps": roadmap_steps},
        "founder": {
            "specialization": "Backend",
            "experience": "5 years",
            "screenshot_url": "https://example.com/screenshot.png",
            "mvp_url": "https://example.com/app",
        },
    }


def _investor(inv_id, name, designation, focus_sectors, min_ticket, max_ticket):
    return {
        "id": inv_id,
        "name": name,
        "firm": f"{name} Capital",
        "designation": designation,
        "focus_sectors": focus_sectors,
        "min_ticket": min_ticket,
        "max_ticket": max_ticket,
    }


def failing_ask_llm_json(prompt, system_prompt=""):
    raise RuntimeError("simulated Groq outage")


def test_agent4_into_agent5_pipeline_falls_back_cleanly_without_groq(monkeypatch):
    monkeypatch.setattr(agent4, "ask_llm_json", failing_ask_llm_json)
    monkeypatch.setattr(agent5, "ask_llm_json", failing_ask_llm_json)

    ideas_pool = [
        # Late-stage roadmap keywords ("beta") push completion over the MVP-
        # ready threshold (see estimate_mvp_readiness's LATE_STAGE_KEYWORDS).
        _idea("idea_ai", "AI", feasibility=80, roadmap_steps=["Step 1: setup", "Step 2: beta launch"]),
        # No late-stage keywords and default-ish feasibility — stays under
        # the "ready" threshold, must be held back from matching entirely.
        _idea("idea_fintech", "FinTech", feasibility=50, roadmap_steps=["Step 1: early research"]),
    ]
    investors_pool = [
        _investor("inv_ai", "Ada", "Partner", ["AI"], min_ticket=100_000, max_ticket=1_000_000),
        _investor("inv_fintech", "Fin", "Associate", ["FinTech"], min_ticket=100_000, max_ticket=1_000_000),
    ]

    # --- Agent 4: shortlisting ---
    top_ideas, top_investors, agent4_logs = agent4.run_agent4(ideas_pool, investors_pool, limit=10)

    assert {i["id"] for i in top_ideas} == {"idea_ai", "idea_fintech"}
    assert {i["id"] for i in top_investors} == {"inv_ai", "inv_fintech"}
    assert any("fallback" in log.lower() or "failed" in log.lower() for log in agent4_logs)
    # Rule-based fallback score must actually have been assigned, not left blank.
    assert all("final_score" in i for i in top_ideas)

    # --- Agent 5: MVP readiness + matchmaking, fed directly from Agent 4's output ---
    matched_pairs, checked_ideas, agent5_logs = agent5.run_agent5(top_ideas, top_investors)

    checked_by_id = {i["id"]: i for i in checked_ideas}
    assert checked_by_id["idea_ai"]["mvp_ready"] is True
    assert checked_by_id["idea_fintech"]["mvp_ready"] is False

    # Only the ready idea can be matched — the held-back one must not appear
    # in the output at all.
    assert len(matched_pairs) == 1
    pair = matched_pairs[0]
    assert pair["idea"]["id"] == "idea_ai"
    # Sector fit (40 of match_score's 100 points) must win idea_ai the
    # on-sector investor over the off-sector one, even though both were
    # sitting in the shortlisted pool Agent 4 handed off.
    assert pair["investor"]["id"] == "inv_ai"
    assert pair["reason"] == "Rule-based baseline (fallback)"
