import logging
from typing import List, Dict, Any, Optional
import pandas as pd

from argus_ai.core.models import (
    MarketContext, AgentVote, ConsensusDecision, TradePassport, DevilsAdvocateReview
)
from argus_ai.agents.quant_agent import QuantAgent
from argus_ai.agents.news_agent import NewsAgent
from argus_ai.agents.macro_agent import MacroAgent
from argus_ai.agents.options_agent import OptionsFlowAgent
from argus_ai.agents.devils_advocate import DevilsAdvocateAgent
from argus_ai.agents.risk_commander import RiskCommander
from argus_ai.core.adaptive_weights import AdaptiveWeightManager

logger = logging.getLogger("argus.committee")


class CommitteeOrchestrator:
    """
    Investment Committee Orchestrator (ARGUS 2.0):
    Coordinates multi-agent debate, integrates Options Flow & Derivatives signals,
    applies dynamic Bayesian adaptive weighting, evaluates Devil's Advocate adversarial
    stress-testing, synthesizes consensus, and generates institutional Trade Passports.
    """

    def __init__(self, adaptive_weight_mgr: Optional[AdaptiveWeightManager] = None):
        self.quant = QuantAgent()
        self.news = NewsAgent()
        self.macro = MacroAgent()
        self.options = OptionsFlowAgent()
        self.devils_advocate = DevilsAdvocateAgent()
        self.risk_commander = RiskCommander()
        self.weight_mgr = adaptive_weight_mgr or AdaptiveWeightManager()

    def evaluate_opportunity(
        self,
        ctx: MarketContext,
        bars_df: pd.DataFrame,
        account_equity: float = 100000.0
    ) -> TradePassport:
        logger.info("🏛️ [ARGUS COMMITTEE] Convening Investment Committee deliberation for %s...", ctx.ticker)

        # 1. Gather Specialized Agent Theses
        quant_vote = self.quant.analyze(ctx, bars_df)
        news_vote = self.news.analyze(ctx)
        macro_vote = self.macro.analyze(ctx)
        options_vote = self.options.analyze(ctx)

        agent_votes = [quant_vote, news_vote, macro_vote, options_vote]

        # 2. Retrieve Dynamic Bayesian Weights
        w_quant = self.weight_mgr.get_weight("Quant Agent")
        w_news = self.weight_mgr.get_weight("News & Sentiment Agent")
        w_macro = self.weight_mgr.get_weight("Macro & Sector Agent")
        w_options = self.weight_mgr.get_weight("Options Flow Agent")

        # Normalize weights
        total_w = w_quant + w_news + w_macro + w_options
        w_quant /= total_w
        w_news /= total_w
        w_macro /= total_w
        w_options /= total_w

        # 3. Dynamic Weighted Score Synthesis
        composite_score = (
            quant_vote.score * w_quant +
            news_vote.score * w_news +
            macro_vote.score * w_macro +
            options_vote.score * w_options
        )
        avg_confidence = (
            quant_vote.confidence * w_quant +
            news_vote.confidence * w_news +
            macro_vote.confidence * w_macro +
            options_vote.confidence * w_options
        )

        proposed_direction = "LONG" if composite_score >= 0 else "SHORT"

        # 4. Adversarial Stress-Test by Devil's Advocate
        da_review = self.devils_advocate.stress_test(
            ctx=ctx,
            proposed_direction=proposed_direction,
            votes=agent_votes
        )

        # 5. Consensus & Veto Determination
        status = "HOLD"
        downsize_factor = 1.0
        primary_cat = quant_vote.key_arguments[0] if quant_vote.key_arguments else "Technical alignment"
        main_opp = da_review.identified_traps[0] if da_review.identified_traps else "None"

        # Check for Devil's Advocate Veto
        if da_review.veto_recommended or da_review.disqualification_risk_pct >= 75.0:
            status = "VETOED"
            downsize_factor = 0.0
            exec_summary = (
                f"Trade proposal VETOED by Devil's Advocate. Severe downside trap detected: {main_opp}."
            )
        elif abs(composite_score) < 25.0:
            status = "REJECTED"
            downsize_factor = 0.0
            exec_summary = (
                f"Trade proposal REJECTED due to insufficient edge (Composite Score: {composite_score:+.1f})."
            )
        elif da_review.disqualification_risk_pct >= 45.0:
            status = "DOWNSIZED"
            downsize_factor = 0.50
            exec_summary = (
                f"Trade proposal APPROVED WITH CAUTION. Risk downsized by 50% due to Devil's Advocate concerns: {main_opp}."
            )
        else:
            status = "APPROVED"
            downsize_factor = 1.0
            exec_summary = (
                f"Trade proposal APPROVED with strong multi-agent consensus (Score: {composite_score:+.1f}, Confidence: {avg_confidence*100:.0f}%)."
            )

        unanimous = all(v.recommendation == ("BUY" if proposed_direction == "LONG" else "SELL") for v in agent_votes)

        consensus = ConsensusDecision(
            status=status,
            overall_confidence=round(avg_confidence, 2),
            composite_score=round(composite_score, 1),
            downsize_factor=downsize_factor,
            primary_catalyst=primary_cat,
            main_opposition=main_opp,
            executive_summary=exec_summary,
            unanimous=unanimous
        )

        # 6. Risk Commander Position Sizing & Bracket Parameters
        risk_params = self.risk_commander.calculate_trade_parameters(
            ctx=ctx,
            direction=proposed_direction,
            consensus=consensus,
            account_equity=account_equity
        )

        # 7. Assemble Immutable Trade Passport
        passport = TradePassport(
            ticker=ctx.ticker,
            asset_class="CRYPTO" if ("BTC" in ctx.ticker or "ETH" in ctx.ticker) else "US_EQUITY",
            direction=proposed_direction,
            current_price=ctx.current_price,
            target_entry=risk_params["entry_price"],
            stop_loss=risk_params["stop_loss"],
            take_profit=risk_params["take_profit"],
            risk_reward_ratio=risk_params["risk_reward_ratio"],
            shares_or_contracts=risk_params["shares"],
            position_size_usd=risk_params["position_size_usd"],
            account_risk_usd=risk_params["account_risk_usd"],
            account_risk_pct=risk_params["account_risk_pct"],
            consensus=consensus,
            agent_votes=agent_votes,
            devils_advocate=da_review,
            execution_status="PENDING"
        )

        logger.info("🛡️ [ARGUS COMMITTEE] Deliberation complete. Status: %s | Score: %+.1f",
                    passport.consensus.status, passport.consensus.composite_score)

        return passport
