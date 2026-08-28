import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch

from argus_ai.core.models import (
    MarketContext, AgentVote, ConsensusDecision, TradePassport, DevilsAdvocateReview, KillSwitchState
)
from argus_ai.committee import CommitteeOrchestrator
from argus_ai.agents.quant_agent import QuantAgent
from argus_ai.agents.news_agent import NewsAgent
from argus_ai.agents.macro_agent import MacroAgent
from argus_ai.agents.devils_advocate import DevilsAdvocateAgent
from argus_ai.agents.risk_commander import RiskCommander
from argus_ai.agents.audit_agent import AuditAgent
from argus_ai.core.kill_switch import EmergencyKillSwitch


@pytest.fixture(autouse=True)
def mock_llm_fallback():
    """Ensure tests run deterministically and fast without real LLM network calls."""
    with patch("argus_ai.agents.base_agent.BaseAgent._initialize_gemini", return_value=None), \
         patch("argus_ai.agents.base_agent.BaseAgent.query_llm_json", return_value=None):
        yield


def create_sample_bars(n=100, mode="bullish"):
    dates = pd.date_range("2026-01-01", periods=n, freq="h")
    if mode == "bullish":
        # Balanced 1.3:1 gain/loss ratio keeping RSI in the 55-62 range
        pattern = [0.30, -0.20, 0.25, -0.18, 0.20, -0.15]
        changes = np.resize(pattern, n)
        close = 100.0 + np.cumsum(changes)
    elif mode == "bearish":
        pattern = [-0.30, 0.20, -0.25, 0.18, -0.20, 0.15]
        changes = np.resize(pattern, n)
        close = 140.0 + np.cumsum(changes)
    else:
        close = 100.0 + np.random.normal(0, 1.0, n)

    high = close + 0.5
    low = close - 0.5
    open_p = close - 0.1
    volume = np.full(n, 25000.0)

    return pd.DataFrame({
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume
    }, index=dates)


def test_quant_agent_bullish_trend():
    agent = QuantAgent()
    bars = create_sample_bars(100, mode="bullish")
    last_close = float(bars["close"].iloc[-1])
    ctx = MarketContext(ticker="NVDA", current_price=last_close, rsi_14=58.0, atr_14=2.0)
    
    vote = agent.analyze(ctx, bars)
    assert vote.agent_name == "Quant Agent"
    assert vote.recommendation == "BUY"
    assert vote.score > 0
    assert vote.confidence >= 0.5


def test_quant_agent_bearish_trend():
    agent = QuantAgent()
    bars = create_sample_bars(100, mode="bearish")
    last_close = float(bars["close"].iloc[-1])
    ctx = MarketContext(ticker="TSLA", current_price=last_close, rsi_14=42.0, atr_14=2.5)
    
    vote = agent.analyze(ctx, bars)
    assert vote.recommendation == "SELL"
    assert vote.score < 0


def test_devils_advocate_veto_on_extreme_traps():
    da = DevilsAdvocateAgent()
    # Overbought RSI + high VIX in a long setup -> high trap risk
    ctx = MarketContext(
        ticker="NVDA",
        current_price=150.0,
        rsi_14=78.0,
        vix_level=28.0,
        market_regime="CHOPPY"
    )
    votes = [
        AgentVote(agent_name="Quant", recommendation="BUY", confidence=0.8, score=60.0, key_arguments=["Breakout above 148"]),
        AgentVote(agent_name="News", recommendation="BUY", confidence=0.7, score=50.0, key_arguments=["Positive AI demand headlines"]),
    ]

    review = da.stress_test(ctx, "LONG", votes)
    assert review.disqualification_risk_pct >= 70.0
    assert len(review.identified_traps) >= 2
    assert review.veto_recommended is True or review.disqualification_risk_pct >= 75.0


def test_devils_advocate_downsize_on_moderate_concern():
    da = DevilsAdvocateAgent()
    ctx = MarketContext(
        ticker="AAPL",
        current_price=220.0,
        rsi_14=69.0, # Just slightly overbought
        vix_level=16.0,
        market_regime="BULLISH"
    )
    votes = [
        AgentVote(agent_name="Quant", recommendation="BUY", confidence=0.75, score=55.0, key_arguments=["Strong 50 EMA bounce"]),
    ]

    review = da.stress_test(ctx, "LONG", votes)
    assert 40.0 <= review.disqualification_risk_pct < 75.0
    assert review.veto_recommended is False


def test_committee_orchestrator_full_flow():
    orchestrator = CommitteeOrchestrator()
    bars = create_sample_bars(100, mode="bullish")
    last_close = float(bars["close"].iloc[-1])
    ctx = MarketContext(
        ticker="SPY",
        current_price=last_close,
        rsi_14=58.0,
        vix_level=14.5,
        market_regime="BULLISH",
        atr_14=2.0
    )

    passport = orchestrator.evaluate_opportunity(ctx, bars, account_equity=100000.0)
    assert isinstance(passport, TradePassport)
    assert passport.ticker == "SPY"
    assert passport.direction == "LONG"
    assert passport.consensus.status in ["APPROVED", "DOWNSIZED", "REJECTED", "VETOED"]
    assert passport.stop_loss < passport.current_price
    assert passport.take_profit > passport.current_price
    assert passport.risk_reward_ratio >= 1.5
    assert len(passport.agent_votes) == 4


def test_risk_commander_bracket_and_sizing():
    commander = RiskCommander()
    consensus = ConsensusDecision(
        status="APPROVED",
        overall_confidence=0.85,
        composite_score=60.0,
        downsize_factor=1.0,
        primary_catalyst="Strong trend",
        main_opposition="None",
        executive_summary="Approved"
    )
    ctx = MarketContext(ticker="AAPL", current_price=200.0, atr_14=4.0)
    
    params = commander.calculate_trade_parameters(ctx, "LONG", consensus, account_equity=100000.0)
    assert params["entry_price"] == 200.0
    assert params["stop_loss"] == 194.0 # 200 - (1.5 * 4.0)
    assert params["take_profit"] == 212.0 # 200 + (3.0 * 4.0) assuming 2:1 RR
    assert params["account_risk_usd"] <= 1000.0 # Max 1% of $100k
    assert params["shares"] > 0


def test_emergency_kill_switch_drawdown_trip():
    mock_alpaca = MagicMock()
    mock_alpaca.get_account_summary.return_value = {"equity": 94000.0} # 6% drawdown from 100k
    mock_alpaca.cancel_all_orders.return_value = 4
    
    kill_switch = EmergencyKillSwitch(alpaca_client=mock_alpaca)
    kill_switch.peak_daily_equity = 100000.0
    
    state = kill_switch.inspect_portfolio_health(current_vix=18.0)
    assert state.is_active is True
    assert state.circuit_breaker_type == "MAX_DAILY_DRAWDOWN"
    assert state.portfolio_frozen is True
    assert mock_alpaca.cancel_all_orders.called


def test_emergency_kill_switch_consecutive_losses():
    mock_alpaca = MagicMock()
    mock_alpaca.get_account_summary.return_value = {"equity": 100000.0}
    mock_alpaca.cancel_all_orders.return_value = 0
    
    kill_switch = EmergencyKillSwitch(alpaca_client=mock_alpaca)
    kill_switch.consecutive_losses = 3
    
    state = kill_switch.inspect_portfolio_health(current_vix=18.0)
    assert state.is_active is True
    assert state.circuit_breaker_type == "CONSECUTIVE_LOSSES"
