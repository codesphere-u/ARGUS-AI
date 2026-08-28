import logging
import datetime
from typing import Optional, Dict, Any

from argus_ai.config.settings import CONFIG, RiskSettings
from argus_ai.core.models import KillSwitchState
from argus_ai.core.alpaca_client import AlpacaClient

logger = logging.getLogger("argus.kill_switch")


class EmergencyKillSwitch:
    """
    Institutional Circuit Breaker & Emergency Portfolio Guard.
    Monitors drawdown, volatility anomalies, and consecutive loss streaks to
    proactively protect capital and freeze automated trading when necessary.
    """

    def __init__(self, alpaca_client: AlpacaClient, risk_settings: Optional[RiskSettings] = None):
        self.alpaca = alpaca_client
        self.risk = risk_settings or CONFIG.risk
        self.state = KillSwitchState()
        self.consecutive_losses: int = 0
        self.peak_daily_equity: float = 0.0

    def inspect_portfolio_health(self, current_vix: float = 18.0) -> KillSwitchState:
        """
        Runs comprehensive automated circuit breaker checks against account metrics.
        Returns the updated KillSwitchState.
        """
        if self.state.is_active:
            return self.state

        acc = self.alpaca.get_account_summary()
        equity = acc.get("equity", 100000.0)

        # Track daily high-water mark
        if equity > self.peak_daily_equity:
            self.peak_daily_equity = equity

        # 1. Daily Drawdown Guard
        if self.peak_daily_equity > 0:
            daily_dd_pct = ((self.peak_daily_equity - equity) / self.peak_daily_equity) * 100.0
            if daily_dd_pct >= self.risk.max_daily_drawdown_pct:
                self.trigger(
                    reason=f"Max Daily Drawdown exceeded ({daily_dd_pct:.2f}% >= {self.risk.max_daily_drawdown_pct}%). Portfolio auto-halted.",
                    circuit_type="MAX_DAILY_DRAWDOWN"
                )
                return self.state

        # 2. Consecutive Loss Streak Guard
        if self.consecutive_losses >= self.risk.max_consecutive_losses:
            self.trigger(
                reason=f"Consecutive loss limit reached ({self.consecutive_losses} consecutive stop-outs). Trading suspended for cooldown.",
                circuit_type="CONSECUTIVE_LOSSES"
            )
            return self.state

        # 3. Market Stress / Extreme VIX Spike
        if current_vix >= 38.0:
            self.trigger(
                reason=f"Extreme market regime detected (VIX = {current_vix:.1f} >= 38.0). Capital preservation freeze active.",
                circuit_type="VOLATILITY_SPIKE"
            )
            return self.state

        return self.state

    def record_trade_result(self, pnl_usd: float):
        """Update consecutive loss counter based on closed trade outcome."""
        if pnl_usd < 0:
            self.consecutive_losses += 1
            logger.warning("[KILL SWITCH] Trade loss recorded: $%.2f. Streak: %d", pnl_usd, self.consecutive_losses)
        else:
            self.consecutive_losses = 0
            logger.info("[KILL SWITCH] Trade profit recorded: $%.2f. Loss streak reset to 0.", pnl_usd)

    def trigger(self, reason: str, circuit_type: str = "MANUAL", liquidate_all: bool = False):
        """
        Engage the Kill Switch: Freeze portfolio and cancel all open Alpaca orders.
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.state.is_active = True
        self.state.triggered_at = now
        self.state.reason = reason
        self.state.circuit_breaker_type = circuit_type
        self.state.portfolio_frozen = True

        logger.critical("🚨🚨 [EMERGENCY KILL SWITCH ENGAGED] 🚨🚨: %s", reason)

        # Immediate Order Cancellation
        cancelled_orders = self.alpaca.cancel_all_orders()
        self.state.open_orders_cancelled = True
        logger.warning("[KILL SWITCH] Cancelled %d open working orders on Alpaca.", cancelled_orders)

        if liquidate_all:
            logger.warning("[KILL SWITCH] Emergency liquidation requested: closing all open positions.")
            self.alpaca.close_all_positions()

    def reset_override(self, manual_justification: str):
        """Authorize manual recovery and resume trading after inspection."""
        logger.info("[KILL SWITCH] Manual override executed by operator: '%s'", manual_justification)
        self.state = KillSwitchState()
        self.consecutive_losses = 0
        acc = self.alpaca.get_account_summary()
        self.peak_daily_equity = acc.get("equity", 100000.0)
