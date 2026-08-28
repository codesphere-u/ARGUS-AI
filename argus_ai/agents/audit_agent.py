import json
import logging
from typing import List, Dict, Any
from pathlib import Path
from argus_ai.core.models import TradePassport
from argus_ai.agents.base_agent import BaseAgent

logger = logging.getLogger("argus.audit")


class AuditAgent(BaseAgent):
    """
    Audit & Reflection Agent: Preserves full committee debate transcripts,
    generates Trade Passports, and conducts post-trade attribution reviews.
    """

    def __init__(self, log_dir: str = "argus_ai/data/passports"):
        super().__init__("Audit & Reflection Agent")
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.passports: List[TradePassport] = []

    def record_passport(self, passport: TradePassport) -> Path:
        """Persist trade passport to JSON log file."""
        self.passports.append(passport)
        file_path = self.log_dir / f"{passport.passport_id}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(passport.model_dump_json(indent=2))
        logger.info("[AUDIT] Trade Passport archived to %s", file_path)
        return file_path

    def generate_post_trade_reflection(self, passport: TradePassport, exit_pnl: float) -> str:
        """
        Evaluate how well the committee's prediction and the Devil's Advocate's
        warnings matched real market behavior after trade closure.
        """
        outcome_str = "PROFITABLE (WIN)" if exit_pnl > 0 else "STOPPED OUT (LOSS)"
        
        system_prompt = (
            "You are the Chief Post-Trade Reflection Auditor for ARGUS AI. "
            "Evaluate why this trade succeeded or failed. Specifically comment on whether the "
            "Devil's Advocate's concerns were validated. Output a 3-sentence executive reflection."
        )
        user_prompt = (
            f"Asset: {passport.ticker} ({passport.direction})\n"
            f"Outcome: {outcome_str} (PnL: ${exit_pnl:.2f})\n"
            f"Consensus Confidence: {passport.consensus.overall_confidence:.2f}\n"
            f"Devil's Advocate Warnings: {', '.join(passport.devils_advocate.identified_traps)}\n"
        )

        llm_res = self.query_llm_json(system_prompt, user_prompt)
        if llm_res and "reflection" in llm_res:
            reflection = llm_res["reflection"]
        else:
            if exit_pnl > 0:
                reflection = f"Trade on {passport.ticker} hit target profit. Quant momentum overrode initial Devil's Advocate cautions cleanly."
            else:
                traps = passport.devils_advocate.identified_traps[0] if passport.devils_advocate.identified_traps else "liquidity drawdown"
                reflection = f"Trade on {passport.ticker} stopped out. Devil's Advocate thesis validated regarding: {traps}."

        passport.post_trade_reflection = reflection
        self.record_passport(passport)
        return reflection
