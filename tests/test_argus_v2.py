import pytest
import json
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch

from argus_ai.core.models import MarketContext, TradePassport, ConsensusDecision, DevilsAdvocateReview
from argus_ai.core.adaptive_weights import AdaptiveWeightManager
from argus_ai.agents.options_agent import OptionsFlowAgent
from argus_ai.mcp_server import ArgusMCPServer
from argus_ai.core.streamer import MarketStreamListener
from argus_ai.core.alerts import InteractiveAlertDispatcher


@pytest.fixture(autouse=True)
def mock_llm_fallback():
    """Ensure tests run deterministically and fast without real LLM network calls."""
    with patch("argus_ai.agents.base_agent.BaseAgent._initialize_gemini", return_value=None), \
         patch("argus_ai.agents.base_agent.BaseAgent.query_llm_json", return_value=None):
        yield


def test_adaptive_weight_manager(tmp_path):
    temp_file = tmp_path / "test_weights.json"
    mgr = AdaptiveWeightManager(persistence_file=str(temp_file), learning_rate=0.1)

    initial_quant_w = mgr.get_weight("Quant Agent")
    assert initial_quant_w > 0

    # Simulate Quant winning call
    mgr.update_agent_performance("Quant Agent", trade_pnl=500.0, was_aligned=True)
    new_quant_w = mgr.get_weight("Quant Agent")
    assert new_quant_w >= initial_quant_w

    # Weights must sum to ~1.0
    total = sum(mgr.weights.values())
    assert abs(total - 1.0) < 0.05
    assert temp_file.exists()


def test_options_flow_agent_bullish():
    agent = OptionsFlowAgent()
    ctx = MarketContext(ticker="NVDA", current_price=150.0, market_regime="BULLISH", vix_level=16.0)
    vote = agent.analyze(ctx)

    assert vote.agent_name == "Options Flow Agent"
    assert vote.recommendation == "BUY"
    assert vote.score > 0
    assert vote.confidence >= 0.7


def test_options_flow_agent_bearish():
    agent = OptionsFlowAgent()
    ctx = MarketContext(ticker="TSLA", current_price=200.0, market_regime="BEARISH", vix_level=28.0)
    vote = agent.analyze(ctx)

    assert vote.recommendation == "SELL"
    assert vote.score < 0


def test_mcp_server_tools_list():
    server = ArgusMCPServer()
    req = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/list",
        "params": {}
    }
    resp = server.process_json_rpc(req)
    assert resp["id"] == "1"
    tools = resp["result"]["tools"]
    tool_names = [t["name"] for t in tools]
    assert "argus_convene_committee" in tool_names
    assert "argus_emergency_kill_switch" in tool_names
    assert "alpaca_get_portfolio_telemetry" in tool_names


def test_mcp_server_convene_committee_tool():
    server = ArgusMCPServer()
    req = {
        "jsonrpc": "2.0",
        "id": "2",
        "method": "tools/call",
        "params": {
            "name": "argus_convene_committee",
            "arguments": {
                "ticker": "NVDA",
                "current_price": 148.50,
                "market_regime": "BULLISH",
                "vix_level": 17.0
            }
        }
    }
    resp = server.process_json_rpc(req)
    assert resp["id"] == "2"
    content = resp["result"]["content"][0]["text"]
    passport_data = json.loads(content)
    assert passport_data["ticker"] == "NVDA"
    assert "consensus" in passport_data


def test_mcp_server_kill_switch_trip():
    server = ArgusMCPServer()
    req = {
        "jsonrpc": "2.0",
        "id": "3",
        "method": "tools/call",
        "params": {
            "name": "argus_emergency_kill_switch",
            "arguments": {
                "action": "TRIP",
                "justification": "Test trip via MCP"
            }
        }
    }
    resp = server.process_json_rpc(req)
    content = json.loads(resp["result"]["content"][0]["text"])
    assert content["status"] == "KILL_SWITCH_ENGAGED"
    assert content["state"]["portfolio_frozen"] is True


def test_streamer_tick_processing():
    listener = MarketStreamListener(symbols=["NVDA"])
    result = listener.process_incoming_tick(symbol="NVDA", price=152.0, volume=35000.0)
    assert result is not None
    assert result["ticker"] == "NVDA"
    assert "consensus" in result


def test_alert_dispatcher_formatting():
    passport = TradePassport(
        ticker="BTC/USD",
        asset_class="CRYPTO",
        direction="LONG",
        current_price=88420.0,
        target_entry=88420.0,
        stop_loss=86100.0,
        take_profit=93980.0,
        risk_reward_ratio=2.4,
        shares_or_contracts=0.43,
        position_size_usd=38020.0,
        account_risk_usd=1000.0,
        account_risk_pct=1.0,
        consensus=ConsensusDecision(
            status="APPROVED",
            overall_confidence=0.88,
            composite_score=68.0,
            downsize_factor=1.0,
            primary_catalyst="Bullish momentum",
            main_opposition="None",
            executive_summary="Approved with high confidence",
            unanimous=True
        ),
        agent_votes=[],
        devils_advocate=DevilsAdvocateReview(
            adversarial_thesis="Low trap risk",
            identified_traps=[],
            disqualification_risk_pct=18.0,
            veto_recommended=False,
            recommended_contingency="Standard bracket stops",
            severity_level="LOW"
        ),
        execution_status="EXECUTED"
    )

    tg_payload = InteractiveAlertDispatcher.format_telegram_alert(passport)
    assert "BTC/USD" in tg_payload["text"]
    assert "APPROVED" in tg_payload["text"]
    assert len(tg_payload["reply_markup"]["inline_keyboard"]) == 2

    discord_payload = InteractiveAlertDispatcher.format_discord_webhook(passport)
    assert "embeds" in discord_payload
    assert discord_payload["embeds"][0]["title"] == "🛡️ Trade Passport: BTC/USD (LONG)"
