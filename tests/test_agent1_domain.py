"""
Regression coverage for agent1.py's idea-domain classification — a real bug
fixed this session. run_agent1's returned idea used to have no `domain` field
at all; main_app.py's _run_idea_activation_pipeline filled that gap by
reusing the founder's own `specialization` text as the idea's domain. Since
match_score() (agent5.py) does an exact-match check of idea domain against
an investor's onboarding-entered focus_sectors list, a founder-skills string
like "Fullstack, AI, Data Analysis" could never equal a clean category like
"AI" — silently zeroing out 40 of match_score's 100 points for every idea on
the platform. Agent 1 must now classify a real domain itself.
"""
import agents.agent1 as agent1


def test_run_agent1_returns_a_domain_distinct_from_raw_llm_extras(monkeypatch):
    calls = []

    def fake_ask_llm_json(prompt, system_prompt=""):
        calls.append(prompt)
        if "checking whether a document" in prompt:
            return {"is_idea": True, "reason": "Describes a real startup idea."}
        if "identify four things" in prompt:
            return {
                "problem": "Small teams can't track security threats across tools.",
                "solution": "A unified dashboard correlating alerts from existing tools.",
                "target_market": "Small security teams at startups.",
                "domain": "Cybersecurity",
            }
        if "smallest possible working version" in prompt:
            return {"must_have": ["Alert ingestion", "Unified dashboard"], "nice_to_have": ["Slack integration"]}
        return {"steps": ["Step 1: set up ingestion", "Step 2: build dashboard", "Step 3: beta launch"]}

    monkeypatch.setattr(agent1, "ask_llm_json", fake_ask_llm_json)

    result = agent1.run_agent1("A real startup idea document.", is_raw_text=True)

    assert result["is_idea"] is True
    assert result["idea"]["domain"] == "Cybersecurity"
    # The domain must come from Agent 1's own classification, not be silently
    # identical to something else read from a founder's profile elsewhere —
    # this test only has Agent 1's own mocked output in scope, so a passing
    # assertion here proves the value traces back to idea_data["domain"].


def test_run_agent1_falls_back_to_saas_when_llm_omits_domain(monkeypatch):
    def fake_ask_llm_json(prompt, system_prompt=""):
        if "checking whether a document" in prompt:
            return {"is_idea": True, "reason": "ok"}
        if "identify four things" in prompt:
            # Simulates an LLM response that forgot the domain field entirely.
            return {"problem": "P", "solution": "S", "target_market": "T"}
        if "smallest possible working version" in prompt:
            return {"must_have": [], "nice_to_have": []}
        return {"steps": []}

    monkeypatch.setattr(agent1, "ask_llm_json", fake_ask_llm_json)

    result = agent1.run_agent1("Some idea text.", is_raw_text=True)

    assert result["idea"]["domain"] == "SaaS"
