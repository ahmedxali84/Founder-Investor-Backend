import pdfplumber
from agents.llm import ask_llm_json

IDEA_CHECK_PROMPT = """You are checking whether a document actually describes a startup or \
business idea (not a resume, contract, research paper, or unrelated document).

A real startup idea document describes: a problem being solved, a proposed product or service, \
and a target market or customer.

Respond with ONLY a JSON object in this exact format, no extra text:
{{"is_idea": true, "reason": "one short sentence explaining why"}}
or
{{"is_idea": false, "reason": "one short sentence explaining why"}}

Document text:
{pdf_text}
"""

IDEA_EXTRACTION_PROMPT = """You are analyzing a startup idea document.

Read the text below and identify four things:
1. The core problem being solved
2. The proposed solution
3. The target market (who this is for)
4. The single business domain/category this startup belongs to — a short, standard category
   an investor would recognize and filter by (e.g. FinTech, HealthTech, EdTech, AI, SaaS,
   E-commerce, Climate Tech, Cybersecurity, Logistics). This describes the STARTUP's market
   category, not the founder's technical skills or the tech stack used to build it.

Respond with ONLY a JSON object in this exact format, no extra text:
{{"problem": "...", "solution": "...", "target_market": "...", "domain": "..."}}

Idea document text:
{pdf_text}
"""

MVP_BUILDER_PROMPT = """You are a product strategist. Based on this startup idea, break it \
down into the smallest possible working version (MVP) - no extra features.

Problem: {problem}
Solution: {solution}
Target market: {target_market}

Split the features into:
- must_have: the smallest set of features needed for the MVP to actually work
- nice_to_have: everything else that can wait

Respond with ONLY a JSON object in this exact format, no extra text:
{{"must_have": ["...", "..."], "nice_to_have": ["...", "..."]}}
"""

ROADMAP_PROMPT = """You are a technical project planner. Based on this MVP scope, create a \
clear, ordered list of steps needed to build it - from setup to launch.

Must-have features: {must_have}

Respond with ONLY a JSON object in this exact format, no extra text:
{{"steps": ["Step 1: ...", "Step 2: ...", "..."]}}
"""

DOMAIN_CLASSIFICATION_PROMPT = """You are classifying an existing startup idea into a business domain.

Problem: {problem}
Solution: {solution}
Target market: {target_market}

Identify the single business domain/category this startup belongs to — a short, standard
category an investor would recognize and filter by (e.g. FinTech, HealthTech, EdTech, AI,
SaaS, E-commerce, Climate Tech, Cybersecurity, Logistics). This describes the STARTUP's
market category, not any technical skills or tech stack.

Respond with ONLY a JSON object in this exact format, no extra text:
{{"domain": "..."}}
"""


def classify_idea_domain(problem: str, solution: str, target_market: str) -> str:
    """
    Classifies an idea whose problem/solution/target_market are already known
    into a single business domain, without re-running the rest of Agent 1's
    pipeline. Used by scripts/backfill_idea_domains.py to correct ideas
    saved before domain was classified at extraction time (see
    IDEA_EXTRACTION_PROMPT) — those had domain copied from the founder's own
    specialization instead, a real bug fixed this session.
    """
    res = ask_llm_json(
        DOMAIN_CLASSIFICATION_PROMPT.format(problem=problem, solution=solution, target_market=target_market),
        system_prompt="You are a startup analyst that classifies businesses into standard investor-facing categories in valid JSON.",
    )
    return (res.get("domain") or "").strip() or "SaaS"


def extract_pdf_text(pdf_path: str) -> str:
    """Extract all text from a PDF file using pdfplumber."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        text = "\n".join(pages).strip()
        if not text:
            raise ValueError("No readable text found in the PDF.")
        return text
    except Exception as e:
        raise ValueError(f"Failed to read PDF: {str(e)}")

def run_agent1(pdf_path_or_text: str, is_raw_text: bool = False) -> dict:
    """
    Run Agent 1 pipeline:
    1. Extract text from PDF (if not raw text)
    2. Check if the text describes a startup idea
    3. Extract problem, solution, target market
    4. Build MVP scope (must-have vs nice-to-have)
    5. Generate step-by-step build roadmap
    """
    if is_raw_text:
        text = pdf_path_or_text.strip()
    else:
        text = extract_pdf_text(pdf_path_or_text)

    # 1. Check if it is a startup idea
    check_res = ask_llm_json(
        IDEA_CHECK_PROMPT.format(pdf_text=text[:4000]),
        system_prompt="You are a startup validator that only replies in valid JSON."
    )
    
    if not check_res.get("is_idea", False):
        return {
            "is_idea": False,
            "rejection_reason": check_res.get("reason", "The document does not describe a startup idea.")
        }

    # 2. Extract idea elements
    idea_data = ask_llm_json(
        IDEA_EXTRACTION_PROMPT.format(pdf_text=text[:4000]),
        system_prompt="You are an analyst that extracts details in valid JSON."
    )
    
    # 3. Build MVP Scope
    mvp_data = ask_llm_json(
        MVP_BUILDER_PROMPT.format(
            problem=idea_data.get("problem", ""),
            solution=idea_data.get("solution", ""),
            target_market=idea_data.get("target_market", "")
        ),
        system_prompt="You are a product strategist that scopes MVPs in valid JSON."
    )
    
    # 4. Generate Roadmap
    must_have_str = ", ".join(mvp_data.get("must_have", []))
    roadmap_data = ask_llm_json(
        ROADMAP_PROMPT.format(must_have=must_have_str),
        system_prompt="You are a technical planner that designs roadmaps in valid JSON."
    )

    return {
        "is_idea": True,
        "idea": {
            "problem": idea_data.get("problem", ""),
            "solution": idea_data.get("solution", ""),
            "target_market": idea_data.get("target_market", ""),
            # Falls back to "SaaS" (matching the generic default this already
            # had before domain was sourced correctly) if the LLM omits it or
            # returns an empty string, rather than leaving ideas with no
            # domain at all — match_score's sector-fit check just scores 0 in
            # that case rather than crashing, same as any other real category
            # that doesn't happen to be in an investor's focus_sectors.
            "domain": (idea_data.get("domain") or "").strip() or "SaaS",
        },
        "mvp": {
            "must_have": mvp_data.get("must_have", []),
            "nice_to_have": mvp_data.get("nice_to_have", [])
        },
        "roadmap": {
            "steps": roadmap_data.get("steps", [])
        }
    }
