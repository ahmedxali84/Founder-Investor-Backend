"""
Regression coverage for agent7.py's agreement drafting — prompt assembly and
verbatim deal-terms handling. ask_llm itself is mocked; this tests that
agent7 builds the right prompt and passes it through correctly, not that an
LLM produces a good agreement.
"""
import agents.agent7 as agent7


def test_run_agent7_embeds_deal_terms_verbatim_not_recomputed(monkeypatch):
    captured = {}

    def fake_ask_llm(prompt, system_prompt="", api_key=""):
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt
        captured["api_key"] = api_key
        return "# Investment Agreement\n..."

    monkeypatch.setattr(agent7, "ask_llm", fake_ask_llm)

    result = agent7.run_agent7(
        founder_data={"name": "Ahmed Ali"},
        idea_data={"title": "Techflix"},
        investor_data={"name": "Mubashir Shabir", "firm": "Wajedo"},
        chat_log=[{"sender": "Ahmed", "text": "Let's do 10% for $200k."}],
        deal_terms={"equity_percent": 10, "amount_usd": 200000},
    )

    assert result == "# Investment Agreement\n..."
    assert "Equity share: 10%" in captured["prompt"]
    assert "Investment amount: $200,000.00 USD" in captured["prompt"]
    assert captured["system_prompt"].startswith("You are Agent 7")
    assert captured["api_key"] == agent7.GROQ_API_KEY_AGENT7


def test_run_agent7_formats_chat_history_and_defaults_missing_fields(monkeypatch):
    captured = {}

    def fake_ask_llm(prompt, system_prompt="", api_key=""):
        captured["prompt"] = prompt
        return "draft"

    monkeypatch.setattr(agent7, "ask_llm", fake_ask_llm)

    agent7.run_agent7(
        founder_data={}, idea_data={}, investor_data={},
        chat_log=[
            {"sender": "Ahmed", "text": "Hello"},
            {"text": "Missing a sender"},  # no 'sender' key -> defaults to 'Unknown'
            {"sender": "Mubashir"},         # no 'text' key -> defaults to ''
        ],
        deal_terms={"equity_percent": 5, "amount_usd": 50000},
    )

    assert "Ahmed: Hello" in captured["prompt"]
    assert "Unknown: Missing a sender" in captured["prompt"]
    assert "Mubashir: \n" in captured["prompt"]


def test_run_agent7_embeds_founder_idea_investor_as_json(monkeypatch):
    captured = {}

    def fake_ask_llm(prompt, system_prompt="", api_key=""):
        captured["prompt"] = prompt
        return "draft"

    monkeypatch.setattr(agent7, "ask_llm", fake_ask_llm)

    agent7.run_agent7(
        founder_data={"name": "Ahmed Ali", "specialization": "AI"},
        idea_data={"title": "Techflix", "domain": "SaaS"},
        investor_data={"name": "Mubashir Shabir"},
        chat_log=[],
        deal_terms={"equity_percent": 8, "amount_usd": 150000},
    )

    assert '"name": "Ahmed Ali"' in captured["prompt"]
    assert '"title": "Techflix"' in captured["prompt"]
    assert '"name": "Mubashir Shabir"' in captured["prompt"]
