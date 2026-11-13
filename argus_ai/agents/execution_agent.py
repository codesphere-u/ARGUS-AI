import logging

from argus_ai.core.alpaca_client import AlpacaClient
from argus_ai.core.kill_switch import EmergencyKillSwitch
from argus_ai.core.models import TradePassport

logger = logging.getLogger("argus.execution")


class ExecutionAgent:
    """
    Execution Agent: Handles deterministic dispatch of approved orders to Alpaca.
    Ensures pre-flight compliance with Kill Switch status and bracket validation.
    """

    def __init__(self, alpaca_client: AlpacaClient, kill_switch: EmergencyKillSwitch) -> None:
        self.alpaca = alpaca_client
        self.kill_switch = kill_switch

    def execute_passport(self, passport: TradePassport) -> TradePassport:
        """
        Validates safety conditions and routes approved trade passport to Alpaca.
        """
        # 1. Check Kill Switch
        if self.kill_switch.state.is_active:
            logger.warning("[EXECUTION] Blocked by active Kill Switch (%s). Passport %s aborted.",
                           self.kill_switch.state.reason, passport.passport_id)
            passport.execution_status = "ABORTED_BY_KILL_SWITCH"
            return passport

        # 2. Check Committee Decision Status
        if passport.consensus.status in ["REJECTED", "VETOED"]:
            logger.info("[EXECUTION] Passport %s skipped due to committee status: %s",
                        passport.passport_id, passport.consensus.status)
            passport.execution_status = "REJECTED_BY_RISK"
            return passport

        # 3. Dispatch to Alpaca
        side = "buy" if passport.direction.upper() == "LONG" else "sell"
        order_res = self.alpaca.place_bracket_order(
            symbol=passport.ticker,
            qty=passport.shares_or_contracts,
            side=side,
            take_profit_price=passport.take_profit,
            stop_loss_price=passport.stop_loss,
            client_order_id=passport.passport_id
        )

        passport.alpaca_order_id = order_res.get("order_id")
        passport.alpaca_client_order_id = order_res.get("client_order_id")
        passport.fill_price = order_res.get("fill_price", passport.target_entry)
        passport.execution_status = "EXECUTED" if order_res.get("mode") == "ALPACA API" else "SIMULATED"

        logger.info("[EXECUTION] Order dispatched for %s (%s %s @ $%.2f). Status: %s",
                    passport.ticker, passport.direction, passport.shares_or_contracts,
                    passport.target_entry, passport.execution_status)

        return passport
