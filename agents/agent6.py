from agents.agent5 import match_score

def run_agent6_for_founder(idea, top_investors, rejected_investor_ids) -> tuple[dict, str]:
    """
    Agent 6 (Joun Ali Bangash): Handles rejections for a founder.
    If the current matched investor rejected the founder, find the next-best investor.
    """
    eligible = [inv for inv in top_investors if inv["id"] not in rejected_investor_ids]
    if not eligible:
        return None, "No more investors left in the shortlisted pool."

    # Sort eligible investors by their match score with this idea
    best_next_investor = max(eligible, key=lambda inv: match_score(idea, inv))
    score = match_score(idea, best_next_investor)
    
    log_msg = f"Agent 6 (Joun Ali Bangash) routed: Match rejected. Recommending next-best investor: {best_next_investor['name']} from {best_next_investor['firm']} (Score: {score})"
    return best_next_investor, log_msg

def run_agent6_for_investor(investor, top_ideas, rejected_idea_ids) -> tuple[dict, str]:
    """
    Agent 6 (Joun Ali Bangash): Handles rejections for an investor.
    If the investor rejects the current startup, recommend the next-best startup.
    """
    eligible = [idea for idea in top_ideas if idea["id"] not in rejected_idea_ids]
    if not eligible:
        return None, "No more startups left in the shortlisted pool."

    # Sort eligible ideas by their match score with this investor
    best_next_idea = max(eligible, key=lambda idea: match_score(idea, investor))
    score = match_score(best_next_idea, investor)
    
    log_msg = f"Agent 6 (Joun Ali Bangash) routed: Idea rejected. Recommending next-best startup: {best_next_idea['title']} (Score: {score})"
    return best_next_idea, log_msg
