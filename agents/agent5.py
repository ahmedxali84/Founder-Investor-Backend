import json
from agents.llm import ask_llm_json

LATE_STAGE_KEYWORDS = ["pilot", "testing", "launch", "deploy", "beta"]

def estimate_mvp_readiness(idea):
    """Estimate MVP completion % and readiness status."""
    base = idea.get("base_scores", {})
    feasibility = base.get("feasibility", 50)
    
    # Check steps
    roadmap = idea.get("roadmap", {})
    steps = roadmap.get("steps", []) if isinstance(roadmap, dict) else roadmap
    if not steps:
        steps = []
        
    late_stage_hits = sum(
        1 for phase in steps if isinstance(phase, str) and any(k in phase.lower() for k in LATE_STAGE_KEYWORDS)
    )
    completion = min(100, feasibility + (late_stage_hits * 4))

    if completion >= 80:
        status, ready = "MVP Ready", True
    elif completion >= 62:
        status, ready = "Near-Ready (final testing needed)", True
    else:
        status, ready = "Not Ready (early build stage)", False

    return {"mvp_completion": completion, "mvp_status": status, "mvp_ready": ready}

def estimate_ticket_need(idea):
    base = idea.get("base_scores", {})
    potential = base.get("potential", 50)
    min_needed = 100_000 + potential * 3_000
    max_needed = min_needed * 3
    return min_needed, max_needed

def ticket_overlap_score(idea, investor):
    idea_min, idea_max = estimate_ticket_need(idea)
    inv_min = investor.get("min_ticket", 0)
    inv_max = investor.get("max_ticket", 0)
    overlap = min(idea_max, inv_max) - max(idea_min, inv_min)
    return 30 if overlap > 0 else max(0, 15 + (overlap / 50_000))

def match_score(idea, investor):
    # Sector fit (40 pts)
    idea_domain = idea.get("domain", "")
    focus_sectors = investor.get("focus_sectors", [])
    sector = 40 if idea_domain in focus_sectors else 0
    
    # Ticket size fit (30 pts)
    ticket = ticket_overlap_score(idea, investor)
    
    # Seniority score (20 pts)
    seniority = (investor.get("final_score", 50) / 100) * 20
    
    # Idea quality score (10 pts)
    idea_quality = (idea.get("final_score", 50) / 100) * 10

    raw_score = sector + ticket + seniority + idea_quality

    # MVP proof penalty: missing screenshot or MVP URL reduces investor match confidence
    founder_info = idea.get("founder", {}) if isinstance(idea.get("founder"), dict) else {}
    has_screenshot = bool(founder_info.get("screenshot_url"))
    has_mvp_url = bool(founder_info.get("mvp_url"))

    penalty_multiplier = 1.0
    if not has_screenshot:
        penalty_multiplier -= 0.15  # -15% penalty for missing screenshot
    if not has_mvp_url:
        penalty_multiplier -= 0.10  # -10% penalty for missing MVP URL

    final_match_score = raw_score * max(0.60, penalty_multiplier)
    return round(final_match_score, 1)

def greedy_match(ready_ideas, investors):
    """Match each ready idea with the single best available investor."""
    ranked_ideas = sorted(
        ready_ideas, key=lambda x: x.get("final_score", 0), reverse=True
    )
    available = investors.copy()
    pairs = []
    for idea in ranked_ideas:
        if not available:
            break
        # Find best investor for this idea
        best = max(available, key=lambda inv: match_score(idea, inv))
        pairs.append({
            "idea": idea,
            "investor": best,
            "score": match_score(idea, best)
        })
        available.remove(best)
    return pairs

