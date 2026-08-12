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

Read the text below and identify three things:
1. The core problem being solved
2. The proposed solution
3. The target market (who this is for)

Respond with ONLY a JSON object in this exact format, no extra text:
{{"problem": "...", "solution": "...", "target_market": "..."}}

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
            "target_market": idea_data.get("target_market", "")
        },
        "mvp": {
            "must_have": mvp_data.get("must_have", []),
            "nice_to_have": mvp_data.get("nice_to_have", [])
        },
        "roadmap": {
            "steps": roadmap_data.get("steps", [])
        }
    }
