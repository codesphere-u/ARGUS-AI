import logging
from typing import Literal

from argus_ai.agents.base_agent import BaseAgent
from argus_ai.core.models import AgentVote, DevilsAdvocateReview, MarketContext

logger = logging.getLogger("argus.devils_advocate")


class DevilsAdvocateAgent(BaseAgent):
    """
    Devil's Advocate Agent (ARGUS Core Innovation):
    An adversarial AI agent tasked with ruthlessly challenging every trade hypothesis.
    It identifies bear/bull traps, crowded trades, liquidity grabs, and failure points.
    """

    def __init__(self, use_llm: bool = True) -> None:
        super().__init__("Devil's Advocate", use_llm=use_llm)

    def stress_test(
        self,
        ctx: MarketContext,
        proposed_direction: str,
        votes: list[AgentVote],
    ) -> DevilsAdvocateReview:
        # Collate affirmative arguments from other agents
        support_points = []
        concerns_pool = []
        for v in votes:
            support_points.extend(v.key_arguments)
            concerns_pool.extend(v.concerns_raised)

        system_prompt = (
            "You are the Chief Adversarial Risk Auditor and Devil's Advocate for ARGUS AI. "
            "Your explicit job is to CHALLENGE and TEAR APART the proposed trade thesis. "
            "Find hidden bear/bull traps, overextended indicators, upcoming macro risks, and false breakouts. "
            "Output JSON with fields: "
            "adversarial_thesis (string), identified_traps (list of strings), "
            "disqualification_risk_pct (number 0 to 100), veto_recommended (boolean), "
            "recommended_contingency (string), severity_level ('LOW', 'MODERATE', 'SEVERE', 'CRITICAL')."
        )
        user_prompt = (
            f"Asset: {ctx.ticker}\n"
            f"Current Price: ${ctx.current_price:.2f}\n"
            f"Proposed Direction: {proposed_direction}\n"
            f"RSI: {ctx.rsi_14:.1f}, VIX: {ctx.vix_level:.1f}\n"
            "Committee Theses in Favor:\n" + "\n".join([f"- {p}" for p in support_points]) + "\n"
            "Preliminary Concerns:\n" + "\n".join([f"- {c}" for c in concerns_pool])
        )

        llm_res = self.query_llm_json(system_prompt, user_prompt)
        if llm_res:
            try:
                return DevilsAdvocateReview(
                    adversarial_thesis=llm_res.get("adversarial_thesis", "Adversarial stress-test executed."),
                    identified_traps=llm_res.get("identified_traps", []),
                    disqualification_risk_pct=float(llm_res.get("disqualification_risk_pct", 30.0)),
                    veto_recommended=bool(llm_res.get("veto_recommended", False)),
                    recommended_contingency=llm_res.get("recommended_contingency", "Proceed with standard stops."),
                    severity_level=llm_res.get("severity_level", "LOW"),
                )
            except (TypeError, KeyError, ValueError) as exc:
                logger.debug("[%s] Invalid LLM JSON payload (%s); using heuristic.", self.name, exc)

        # Deterministic Adversarial Heuristic Engine
        traps: list[str] = []
        risk_pct = 20.0
        thesis_points: list[str] = []
        severity: Literal["LOW", "MODERATE", "SEVERE", "CRITICAL"] = "LOW"
        veto = False

        if proposed_direction.upper() == "LONG":
            if ctx.rsi_14 > 68.0:
                traps.append(f"Overbought Momentum Trap: RSI is {ctx.rsi_14:.1f}; high probability of pullback into 20 EMA")
                risk_pct += 28.0
                thesis_points.append("Entering long after extended expansion offers poor asymmetric reward.")
            if ctx.vix_level > 24.0:
                traps.append(f"Elevated Volatility Flush: VIX {ctx.vix_level:.1f} creates wide bid-ask spread and gap-down vulnerability")
                risk_pct += 25.0
            if ctx.market_regime == "CHOPPY":
                traps.append("Range-bound fakeout: Chop regime frequently triggers false upward breakouts")
                risk_pct += 18.0
        else: # SHORT
            if ctx.rsi_14 < 35.0:
                traps.append(f"Oversold Short Trap: RSI is {ctx.rsi_14:.1f}; vulnerable to violent short-covering squeeze")
                risk_pct += 28.0
                thesis_points.append("Shorting near key liquidity demand zone presents severe squeeze risk.")
            if ctx.market_regime == "BULLISH":
                traps.append("Counter-trend fighting: Shorting into a secular bullish regime")
                risk_pct += 30.0

        if len(traps) >= 2:
            severity = "SEVERE" if risk_pct < 75 else "CRITICAL"
        elif len(traps) == 1:
            severity = "MODERATE"

        if risk_pct >= 75.0:
            veto = True
            contingency = "Full Veto: Discard trade setup due to severe asymmetric downside."
        elif risk_pct >= 45.0:
            contingency = "Downsize capital allocation by 50% and demand strict 1.5x ATR trailing stop."
        else:
            contingency = "Pass with standard risk controls; no fatal structural flaws detected."

        if not thesis_points:
            thesis_points.append(f"Counter-thesis evaluated on {ctx.ticker}: Market structure exhibits acceptable risk parameters.")

        return DevilsAdvocateReview(
            adversarial_thesis=" ".join(thesis_points),
            identified_traps=traps or ["No major structural bear/bull traps identified"],
            disqualification_risk_pct=round(min(100.0, risk_pct), 1),
            veto_recommended=veto,
            recommended_contingency=contingency,
            severity_level=severity
        )
