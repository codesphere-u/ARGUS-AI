import logging
from typing import Any

from argus_ai.config.settings import CONFIG, RiskSettings
from argus_ai.core.models import ConsensusDecision, MarketContext

logger = logging.getLogger("argus.risk_commander")


class RiskCommander:
    """
    Risk Commander: Formulates institutional position sizing, hard stop-loss brackets,
    target profit levels, and validates portfolio exposure limits.
    """

    def __init__(self, risk_settings: RiskSettings | None = None) -> None:
        self.settings = risk_settings or CONFIG.risk

    def calculate_trade_parameters(
        self,
        ctx: MarketContext,
        direction: str,
        consensus: ConsensusDecision,
        account_equity: float,
    ) -> dict[str, Any]:
        """
        Derive dynamic bracket prices (Entry, Stop Loss, Take Profit)
        and volatility-calibrated share quantity.
        """
        current_price = ctx.current_price
        atr = max(ctx.atr_14, current_price * 0.01)

        # Multiplier on ATR based on direction & volatility
        sl_distance = atr * 1.5
        tp_distance = sl_distance * self.settings.default_risk_reward_ratio

        if direction.upper() == "LONG":
            entry_price = current_price
            stop_loss = round(current_price - sl_distance, 2)
            take_profit = round(current_price + tp_distance, 2)
        else: # SHORT
            entry_price = current_price
            stop_loss = round(current_price + sl_distance, 2)
            take_profit = round(current_price - tp_distance, 2)

        # Dollar risk budget (e.g. 1% of equity)
        base_risk_budget_usd = account_equity * (self.settings.max_account_risk_pct / 100.0)
        
        # Apply downsize factor from committee consensus
        adjusted_risk_budget_usd = base_risk_budget_usd * consensus.downsize_factor
        
        # Dollar risk per share = |Entry - SL|
        risk_per_share = abs(entry_price - stop_loss)
        if risk_per_share <= 0:
            risk_per_share = entry_price * 0.01

        # Raw shares
        raw_shares = adjusted_risk_budget_usd / risk_per_share
        
        # For crypto, fractional is fine; for stocks, round appropriately or keep fractional for Alpaca
        if "BTC" in ctx.ticker or "ETH" in ctx.ticker:
            shares = round(raw_shares, 4)
        else:
            shares = max(1.0, round(raw_shares, 2))

        position_size_usd = round(shares * entry_price, 2)

        # Exposure ceiling check
        if position_size_usd > self.settings.max_position_size_usd:
            position_size_usd = self.settings.max_position_size_usd
            shares = round(position_size_usd / entry_price, 2)
            adjusted_risk_budget_usd = shares * risk_per_share

        actual_risk_pct = round((adjusted_risk_budget_usd / account_equity) * 100.0, 2)
        rr_ratio = round(abs(take_profit - entry_price) / abs(entry_price - stop_loss), 2)

        return {
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward_ratio": rr_ratio,
            "shares": shares,
            "position_size_usd": position_size_usd,
            "account_risk_usd": round(adjusted_risk_budget_usd, 2),
            "account_risk_pct": actual_risk_pct,
            "downsize_applied": consensus.downsize_factor < 1.0
        }
