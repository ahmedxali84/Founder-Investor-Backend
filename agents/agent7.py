from agents.llm import ask_llm, GROQ_API_KEY_AGENT7

AGREEMENT_DRAFT_PROMPT = """You are Agent 7, a legal drafting expert for early-stage startup investments.
You will write a formal, binding early-stage Investment Agreement / Term Sheet between a Founder and an Investor.
You must compile this based on:
1. The Founder's Profile and Startup Idea (MVP and Roadmap details).
2. The Investor's Profile (Firm name, Designation).
3. The agreed deal terms below — use these exact figures verbatim; they were entered directly by
   the parties, so do not recompute, round, or override them with anything implied by the chat log.
4. The Chat Logs of their discussion (use only for any other special conditions or terms they talked
   about — vesting schedule, board seats, exclusivity, etc. — never for the funding amount or equity share).

Founder details:
{founder_json}

Startup Idea & MVP details:
{idea_json}

Investor details:
{investor_json}

Agreed deal terms (authoritative — do not change these numbers):
- Equity share: {equity_percent}%
- Investment amount: ${amount_usd:,.2f} USD

Conversation History (Chat Log):
{chat_history}

Draft a detailed, professional, and comprehensive Startup-Investor Investment Agreement. Format it beautifully in Markdown, with clear section headings, numbered sections, bullet points, and signature blocks.
"""

def run_agent7(founder_data: dict, idea_data: dict, investor_data: dict, chat_log: list, deal_terms: dict) -> str:
    """
    Agent 7: drafts the final investment agreement from the founder/investor
    profiles, the user-supplied deal terms (equity_percent, amount_usd), and
    the conversation log for any remaining special conditions.
    """
    import json
    chat_history_str = ""
    for msg in chat_log:
        chat_history_str += f"{msg.get('sender', 'Unknown')}: {msg.get('text', '')}\n"

    prompt = AGREEMENT_DRAFT_PROMPT.format(
        founder_json=json.dumps(founder_data, indent=2),
        idea_json=json.dumps(idea_data, indent=2),
        investor_json=json.dumps(investor_data, indent=2),
        equity_percent=deal_terms.get("equity_percent"),
        amount_usd=deal_terms.get("amount_usd"),
        chat_history=chat_history_str
    )

    agreement_draft = ask_llm(
        prompt,
        system_prompt="You are Agent 7, an expert startup attorney who drafts professional legal agreements based on deal details and chat logs.",
        api_key=GROQ_API_KEY_AGENT7,
    )
    return agreement_draft
