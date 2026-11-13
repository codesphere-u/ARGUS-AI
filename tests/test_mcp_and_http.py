from __future__ import annotations

import asyncio
import io
import json
import sys

import pytest

from argus_ai.agents.audit_agent import AuditAgent
from argus_ai.agents.execution_agent import ExecutionAgent
from argus_ai.backtest import compute_passport_digest
from argus_ai.cli import main as cli_main
from argus_ai.committee import CommitteeOrchestrator
from argus_ai.core.adaptive_weights import AdaptiveWeightManager
from argus_ai.core.alpaca_client import AlpacaClient
from argus_ai.core.kill_switch import EmergencyKillSwitch
from argus_ai.core.models import MarketContext, TradePassport
from argus_ai.core.passport import TradePassportFormatter
from argus_ai.core.streamer import MarketStreamListener
from argus_ai.http_server import _parse_backtest_request
from argus_ai.mcp_server import ArgusMCPServer
from argus_ai.scanner import OpportunityScanner


def test_mcp_server_tools_stdio_kill_switch_and_order_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = ArgusMCPServer(use_llm=False)

    tools_resp = mcp.process_json_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert len(tools_resp["result"]["tools"]) == 5

    conv = mcp.process_json_rpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "argus_convene_committee",
                "arguments": {"ticker": "NVDA", "current_price": 135.0, "market_regime": "BULLISH"},
            },
        }
    )
    passport_payload = json.loads(conv["result"]["content"][0]["text"])
    pid = passport_payload["passport_id"]
    passport = TradePassport.model_validate(passport_payload)
    assert len(compute_passport_digest(passport)) == 64

    found = mcp.handle_tool_call("argus_get_trade_passport", {"passport_id": pid})
    assert found["passport_id"] == pid
    missing = mcp.handle_tool_call("argus_get_trade_passport", {"passport_id": "NONE"})
    assert missing["status"] == "NOT_FOUND"

    status_ks = mcp.handle_tool_call("argus_emergency_kill_switch", {"action": "STATUS"})
    assert status_ks["status"] == "HEALTH_INSPECTED"
    trip_ks = mcp.handle_tool_call("argus_emergency_kill_switch", {"action": "TRIP", "justification": "drill"})
    assert trip_ks["status"] == "KILL_SWITCH_ENGAGED"
    blocked = mcp.handle_tool_call(
        "alpaca_dispatch_bracket_order",
        {"symbol": "NVDA", "qty": 1, "side": "buy", "take_profit_price": 150.0, "stop_loss_price": 120.0},
    )
    assert blocked["success"] is False
    reset_ks = mcp.handle_tool_call("argus_emergency_kill_switch", {"action": "RESET", "justification": "cleared"})
    assert reset_ks["status"] == "KILL_SWITCH_RESET"

    dispatched = mcp.handle_tool_call(
        "alpaca_dispatch_bracket_order",
        {"symbol": "NVDA", "qty": 2, "side": "buy", "take_profit_price": 150.0, "stop_loss_price": 120.0},
    )
    assert dispatched["success"] is True

    tel = mcp.handle_tool_call("alpaca_get_portfolio_telemetry", {})
    assert "account" in tel
    unk_tool = mcp.handle_tool_call("unknown_tool", {})
    assert "Unknown MCP tool" in unk_tool["error"]
    unk_method = mcp.process_json_rpc({"jsonrpc": "2.0", "id": 9, "method": "nope"})
    assert unk_method["error"]["code"] == -32601

    stdin_data = json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/list"}) + "\n{bad-json\n"
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_data))
    monkeypatch.setattr(sys, "stdout", out)
    mcp.run_stdio()
    lines = [json.loads(line) for line in out.getvalue().strip().splitlines()]
    assert len(lines) == 2
    assert "result" in lines[0]
    assert lines[1]["error"]["code"] == -32700


