from typing import List
from argus_ai.agents.base_agent import BaseAgent
from argus_ai.core.models import AgentVote, MarketContext


class MacroAgent(BaseAgent):
    """
    Macro & Sector Agent: Evaluates overarching equity market breadth (SPY/QQQ),
    VIX implied volatility, interest rate environment, and sector rotation.
    """

    def __init__(self):
        super().__init__("Macro & Sector Agent")

    def analyze(self, ctx: MarketContext) -> AgentVote:
        vix = ctx.vix_level
        regime = ctx.market_regime

        system_prompt = (
            "You are the Chief Macroeconomic & Market Regime Strategist for ARGUS AI. "
            "Analyze macroeconomic context (VIX, market regime, sector momentum) for the proposed trade. "
            "Output JSON with fields: recommendation ('BUY', 'SELL', 'HOLD'), confidence (0.0 to 1.0), "
            "score (-100 to 100), key_arguments (list of strings), concerns_raised (list of strings)."
        )
        user_prompt = (
            f"Asset: {ctx.ticker}\n"
            f"Current VIX: {vix:.2f}\n"
            f"Broad Market Regime: {regime}\n"
        )

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
                    metrics={"vix": vix, "regime": regime}
                )
            except Exception:
                pass

        # Deterministic Macro Rule Engine
        score = 0.0
        recs: List[str] = []
        concerns: List[str] = []

        if vix < 15.0:
            score += 35.0
            recs.append(f"Low VIX environment ({vix:.1f}) provides ideal conditions for momentum & trend following")
        elif 15.0 <= vix < 22.0:
            score += 15.0
            recs.append(f"Normal market volatility regime (VIX = {vix:.1f})")
        elif 22.0 <= vix < 30.0:
            score -= 30.0
            concerns.append(f"Elevated VIX ({vix:.1f}) indicates heightened market uncertainty & tail risk")
        else:
            score -= 60.0
            concerns.append(f"Extreme VIX spike ({vix:.1f}) - risk-off defensive posture recommended")

        if regime.upper() == "BULLISH":
            score += 30.0
            recs.append("Broad market indices (SPY/QQQ) trading above key moving averages with positive breadth")
        elif regime.upper() == "BEARISH":
            score -= 30.0
            concerns.append("Broad market indices under distribution pressure")
        elif regime.upper() == "CHOPPY":
            score -= 10.0
            concerns.append("Index breadth showing range-bound rotational chop")

        if score >= 20.0:
            rec = "BUY"
            conf = 0.80
        elif score <= -20.0:
            rec = "SELL"
            conf = 0.80
        else:
            rec = "HOLD"
            conf = 0.50

        return AgentVote(
            agent_name=self.name,
            recommendation=rec,
            confidence=conf,
            score=max(-100.0, min(100.0, score)),
            key_arguments=recs or ["Macro environment stable"],
            concerns_raised=concerns,
            metrics={"vix": round(vix, 2), "market_regime": regime}
        )
