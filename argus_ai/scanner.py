import logging

from argus_ai.agents.audit_agent import AuditAgent
from argus_ai.agents.execution_agent import ExecutionAgent
from argus_ai.committee import CommitteeOrchestrator
from argus_ai.config.settings import CONFIG
from argus_ai.core.alpaca_client import AlpacaClient
from argus_ai.core.kill_switch import EmergencyKillSwitch
from argus_ai.core.models import MarketContext, TradePassport

logger = logging.getLogger("argus.scanner")


class OpportunityScanner:
    """
    Market Opportunity Scanner:
    Continuously monitors watchlist assets, builds live market context,
    runs the ARGUS Investment Committee, and routes approved trades for execution.
    """

    def __init__(
        self,
        alpaca_client: AlpacaClient | None = None,
        kill_switch: EmergencyKillSwitch | None = None,
        use_llm: bool = True,
    ) -> None:
        self.alpaca = alpaca_client or AlpacaClient()
        self.kill_switch = kill_switch or EmergencyKillSwitch(self.alpaca)
        self.committee = CommitteeOrchestrator(use_llm=use_llm)
        self.execution = ExecutionAgent(self.alpaca, self.kill_switch)
        self.audit = AuditAgent()

    def scan_asset(self, symbol: str, custom_regime: str = "BULLISH", custom_vix: float = 17.5) -> TradePassport:
        """Scan a single asset, convene committee, and execute if approved."""
        # 1. Health check kill switch
        self.kill_switch.inspect_portfolio_health(current_vix=custom_vix)

        # 2. Fetch market data
        current_price = self.alpaca.get_latest_price(symbol)
        bars_df = self.alpaca.get_historical_bars(symbol, bars=50)

        # 3. Formulate market context
        ctx = MarketContext(
            ticker=symbol,
            current_price=current_price,
            high_24h=float(bars_df["high"].max()),
            low_24h=float(bars_df["low"].min()),
            volume_24h=float(bars_df["volume"].sum()),
            market_regime=custom_regime,
            vix_level=custom_vix,
            recent_news=[
                f"{symbol} experiencing elevated institutional volume on product roadmap updates",
                "Sector momentum remains constructive across tech and growth equities",
            ],
        )

        acc = self.alpaca.get_account_summary()
        equity = float(acc.get("equity", 100000.0))

        # 4. Committee Deliberation
        passport = self.committee.evaluate_opportunity(
            ctx=ctx,
            bars_df=bars_df,
            account_equity=equity,
        )

        # 5. Execution Pipeline
        passport = self.execution.execute_passport(passport)

        # 6. Audit & Archive
        self.audit.record_passport(passport)

        return passport

    def scan_watchlist(self, watchlist: list[str] | None = None) -> list[TradePassport]:
        """Scan full watchlist and return generated Trade Passports."""
        symbols = watchlist or CONFIG.watchlist
        results: list[TradePassport] = []
        for sym in symbols:
            try:
                p = self.scan_asset(sym)
                results.append(p)
            except Exception as e:  # noqa: BLE001 - isolate watchlist asset scan failures
                logger.error("[SCANNER] Error scanning %s: %s", sym, e)
        return results
