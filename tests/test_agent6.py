"""
Regression coverage for agent6.py's rejection-routing logic — pure, no LLM
or network calls, so this is fully deterministic.
"""
import agents.agent6 as agent6


def _idea(idea_id, domain="AI", potential=70, feasibility=80, market_fit=65, final_score=75):
    return {
        "id": idea_id,
        "title": f"Idea {idea_id}",
        "domain": domain,
        "final_score": final_score,
        "base_scores": {"potential": potential, "feasibility": feasibility, "market_fit": market_fit},
        "founder": {},
    }


def _investor(inv_id, name, focus_sectors=("AI",), final_score=75, min_ticket=100_000, max_ticket=1_000_000):
    return {
        "id": inv_id,
        "name": name,
        "firm": f"{name} Capital",
        "final_score": final_score,
        "focus_sectors": list(focus_sectors),
        "min_ticket": min_ticket,
        "max_ticket": max_ticket,
    }


def test_run_agent6_for_founder_routes_to_next_best_eligible_investor():
    idea = _idea("idea_01")
    investors = [
        _investor("inv_low", "Low Fit", focus_sectors=("FinTech",), final_score=50),
        _investor("inv_high", "High Fit", focus_sectors=("AI",), final_score=95),
    ]

    result, log = agent6.run_agent6_for_founder(idea, investors, rejected_investor_ids=set())

    assert result["id"] == "inv_high"
    assert "High Fit" in log


def test_run_agent6_for_founder_excludes_rejected_investors():
    idea = _idea("idea_01")
    investors = [
        _investor("inv_high", "High Fit", focus_sectors=("AI",), final_score=95),
        _investor("inv_second", "Second Choice", focus_sectors=("AI",), final_score=60),
    ]

    result, log = agent6.run_agent6_for_founder(idea, investors, rejected_investor_ids={"inv_high"})

    assert result["id"] == "inv_second"


def test_run_agent6_for_founder_returns_none_when_all_rejected():
    idea = _idea("idea_01")
    investors = [_investor("inv_a", "A"), _investor("inv_b", "B")]

    result, log = agent6.run_agent6_for_founder(idea, investors, rejected_investor_ids={"inv_a", "inv_b"})

    assert result is None
    assert "No more investors" in log


def test_run_agent6_for_investor_routes_to_next_best_eligible_idea():
    investor = _investor("inv_01", "Investor One", focus_sectors=("AI",))
    ideas = [
        _idea("idea_low", domain="FinTech", final_score=40),
        _idea("idea_high", domain="AI", final_score=95),
    ]

    result, log = agent6.run_agent6_for_investor(investor, ideas, rejected_idea_ids=set())

    assert result["id"] == "idea_high"
    assert "Idea idea_high" in log


def test_run_agent6_for_investor_excludes_rejected_ideas():
    investor = _investor("inv_01", "Investor One", focus_sectors=("AI",))
    ideas = [
        _idea("idea_high", domain="AI", final_score=95),
        _idea("idea_second", domain="AI", final_score=60),
    ]

    result, log = agent6.run_agent6_for_investor(investor, ideas, rejected_idea_ids={"idea_high"})

    assert result["id"] == "idea_second"


def test_run_agent6_for_investor_returns_none_when_all_rejected():
    investor = _investor("inv_01", "Investor One")
    ideas = [_idea("idea_a"), _idea("idea_b")]

    result, log = agent6.run_agent6_for_investor(investor, ideas, rejected_idea_ids={"idea_a", "idea_b"})

    assert result is None
    assert "No more startups" in log
