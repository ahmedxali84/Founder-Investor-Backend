import json
from agents.llm import ask_llm_json

DESIGNATION_SCORES = {
    "Managing Director": 100,
    "Partner": 95,
    "General Partner": 95,
    "Partner / Director": 95,
    "Vice President": 80,
    "Principal": 75,
    "Angel Investor": 70,
    "Associate": 45,
    "Analyst": 25,
}

def score_ideas_rule_based(ideas):
    scored = []
    for idea in ideas:
        base = idea.get("base_scores", {"potential": 50, "feasibility": 50, "market_fit": 50})
        avg_score = round(
            (base.get("potential", 50) + base.get("feasibility", 50) + base.get("market_fit", 50)) / 3,
            2
        )
        item = idea.copy()
        item["rule_based_score"] = avg_score
        scored.append(item)
    scored.sort(key=lambda x: x["rule_based_score"], reverse=True)
    return scored

def score_investors_rule_based(investors):
    ranked = []
    for inv in investors:
        score = DESIGNATION_SCORES.get(inv.get("designation", "Analyst"), 50)
        item = inv.copy()
        item["rule_based_score"] = score
        ranked.append(item)
    ranked.sort(key=lambda x: (x["rule_based_score"], x["name"]), reverse=True)
    return ranked

def run_agent4(ideas_pool, investors_pool, limit=10) -> tuple[list, list, list]:
    """
    Agent 4 (Saad): Shortlists the Top N ideas and Top N investors.
    Uses Groq to judge and rank based on problem/solution fit and investor credentials,
    falling back to rule-based scores on failure.
    """
    logs = []
    logs.append("Executing [Agent 4 - Saad]: Preparing data and pre-scoring pools...")
    
    scored_ideas = score_ideas_rule_based(ideas_pool)
    ranked_investors = score_investors_rule_based(investors_pool)
    
    logs.append(f"Pre-scoring complete: {len(scored_ideas)} ideas and {len(ranked_investors)} investors loaded.")
    
    ideas_payload = [
        {
            "id": i["id"],
            "title": i["title"],
            "domain": i["domain"],
            "problem": (i.get("problem") or "")[:200],
            "solution": (i.get("solution") or "")[:200],
            "founder_specialization": i.get("founder", {}).get("specialization"),
            "founder_experience": i.get("founder", {}).get("experience"),
            "rule_based_score": i.get("rule_based_score"),
        }
        for i in scored_ideas
    ]

    investors_payload = [
        {
            "id": inv["id"],
            "name": inv["name"],
            "firm": inv.get("firm"),
            "designation": inv.get("designation"),
            "focus_sectors": inv.get("focus_sectors"),
            "min_ticket": inv.get("min_ticket"),
            "max_ticket": inv.get("max_ticket"),
            "rule_based_score": inv.get("rule_based_score"),
        }
        for inv in ranked_investors
    ]

    system_prompt = (
        "You are Agent 4 (Saad) in a startup-to-investor matching pipeline. "
        "Select the strongest ideas and the strongest investors. Respond with STRICT JSON ONLY "
        "matching exactly this schema:\n"
        '{"top_ideas": [{"id": "idea_01", "score": 91.5, "reason": "one short sentence"}], '
        '"top_investors": [{"id": "inv_01", "score": 97.0, "reason": "one short sentence"}]}'
    )

    prompt = json.dumps({
        "limit": limit,
        "ideas_pool": ideas_payload,
        "investors_pool": investors_payload
    })

    top_ideas = []
    top_investors = []
    
    try:
        logs.append("Querying Groq to independently evaluate and shortlist top candidates...")
        res = ask_llm_json(prompt, system_prompt=system_prompt)
        logs.append("Groq shortlist retrieved successfully. Validating IDs...")
        
        ideas_by_id = {i["id"]: i for i in scored_ideas}
        investors_by_id = {inv["id"]: inv for inv in ranked_investors}
        
        for item in res.get("top_ideas", []):
            idea_id = item.get("id")
            if idea_id in ideas_by_id:
                merged = ideas_by_id[idea_id].copy()
                merged["ai_overall_score"] = item.get("score", merged["rule_based_score"])
                merged["ai_reason"] = item.get("reason", "")
                merged["final_score"] = merged["ai_overall_score"]
                top_ideas.append(merged)
                
        for item in res.get("top_investors", []):
            inv_id = item.get("id")
            if inv_id in investors_by_id:
                merged = investors_by_id[inv_id].copy()
                merged["ai_overall_score"] = item.get("score", merged["rule_based_score"])
                merged["ai_reason"] = item.get("reason", "")
                merged["final_score"] = merged["ai_overall_score"]
                top_investors.append(merged)
                
        logs.append(f"Successfully validated {len(top_ideas)} ideas and {len(top_investors)} investors.")
        
    except Exception as exc:
        logs.append(f"Groq evaluation failed ({exc}). Falling back to rule-based shortlist.")
        top_ideas = []
        top_investors = []
        
    # Fallback to pre-scored top elements if something was missing or errored
    if not top_ideas:
        top_ideas = scored_ideas[:limit]
        for idea in top_ideas:
            idea["final_score"] = idea["rule_based_score"]
            idea["ai_overall_score"] = idea["rule_based_score"]
            idea["ai_reason"] = "Rule-based fallback score"
            
    if not top_investors:
        top_investors = ranked_investors[:limit]
        for inv in top_investors:
            inv["final_score"] = inv["rule_based_score"]
            inv["ai_overall_score"] = inv["rule_based_score"]
            inv["ai_reason"] = "Rule-based fallback score"

    # Ensure they are sorted by score
    top_ideas.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    top_investors.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    
    return top_ideas[:limit], top_investors[:limit], logs
