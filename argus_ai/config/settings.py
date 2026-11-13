import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class AlpacaSettings:
    api_key: str = field(default_factory=lambda: os.getenv("ALPACA_API_KEY", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("ALPACA_SECRET_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"))
    use_paper: bool = field(default_factory=lambda: os.getenv("ALPACA_USE_PAPER", "true").lower() == "true")


@dataclass
class RiskSettings:
    max_account_risk_pct: float = 1.0       # Max risk per trade (1% of equity)
    max_portfolio_exposure_pct: float = 25.0 # Max total portfolio capital deployed
    max_daily_drawdown_pct: float = 2.5     # Emergency kill switch triggers at -2.5% daily drawdown
    max_position_size_usd: float = 10000.0   # Absolute ceiling per position
    max_consecutive_losses: int = 3          # Halts new entries after 3 straight losses
    default_risk_reward_ratio: float = 2.0  # Minimum required R:R
    volatility_lookback_bars: int = 20


@dataclass
class CommitteeSettings:
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    model_name: str = field(default_factory=lambda: os.getenv("ARGUS_MODEL", "gemini-2.5-flash"))
    consensus_threshold: float = 0.65       # Minimum weighted consensus score to consider a trade
    devils_advocate_veto_threshold: float = 0.80 # Veto probability threshold from Devil's Advocate
    enable_mcp: bool = True


@dataclass
class ArgusConfig:
    alpaca: AlpacaSettings = field(default_factory=AlpacaSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    committee: CommitteeSettings = field(default_factory=CommitteeSettings)
    watchlist: list[str] = field(
        default_factory=lambda: [
            "NVDA",
            "AAPL",
            "MSFT",
            "TSLA",
            "AMD",
            "SPY",
            "QQQ",
            "BTC/USD",
            "ETH/USD",
        ]
    )


CONFIG = ArgusConfig()