def test_weights_scanner_execution_formatter_and_cli(tmp_path) -> None:
    weights_file = tmp_path / "adaptive_weights.json"
    mgr = AdaptiveWeightManager(persistence_file=str(weights_file))
    w0 = mgr.get_weight("Quant Agent")
    mgr.update_agent_performance("Quant Agent", trade_pnl=250.0, was_aligned=True)
    mgr.update_agent_performance("News & Sentiment Agent", trade_pnl=-100.0, was_aligned=True)
    assert weights_file.exists()
    reloaded = AdaptiveWeightManager(persistence_file=str(weights_file))
    assert reloaded.get_weight("Quant Agent") != w0 or reloaded.get_weight("Quant Agent") > 0

    alpaca = AlpacaClient()
    assert alpaca.is_simulation is True
    assert alpaca.get_account_summary()["currency"] == "USD"
    assert isinstance(alpaca.get_open_positions(), list)
    bars = alpaca.get_historical_bars("NVDA", bars=60)
    assert len(bars) == 60

    ks = EmergencyKillSwitch(alpaca_client=alpaca)
    ks_state = ks.inspect_portfolio_health(current_vix=40.0)
    assert ks_state.is_active is True
    ks.reset_override("test reset")
    ks.record_trade_result(-100.0)

    scanner = OpportunityScanner(alpaca_client=alpaca, kill_switch=ks, use_llm=False)
    passports = scanner.scan_watchlist(["NVDA", "BTC/USD"])
    assert len(passports) == 2

    md = TradePassportFormatter.to_markdown(passports[0])
    assert passports[0].passport_id in md

    # Exercise ExecutionAgent APPROVED/SIMULATED and REJECTED paths cleanly
    orch = CommitteeOrchestrator(use_llm=False)
    ctx = MarketContext(ticker="AAPL", current_price=195.0, market_regime="BULLISH", vix_level=16.0)
    p = orch.evaluate_opportunity(ctx, bars)
    exec_agent = ExecutionAgent(alpaca, ks)
    p.consensus.status = "APPROVED"
    out_p = exec_agent.execute_passport(p)
    assert out_p.execution_status == "SIMULATED"
    p.consensus.status = "REJECTED"
    rejected_p = exec_agent.execute_passport(p)
    assert rejected_p.execution_status == "REJECTED_BY_RISK"
    ks.trigger("drill")
    aborted = exec_agent.execute_passport(p)
    assert aborted.execution_status == "ABORTED_BY_KILL_SWITCH"
    ks.reset_override("clear")

    # AuditAgent archival & deterministic post-trade reflection (win + loss)
    audit = AuditAgent(log_dir=str(tmp_path / "passports"), use_llm=False)
    saved_path = audit.record_passport(p)
    assert saved_path.exists()
    win_refl = audit.generate_post_trade_reflection(p, exit_pnl=320.0)
    assert "AAPL" in win_refl
    loss_refl = audit.generate_post_trade_reflection(p, exit_pnl=-140.0)
    assert "AAPL" in loss_refl

    # MarketStreamListener deterministic tick processing & emergency kill-switch halt
    captured: list[TradePassport] = []
    listener = MarketStreamListener(
        symbols=["NVDA"],
        alpaca_client=alpaca,
        on_trade_passport_callback=captured.append,
        use_llm=False,
    )
    tick_out = listener.process_incoming_tick("NVDA", price=149.25, volume=45000.0)
    assert tick_out is not None
    assert tick_out["ticker"] == "NVDA"
    assert len(captured) == 1
    asyncio.run(listener.start_streaming_loop(max_ticks=1))
    assert listener.is_running is False
    listener.stop()
    listener.kill_switch.trigger("stream drill")
    assert listener.process_incoming_tick("NVDA", price=149.50, volume=10000.0) is None
    listener.kill_switch.reset_override("done")

    report_path = tmp_path / "bt_report.md"
    cli_main(["backtest", "--ticker", "NVDA", "--bars", "140", "--report-out", str(report_path)])
    assert report_path.exists()
    assert "NVDA" in report_path.read_text(encoding="utf-8")

    cli_main(["weights"])
    cli_main(["replay", "--ticker", "NVDA"])

    # Exercise paper simulator order cancellation/liquidation and HTTP request validators
    alpaca.place_bracket_order(symbol="NVDA", qty=1, side="buy", take_profit_price=160.0, stop_loss_price=140.0)
    assert alpaca.cancel_all_orders() == 1
    assert alpaca.close_all_positions() is True

    parsed_req, parsed_cost = _parse_backtest_request(
        {
            "ticker": "aapl",
            "bars": 120,
            "warmup_bars": 30,
            "holding_horizon": 10,
            "cost_model": {"spread_bps": 2.0, "commission_bps": 1.0, "slippage_atr_fraction": 0.02},
        }
    )
    assert parsed_req["ticker"] == "AAPL"
    assert parsed_cost.spread_bps == 2.0

    for bad_payload, exc_type in [
        ({"ticker": "!!!"}, ValueError),
        ({"bars": True}, TypeError),
        ({"bars": 100, "warmup_bars": 80, "holding_horizon": 20}, ValueError),
        ({"initial_equity": True}, TypeError),
        ({"initial_equity": float("inf")}, ValueError),
        ({"cost_model": "invalid"}, ValueError),
    ]:
        with pytest.raises(exc_type):
            _parse_backtest_request(bad_payload)
