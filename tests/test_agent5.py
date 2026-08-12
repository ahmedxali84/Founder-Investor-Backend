"""
Regression coverage for agents/agent5.py's matching invariant: every idea id
and every investor id must appear at most once across a single run's
refined_pairs. This was violated by a real bug fixed earlier this session —
see the comment above the fallback-fill loop in agents/agent5.py.
"""
import agents.agent5 as agent5


def _idea(idea_id, title, final_score, feasibility=90, potential=50, domain="SaaS"):
    return {
        "id": idea_id,
        "title": title,
        "domain": domain,
        "final_score": final_score,
        "base_scores": {"feasibility": feasibility, "potential": potential},
        "roadmap": {"steps": ["Step 1: build", "Step 2: launch to early users"]},
        "founder": {"specialization": "Generalist"},
    }


def _investor(inv_id, name, final_score, min_ticket=100_000, max_ticket=400_000, focus_sectors=("SaaS",)):
    return {
        "id": inv_id,
        "name": name,
        "firm": f"{name} Capital",
        "final_score": final_score,
        "min_ticket": min_ticket,
        "max_ticket": max_ticket,
        "focus_sectors": list(focus_sectors),
    }


def _two_ideas_two_investors():
    ideas = [
        _idea("idea_01", "Idea One", final_score=80),
        _idea("idea_02", "Idea Two", final_score=70),
    ]
    investors = [
        _investor("inv_01", "Investor One", final_score=90),
        _investor("inv_02", "Investor Two", final_score=60),
    ]
    return ideas, investors


def _assert_no_duplicate_ids(pairs):
    idea_ids = [p["idea"]["id"] for p in pairs]
    investor_ids = [p["investor"]["id"] for p in pairs]
    assert len(idea_ids) == len(set(idea_ids)), f"duplicate idea id in {idea_ids}"
    assert len(investor_ids) == len(set(investor_ids)), f"duplicate investor id in {investor_ids}"


def test_greedy_match_produces_unique_pairs():
    ideas, investors = _two_ideas_two_investors()
    pairs = agent5.greedy_match(ideas, investors)
    assert len(pairs) == 2
    _assert_no_duplicate_ids(pairs)
    # Higher-scoring idea should get the higher-scoring/better-fit investor first.
    assert pairs[0]["idea"]["id"] == "idea_01"
    assert pairs[0]["investor"]["id"] == "inv_01"


def test_run_agent5_refinement_swap_does_not_duplicate_investor(monkeypatch):
    """
    Reproduces the exact scenario the fix addresses: Groq's refinement
    reassigns inv_01 (rule-based baseline: paired with idea_01) onto idea_02
    instead. The old buggy fallback-fill loop only checked idea ids, so it
    would then also fill idea_01's slot with its original rule-based
    investor (inv_01) — even though inv_01 was already used above — assigning
    inv_01 to two different ideas in the same result.
    """
    ideas, investors = _two_ideas_two_investors()

    def fake_ask_llm_json(prompt, system_prompt=""):
        return {"matches": [{"idea_id": "idea_02", "investor_id": "inv_01", "reason": "better fit"}]}

    monkeypatch.setattr(agent5, "ask_llm_json", fake_ask_llm_json)

    refined_pairs, checked_ideas, logs = agent5.run_agent5(ideas, investors)

    _assert_no_duplicate_ids(refined_pairs)
    # The Groq-directed swap itself must be honored.
    assert any(p["idea"]["id"] == "idea_02" and p["investor"]["id"] == "inv_01" for p in refined_pairs)
    # idea_01's original rule-based investor (inv_01) must NOT reappear paired
    # with idea_01 — that would be the exact bug this test guards against.
    assert not any(p["idea"]["id"] == "idea_01" and p["investor"]["id"] == "inv_01" for p in refined_pairs)


def test_run_agent5_falls_back_cleanly_on_groq_failure(monkeypatch):
    ideas, investors = _two_ideas_two_investors()

    def failing_ask_llm_json(prompt, system_prompt=""):
        raise RuntimeError("simulated Groq outage")

    monkeypatch.setattr(agent5, "ask_llm_json", failing_ask_llm_json)

    refined_pairs, checked_ideas, logs = agent5.run_agent5(ideas, investors)

    assert len(refined_pairs) == 2
    _assert_no_duplicate_ids(refined_pairs)
    assert all(p["reason"] == "Rule-based baseline (fallback)" for p in refined_pairs)
