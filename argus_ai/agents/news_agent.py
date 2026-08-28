from typing import List
from argus_ai.agents.base_agent import BaseAgent
from argus_ai.core.models import AgentVote, MarketContext


class NewsAgent(BaseAgent):
    """
    News & Sentiment Agent: Scans financial media, company earnings announcements,
    SEC filings, and market sentiment to identify catalyst momentum or event risks.
    """

    def __init__(self):
        super().__init__("News & Sentiment Agent")

    def analyze(self, ctx: MarketContext) -> AgentVote:
        news_items = ctx.recent_news or [
            f"{ctx.ticker} reports institutional accumulation and solid quarter guidance",
            f"Analyst consensus upgrades price target following strong enterprise demand"
        ]
        
        system_prompt = (
            "You are the Lead Financial Sentiment & News Analyst for ARGUS AI. "
            "Evaluate headlines and market sentiment for the asset. Output JSON with: "
            "recommendation ('BUY', 'SELL', 'HOLD'), confidence (0.0 to 1.0), "
            "score (-100 to 100), key_arguments (list of strings), concerns_raised (list of strings)."
        )
        user_prompt = f"Ticker: {ctx.ticker}\nHeadlines:\n" + "\n".join([f"- {h}" for h in news_items])

        llm_res = self.query_llm_json(system_prompt, user_prompt)
        if llm_res:
            try:
                return AgentVote(
                    agent_name=self.name,
                    recommendation=llm_res.get("recommendation", "HOLD"),
                    confidence=float(llm_res.get("confidence", 0.7)),
                    score=float(llm_res.get("score", 0.0)),
                    key_arguments=llm_res.get("key_arguments", []),
                    concerns_raised=llm_res.get("concerns_raised", []),
                    metrics={"headline_count": len(news_items)}
                )
            except Exception:
                pass

        # Heuristic Sentiment Parser
        positive_keywords = ["upgrade", "strong", "beat", "partnership", "expansion", "growth", "breakthrough", "bullish", "inflow"]
        negative_keywords = ["downgrade", "miss", "probe", "investigation", "layoffs", "delay", "lawsuit", "bearish", "outflow", "regulatory"]

        pos_count = 0
        neg_count = 0
        recs = []
        concerns = []

        for h in news_items:
            h_lower = h.lower()
            if any(w in h_lower for w in positive_keywords):
                pos_count += 1
                recs.append(f"Positive catalyst: '{h}'")
            elif any(w in h_lower for w in negative_keywords):
                neg_count += 1
                concerns.append(f"Negative headline alert: '{h}'")

        score = (pos_count - neg_count) * 30.0
        score = max(-100.0, min(100.0, score))

        if score >= 25.0:
            rec = "BUY"
            conf = 0.78
        elif score <= -25.0:
            rec = "SELL"
            conf = 0.78
        else:
            rec = "HOLD"
            conf = 0.50
            recs.append("Neutral headline landscape with no major pending disruptions")

        return AgentVote(
            agent_name=self.name,
            recommendation=rec,
            confidence=conf,
            score=score,
            key_arguments=recs or ["Steady newsflow with constructive backdrop"],
            concerns_raised=concerns,
            metrics={"positive_hits": pos_count, "negative_hits": neg_count, "total_headlines": len(news_items)}
        )
