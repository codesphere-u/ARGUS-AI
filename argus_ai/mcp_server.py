import sys
import json
import logging
import asyncio
from typing import Dict, Any, List, Optional
import pandas as pd

from argus_ai.committee import CommitteeOrchestrator
from argus_ai.core.models import MarketContext
from argus_ai.core.alpaca_client import AlpacaClient
from argus_ai.core.kill_switch import EmergencyKillSwitch
from argus_ai.agents.audit_agent import AuditAgent

logger = logging.getLogger("argus.mcp_server")


class ArgusMCPServer:
    """
    Official Model Context Protocol (MCP) Server for ARGUS AI & Alpaca Trading.
    Enables Claude Desktop, Cursor, Gemini, and AI Agents to execute standardized
    tools for market evaluation, Trade Passport inspection, kill-switch control,
    and Alpaca bracket order dispatching.
    """

    TOOLS = [
        {
            "name": "argus_convene_committee",
            "description": "Convenes the ARGUS AI 7-agent Investment Committee to analyze an asset, stress-test it with Devil's Advocate, and produce a Trade Passport.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol (e.g., NVDA, AAPL, BTC/USD)"},
                    "current_price": {"type": "number", "description": "Current market price of asset"},
                    "market_regime": {"type": "string", "enum": ["BULLISH", "BEARISH", "CHOPPY", "VOLATILE"], "default": "BULLISH"},
                    "vix_level": {"type": "number", "default": 18.0}
                },
                "required": ["ticker", "current_price"]
            }
        },
        {
            "name": "argus_get_trade_passport",
            "description": "Retrieves the immutable cryptographic Trade Passport for a trade by its ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "passport_id": {"type": "string", "description": "Passport identifier (e.g. PASSPORT-20260828-NVDA01)"}
                },
                "required": ["passport_id"]
            }
        },
        {
            "name": "argus_emergency_kill_switch",
            "description": "Emergency institutional circuit breaker. Freezes automated trading and cancels all open Alpaca orders.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["TRIP", "STATUS", "RESET"]},
                    "justification": {"type": "string", "description": "Audit reason for tripping or resetting"}
                },
                "required": ["action"]
            }
        },
        {
            "name": "alpaca_get_portfolio_telemetry",
            "description": "Fetches real-time equity, buying power, daily PnL, and open positions from Alpaca.",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        },
        {
            "name": "alpaca_dispatch_bracket_order",
            "description": "Routes a pre-verified bracket order (entry, stop-loss, take-profit) directly to Alpaca.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "qty": {"type": "number"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "take_profit_price": {"type": "number"},
                    "stop_loss_price": {"type": "number"},
                    "client_order_id": {"type": "string"}
                },
                "required": ["symbol", "qty", "side", "take_profit_price", "stop_loss_price"]
            }
        }
    ]

    def __init__(self):
        self.orchestrator = CommitteeOrchestrator()
        self.alpaca = AlpacaClient()
        self.kill_switch = EmergencyKillSwitch(alpaca_client=self.alpaca)
        self.audit = AuditAgent()

    def handle_tool_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch MCP tool calls to underlying ARGUS AI modules."""
        if tool_name == "argus_convene_committee":
            ticker = arguments.get("ticker", "NVDA")
            price = float(arguments.get("current_price", 150.0))
            regime = arguments.get("market_regime", "BULLISH")
            vix = float(arguments.get("vix_level", 18.0))

            ctx = MarketContext(
                ticker=ticker,
                current_price=price,
                market_regime=regime,
                vix_level=vix,
                rsi_14=58.0,
                atr_14=price * 0.02
            )

            # Generate synthetic historical bars
            dates = pd.date_range("2026-01-01", periods=50, freq="h")
            close = pd.Series([price * (1.0 + 0.001 * i) for i in range(50)], index=dates)
            bars_df = pd.DataFrame({
                "open": close - 0.2, "high": close + 0.5, "low": close - 0.5,
                "close": close, "volume": [25000.0]*50
            }, index=dates)

            passport = self.orchestrator.evaluate_opportunity(ctx, bars_df)
            self.audit.record_passport(passport)
            return passport.model_dump()

        elif tool_name == "argus_get_trade_passport":
            pid = arguments.get("passport_id")
            for p in self.audit.passports:
                if p.passport_id == pid:
                    return p.model_dump()
            return {"status": "NOT_FOUND", "passport_id": pid}

        elif tool_name == "argus_emergency_kill_switch":
            action = arguments.get("action", "STATUS").upper()
            justification = arguments.get("justification", "Operator action via MCP")

            if action == "TRIP":
                self.kill_switch.trigger(reason=justification, circuit_type="MANUAL")
                return {"status": "KILL_SWITCH_ENGAGED", "state": self.kill_switch.state.model_dump()}
            elif action == "RESET":
                self.kill_switch.reset_override(manual_justification=justification)
                return {"status": "KILL_SWITCH_RESET", "state": self.kill_switch.state.model_dump()}
            else:
                state = self.kill_switch.inspect_portfolio_health()
                return {"status": "HEALTH_INSPECTED", "state": state.model_dump()}

        elif tool_name == "alpaca_get_portfolio_telemetry":
            acc = self.alpaca.get_account_summary()
            positions = self.alpaca.get_open_positions()
            return {"account": acc, "open_positions": positions}

        elif tool_name == "alpaca_dispatch_bracket_order":
            if self.kill_switch.state.is_active:
                return {"error": "Execution blocked: Emergency Kill Switch is ACTIVE", "success": False}
            res = self.alpaca.place_bracket_order(**arguments)
            return {"dispatch_result": res, "success": True}

        else:
            return {"error": f"Unknown MCP tool: {tool_name}"}

    def process_json_rpc(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Process incoming MCP JSON-RPC message."""
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params", {})

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": self.TOOLS}}
        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            try:
                res = self.handle_tool_call(tool_name, tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}]}
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": str(e)}
                }
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}
            }

    def run_stdio(self):
        """Run MCP server in Standard I/O mode for Claude Desktop / Cursor."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                resp = self.process_json_rpc(req)
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
            except Exception as e:
                err_resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {e}"}}
                sys.stdout.write(json.dumps(err_resp) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    server = ArgusMCPServer()
    server.run_stdio()
