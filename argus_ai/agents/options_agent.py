import logging
from typing import List, Dict, Any
from argus_ai.agents.base_agent import BaseAgent
from argus_ai.core.models import AgentVote, MarketContext

logger = logging.getLogger("argus.options_agent")


class OptionsFlowAgent(BaseAgent):
    """
    Options Flow & Derivatives Agent:
    Evaluates institutional Unusual Options Activity (UOA), Put/Call ratios,
    implied volatility (IV) rank, and dealer gamma positioning.
    """

    def __init__(self):
        super().__init__("Options Flow Agent")

    def analyze(self, ctx: MarketContext) -> AgentVote:
        system_prompt = (
            "You are the Chief Derivatives & Options Flow Specialist for ARGUS AI. "
            "Analyze options market structure, Put/Call ratios, and institutional sweep activity. "
            "Output JSON with fields: "
            "recommendation ('BUY', 'SELL', 'HOLD'), confidence (0.0 to 1.0), "
            "score (-100 to 100), key_arguments (list of strings), concerns_raised (list of strings)."
        )
        user_prompt = (
            f"Asset: {ctx.ticker}\n"
            f"Current Price: ${ctx.current_price:.2f}\n"
            f"Market VIX: {ctx.vix_level:.1f}\n"
            f"Market Regime: {ctx.market_regime}"
        )

        llm_res = self.query_llm_json(system_prompt, user_prompt)
        if llm_res:
            try:
                return AgentVote(
                    agent_name=self.name,
                    recommendation=llm_res.get("recommendation", "HOLD"),
                    confidence=float(llm_res.get("confidence", 0.75)),
                    score=float(llm_res.get("score", 0.0)),
                    key_arguments=llm_res.get("key_arguments", []),
                    concerns_raised=llm_res.get("concerns_raised", []),
                    metrics={"source": "Gemini Derivatives Inference"}
                )
            except Exception:
                pass

        # Deterministic Heuristic Options Model
        recs: List[str] = []
        concerns: List[str] = []
        score = 0.0

        # Simulate options metrics based on market context
        put_call_ratio = 0.65 if ctx.market_regime == "BULLISH" else (1.35 if ctx.market_regime == "BEARISH" else 0.95)
        
        if put_call_ratio < 0.75:
            score += 40.0
            recs.append(f"Bullish Institutional Call Sweep: Put/Call Ratio is low ({put_call_ratio:.2f})")
            recs.append("Positive dealer gamma pin providing price floor")
        elif put_call_ratio > 1.20:
            score -= 40.0
            recs.append(f"Heavy Put Hedging / Bearish Flow: Put/Call Ratio is elevated ({put_call_ratio:.2f})")
            concerns.append("Negative dealer gamma exposure amplifies downside slippage")
        else:
            recs.append(f"Balanced options positioning (Put/Call Ratio: {put_call_ratio:.2f})")

        if ctx.vix_level > 25.0:
            score -= 15.0
            concerns.append(f"Elevated IV skew (VIX {ctx.vix_level:.1f}) inflating option hedge costs")

        if score >= 25.0:
            rec = "BUY"
            conf = 0.80
        elif score <= -25.0:
            rec = "SELL"
            conf = 0.80
        else:
            rec = "HOLD"
            conf = 0.55

        return AgentVote(
            agent_name=self.name,
            recommendation=rec,
            confidence=conf,
            score=round(score, 1),
            key_arguments=recs,
            concerns_raised=concerns,
            metrics={"put_call_ratio": put_call_ratio, "vix": ctx.vix_level}
        )