def run_agent5(top_ideas, top_investors) -> tuple[list, list, list]:
    """
    Agent 5 (Ashar Bhai): Checks MVP readiness, creates matches,
    and calls Groq to refine/swap pairings for optimal alignment.
    """
    logs = []
    logs.append("Executing [Agent 5 - Ashar Bhai]: Conducting MVP cross-checks...")
    
    # 1. MVP readiness cross-check
    checked_ideas = []
    for idea in top_ideas:
        checked = idea.copy()
        checked.update(estimate_mvp_readiness(idea))
        checked_ideas.append(checked)
        
    ready_ideas = [i for i in checked_ideas if i["mvp_ready"]]
    on_hold_ideas = [i for i in checked_ideas if not i["mvp_ready"]]
    
    logs.append(f"MVP check done: {len(ready_ideas)} ready/near-ready, {len(on_hold_ideas)} held back.")
    
    if not ready_ideas:
        logs.append("No ideas are ready for matchmaking.")
        return [], checked_ideas, logs

    # 2. Rule-based matchmaking baseline
    logs.append("Performing rule-based greedy matching...")
    rule_based_pairs = greedy_match(ready_ideas, top_investors)
    
    # 3. Groq refinement
    system_prompt = (
        "You are Agent 5 (Ashar Bhai) in a startup-to-investor matching pipeline. "
        "You review suggested pairs of founders and investors and perform swaps if it improves overall fit. "
        "Every idea/investor id must remain used exactly once. Respond with STRICT JSON ONLY matching: "
        '{"matches": [{"idea_id": "idea_01", "investor_id": "inv_01", "reason": "one short sentence"}]}'
    )

    pairs_payload = [
        {
            "idea_id": p["idea"]["id"],
            "idea_title": p["idea"]["title"],
            "idea_domain": p["idea"]["domain"],
            "mvp_status": p["idea"]["mvp_status"],
            "founder_specialization": p["idea"].get("founder", {}).get("specialization"),
            "investor_id": p["investor"]["id"],
            "investor_name": p["investor"]["name"],
            "investor_firm": p["investor"].get("firm"),
            "investor_focus_sectors": p["investor"].get("focus_sectors"),
            "rule_based_match_score": p["score"],
        }
        for p in rule_based_pairs
    ]

    refined_pairs = []
    try:
        logs.append("Sending baseline pairings to Groq for expert refinement and swaps...")
        prompt = json.dumps({"pairs": pairs_payload})
        res = ask_llm_json(prompt, system_prompt=system_prompt)
        
        ideas_by_id = {p["idea"]["id"]: p["idea"] for p in rule_based_pairs}
        investors_by_id = {p["investor"]["id"]: p["investor"] for p in rule_based_pairs}
        
        used_ideas = set()
        used_investors = set()
        
        for item in res.get("matches", []):
            idea_id = item.get("idea_id")
            inv_id = item.get("investor_id")
            idea = ideas_by_id.get(idea_id)
            investor = investors_by_id.get(inv_id)
            
            if idea and investor and idea_id not in used_ideas and inv_id not in used_investors:
                refined_pairs.append({
                    "idea": idea,
                    "investor": investor,
                    "score": match_score(idea, investor),
                    "reason": item.get("reason", "Refined by Ashar Bhai")
                })
                used_ideas.add(idea_id)
                used_investors.add(inv_id)
                
        # Fill in any missing pairs from the rule-based matcher — must check
        # both ids, not just the idea's: Groq's refinement can swap an
        # investor onto a different idea than their rule-based baseline
        # pairing, so checking only used_ideas let that same investor get
        # appended a second time here under their original idea, assigning
        # one investor to two matches at once.
        for p in rule_based_pairs:
            if p["idea"]["id"] not in used_ideas and p["investor"]["id"] not in used_investors:
                filler = p.copy()
                filler["reason"] = "Rule-based baseline"
                refined_pairs.append(filler)
                used_ideas.add(p["idea"]["id"])
                used_investors.add(p["investor"]["id"])
                
        logs.append(f"Refinement complete. Generated {len(refined_pairs)} matched pairs.")
    except Exception as exc:
        logs.append(f"Groq refinement failed ({exc}). Falling back to rule-based baseline.")
        refined_pairs = rule_based_pairs
        for p in refined_pairs:
            p["reason"] = "Rule-based baseline (fallback)"
            
    return refined_pairs, checked_ideas, logs
