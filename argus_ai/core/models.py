from typing import List, Dict, Any, Optional, Literal
from pydantic import BaseModel, Field
import datetime
import uuid


class AgentVote(BaseModel):
    agent_name: str
    recommendation: Literal["BUY", "SELL", "HOLD", "VETO"]
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0")
    score: float = Field(ge=-100.0, le=100.0, description="Directional sentiment score (-100 to +100)")
    key_arguments: List[str] = Field(default_factory=list)
    concerns_raised: List[str] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)


class DevilsAdvocateReview(BaseModel):
    adversarial_thesis: str
    identified_traps: List[str] = Field(default_factory=list)
    disqualification_risk_pct: float = Field(ge=0.0, le=100.0)
    veto_recommended: bool
    recommended_contingency: str
    severity_level: Literal["LOW", "MODERATE", "SEVERE", "CRITICAL"] = "LOW"


class ConsensusDecision(BaseModel):
    status: Literal["APPROVED", "DOWNSIZED", "REJECTED", "VETOED"]
    overall_confidence: float = Field(ge=0.0, le=1.0)
    composite_score: float = Field(ge=-100.0, le=100.0)
    downsize_factor: float = Field(default=1.0, ge=0.0, le=1.0)
    primary_catalyst: str
    main_opposition: str
    executive_summary: str
    unanimous: bool = False


class MarketContext(BaseModel):
    ticker: str
    current_price: float
    high_24h: float = 0.0
    low_24h: float = 0.0
    volume_24h: float = 0.0
    rsi_14: float = 50.0
    macd_signal: str = "NEUTRAL"
    atr_14: float = 1.0
    sma_50: float = 0.0
    sma_200: float = 0.0
    market_regime: str = "NEUTRAL" # BULLISH, BEARISH, CHOPPY, VOLATILE
    vix_level: float = 18.0
    recent_news: List[str] = Field(default_factory=list)


class TradePassport(BaseModel):
    passport_id: str = Field(default_factory=lambda: f"PASSPORT-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}")
    timestamp: str = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    ticker: str
    asset_class: Literal["US_EQUITY", "CRYPTO", "FOREX"] = "US_EQUITY"
    direction: Literal["LONG", "SHORT"]
    
    # Pricing & Bracket Strategy
    current_price: float
    target_entry: float
    stop_loss: float
    take_profit: float
    risk_reward_ratio: float
    
    # Risk Allocation
    shares_or_contracts: float
    position_size_usd: float
    account_risk_usd: float
    account_risk_pct: float
    
    # Committee Deliberation
    consensus: ConsensusDecision
    agent_votes: List[AgentVote]
    devils_advocate: DevilsAdvocateReview
    
    # Execution Tracking
    execution_status: Literal["PENDING", "EXECUTED", "REJECTED_BY_RISK", "ABORTED_BY_KILL_SWITCH", "SIMULATED"] = "PENDING"
    alpaca_order_id: Optional[str] = None
    alpaca_client_order_id: Optional[str] = None
    fill_price: Optional[float] = None
    post_trade_reflection: Optional[str] = None


class KillSwitchState(BaseModel):
    is_active: bool = False
    triggered_at: Optional[str] = None
    reason: Optional[str] = None
    circuit_breaker_type: Literal["NONE", "MAX_DAILY_DRAWDOWN", "CONSECUTIVE_LOSSES", "VOLATILITY_SPIKE", "MANUAL"] = "NONE"
    portfolio_frozen: bool = False
    open_orders_cancelled: bool = False
