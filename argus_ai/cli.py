import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from argus_ai.backtest import (
    AdversarialBacktestEngine,
    compute_passport_digest,
    generate_multi_regime_dataset,
    replay_passport,
)
from argus_ai.config.settings import CONFIG
from argus_ai.core.adaptive_weights import AdaptiveWeightManager
from argus_ai.core.alpaca_client import AlpacaClient
from argus_ai.core.kill_switch import EmergencyKillSwitch
from argus_ai.core.streamer import MarketStreamListener
from argus_ai.http_server import serve
from argus_ai.mcp_server import ArgusMCPServer
from argus_ai.scanner import OpportunityScanner

logger = logging.getLogger("argus.cli")

# Configure Windows console encoding for UTF-8 safety
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError, ValueError) as exc:
        logger.debug("Could not reconfigure stdio encoding: %s", exc)

console = Console(force_terminal=True, highlight=False)


def print_banner():
    banner = """
================================================================================
    ARGUS AI: Institutional Multi-Agent Autonomous Trading Desk
    "An AI Trading Desk that can disagree with itself"
================================================================================
    """
    console.print(f"[bold cyan]{banner}[/bold cyan]")


def display_passport_rich(passport):
    status_colors = {
        "APPROVED": "bold green",
        "DOWNSIZED": "bold yellow",
        "REJECTED": "bold red",
        "VETOED": "bold magenta",
    }
    color = status_colors.get(passport.consensus.status, "bold white")
    
    # 1. Main Header
    console.print(Panel(
        f"[bold white]Asset:[/bold white] [cyan]{passport.ticker}[/cyan] | "
        f"[bold white]Direction:[/bold white] [{'green' if passport.direction == 'LONG' else 'red'}]{passport.direction}[/{'green' if passport.direction == 'LONG' else 'red'}] | "
        f"[bold white]Price:[/bold white] ${passport.current_price:.2f} | "
        f"[bold white]Consensus:[/bold white] [{color}]{passport.consensus.status}[/{color}] | "
        f"[bold white]Execution:[/bold white] [bold yellow]{passport.execution_status}[/bold yellow]\n"
        f"[dim]ID: {passport.passport_id} | Timestamp: {passport.timestamp}[/dim]",
        title=f"TRADE PASSPORT: {passport.ticker}",
        border_style=color
    ))

    # 2. Committee Votes Table
    table = Table(title="Investment Committee Scorecard", header_style="bold magenta")
    table.add_column("Agent", style="cyan")
    table.add_column("Recommendation", justify="center")
    table.add_column("Score", justify="right")
    table.add_column("Confidence", justify="right")
    table.add_column("Key Thesis / Concerns", style="dim")

    for v in passport.agent_votes:
        rec_color = "green" if v.recommendation == "BUY" else ("red" if v.recommendation == "SELL" else "yellow")
        table.add_row(
            v.agent_name,
            f"[{rec_color}]{v.recommendation}[/{rec_color}]",
            f"{v.score:+.1f}",
            f"{v.confidence * 100:.0f}%",
            f"{'; '.join(v.key_arguments[:1])} (Concerns: {'; '.join(v.concerns_raised[:1]) if v.concerns_raised else 'None'})"
        )
    console.print(table)

    # 3. Devil's Advocate Stress Test Panel
    da = passport.devils_advocate
    da_style = "bold red" if da.veto_recommended else ("yellow" if da.disqualification_risk_pct > 40 else "green")
    traps_text = "\n".join([f"  * {t}" for t in da.identified_traps])
    console.print(Panel(
        f"[bold red]Adversarial Thesis:[/bold red] {da.adversarial_thesis}\n\n"
        f"[bold yellow]Identified Traps:[/bold yellow]\n{traps_text}\n\n"
        f"[bold white]Disqualification Risk:[/bold white] [{da_style}]{da.disqualification_risk_pct:.1f}%[/{da_style}] | "
        f"[bold white]Veto Recommended:[/bold white] [{'bold red' if da.veto_recommended else 'bold green'}]"
        f"{'YES [VETO]' if da.veto_recommended else 'NO [PASS]'}[/{'bold red' if da.veto_recommended else 'bold green'}] | "
        f"[bold white]Contingency:[/bold white] {da.recommended_contingency}",
        title="Devil's Advocate Dissent & Stress-Test (USP)",
        border_style=da_style
    ))

    # 4. Bracket & Risk Table
    risk_tbl = Table(title="Alpaca Bracket & Risk Budgeting", header_style="bold blue")
    risk_tbl.add_column("Target Entry", justify="center")
    risk_tbl.add_column("Stop Loss (1.5x ATR)", justify="center", style="red")
    risk_tbl.add_column("Take Profit (Min 2:1)", justify="center", style="green")
    risk_tbl.add_column("Position Shares", justify="center")
    risk_tbl.add_column("Allocated USD", justify="center")
    risk_tbl.add_column("Account Risk %", justify="center", style="yellow")

    risk_tbl.add_row(
        f"${passport.target_entry:.2f}",
        f"${passport.stop_loss:.2f}",
        f"${passport.take_profit:.2f}",
        f"{passport.shares_or_contracts:.1f}",
        f"${passport.position_size_usd:.2f}",
        f"{passport.account_risk_pct:.2f}%"
    )
    console.print(risk_tbl)
    console.print("\n" + "="*80 + "\n")


def display_backtest_rich(report):
    a = report.argus_committee
    b = report.naive_baseline
    c = report.cost_model

    tbl = Table(
        title=f"Walk-Forward Counterfactual Backtest: {report.ticker} ({report.total_bars} Bars, {report.evaluated_opportunities} Deliberations)",
        header_style="bold cyan",
    )
    tbl.add_column("Institutional Metric", style="bold white")
    tbl.add_column("ARGUS 7-Agent Committee", justify="right", style="bold green")
    tbl.add_column("Naive Quant Baseline", justify="right", style="yellow")
    tbl.add_column("ARGUS Structural Edge", justify="right", style="bold magenta")

    tbl.add_row("Ending Equity", f"${a.ending_equity:,.2f}", f"${b.ending_equity:,.2f}", f"${a.ending_equity - b.ending_equity:+,.2f}")
    tbl.add_row("Total Net Return (%)", f"{a.total_return_pct:+.2f}%", f"{b.total_return_pct:+.2f}%", f"{report.return_outperformance_pct:+.2f}%")
    tbl.add_row("Max Drawdown (%)", f"{a.max_drawdown_pct:.2f}%", f"{b.max_drawdown_pct:.2f}%", f"-{report.drawdown_reduction_pct:.2f}% DD")
    tbl.add_row("Sharpe Ratio (Ann.)", f"{a.sharpe_ratio:.2f}", f"{b.sharpe_ratio:.2f}", f"{report.sharpe_improvement:+.2f}")
    tbl.add_row("Sortino Ratio (Ann.)", f"{a.sortino_ratio:.2f}", f"{b.sortino_ratio:.2f}", f"{a.sortino_ratio - b.sortino_ratio:+.2f}")
    tbl.add_row("Win Rate (%)", f"{a.win_rate_pct:.1f}%", f"{b.win_rate_pct:.1f}%", f"{a.win_rate_pct - b.win_rate_pct:+.1f}%")
    tbl.add_row("Profit Factor", f"{a.profit_factor:.2f}", f"{b.profit_factor:.2f}", f"{a.profit_factor - b.profit_factor:+.2f}")
    tbl.add_row("Executed Trades", f"{a.total_trades}", f"{b.total_trades}", f"{a.total_trades - b.total_trades:+d}")
    tbl.add_row("Transaction Costs", f"${a.total_costs_usd:,.2f}", f"${b.total_costs_usd:,.2f}", f"${a.total_costs_usd - b.total_costs_usd:+,.2f}")
    console.print(tbl)

    console.print(Panel(
        f"[bold white]Devil's Advocate Veto Count:[/bold white] [bold magenta]{a.vetoed_trades_count}[/bold magenta] | "
        f"[bold white]50% Downsized Count:[/bold white] [bold yellow]{a.downsized_trades_count}[/bold yellow] | "
        f"[bold white]Counterfactual Losses Saved:[/bold white] [bold green]${a.veto_saved_loss_usd:,.2f}[/bold green] | "
        f"[bold white]Veto Precision:[/bold white] [bold cyan]{a.veto_accuracy_pct:.1f}%[/bold cyan]\n"
        f"[dim]Cost Model: Spread {c.spread_bps:.1f} bps | Commission {c.commission_bps:.1f} bps/side | Slippage {c.slippage_atr_fraction*100:.1f}% ATR | Mean Disagreement Entropy: {a.avg_disagreement_entropy:.4f}[/dim]",
        title="Adversarial Dissent & Veto Attribution",
        border_style="magenta",
    ))


def display_replay_rich(verification):
    border = "green" if verification.is_valid else "red"
    status_str = "[bold green]VALIDATED (ALL INVARIANTS PASSED)[/bold green]" if verification.is_valid else "[bold red]INVARIANT VIOLATION DETECTED[/bold red]"
    violations_str = "\n".join([f"  * [red]{v}[/red]" for v in verification.violations]) if verification.violations else "  * [green]None[/green]"
    console.print(Panel(
        f"[bold white]Passport ID:[/bold white] [cyan]{verification.passport_id}[/cyan] | "
        f"[bold white]Ticker:[/bold white] {verification.ticker} | "
        f"[bold white]Consensus:[/bold white] {verification.consensus_status}\n"
        f"[bold white]Verification Status:[/bold white] {status_str}\n"
        f"[bold white]SHA-256 Digest:[/bold white] [dim]{verification.computed_digest}[/dim]\n"
        f"[bold white]Disagreement Entropy:[/bold white] {verification.disagreement_entropy:.4f}\n"
        f"[bold white]Invariants Checked:[/bold white] {', '.join(verification.invariants_checked)}\n"
        f"[bold white]Violations:[/bold white]\n{violations_str}",
        title="Trade Passport Deterministic Replay & Audit Verification",
        border_style=border,
    ))


def main(argv: list[str] | None = None) -> None:
    print_banner()
    parser = argparse.ArgumentParser(description="ARGUS AI Autonomous Trading Desk CLI")
    parser.add_argument(
        "command",
        choices=[
            "scan",
            "single",
            "adversarial-demo",
            "kill-switch",
            "mcp",
            "stream",
            "weights",
            "backtest",
            "replay",
            "serve",
        ],
        nargs="?",
        default="scan",
    )
    parser.add_argument("--ticker", type=str, default="NVDA", help="Ticker to evaluate for 'single' or 'backtest' mode")
    parser.add_argument("--regime", type=str, default="BULLISH", help="Market regime (BULLISH, BEARISH, CHOPPY)")
    parser.add_argument("--vix", type=float, default=17.5, help="Simulated VIX index level")
    parser.add_argument("--bars", type=int, default=240, help="Number of historical bars for 'backtest' mode")
    parser.add_argument("--report-out", type=str, default="", help="Optional markdown path to write backtest report")
    parser.add_argument("--passport", type=str, default="", help="Path to TradePassport JSON file for 'replay' mode")
    parser.add_argument("--trigger-kill", action="store_true", help="Trigger manual kill switch")
    parser.add_argument("--reset-kill", action="store_true", help="Reset manual kill switch")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind host for 'serve' HTTP workbench")
    parser.add_argument("--port", type=int, default=8000, help="Bind port for 'serve' HTTP workbench")

    args = parser.parse_args(argv)

    if args.command == "serve":
        console.print(
            Panel(
                f"[bold green]HTTP Workbench listening on[/bold green] [cyan]http://{args.host}:{args.port}[/cyan]\n"
                "[dim]Endpoints: / (Portal UI / Info), /health, /healthz, /readyz, /info, /metrics, "
                "/api/committee-spec, /api/backtest, /api/verify-passport[/dim]",
                title="ARGUS AI HTTP Workbench & Observability Server",
                border_style="cyan",
            )
        )
        serve(host=args.host, port=args.port)
        return

    alpaca = AlpacaClient()
    kill_switch = EmergencyKillSwitch(alpaca)
    scanner = OpportunityScanner(alpaca, kill_switch)

    acc = alpaca.get_account_summary()
    console.print(Panel(
        f"[bold white]Engine Mode:[/bold white] [bold green]{acc['mode']}[/bold green] | "
        f"[bold white]Account Equity:[/bold white] [cyan]${acc['equity']:,.2f}[/cyan] | "
        f"[bold white]Buying Power:[/bold white] [cyan]${acc['buying_power']:,.2f}[/cyan] | "
        f"[bold white]Daily PnL:[/bold white] [green]+${acc['daily_pnl']:,.2f} ({acc['daily_pnl_pct']:+.2f}%)[/green]",
        title="Alpaca Portfolio Status",
        border_style="cyan"
    ))

    if args.command == "scan":
        console.print(f"[bold yellow]Scanning ARGUS Watchlist: {', '.join(CONFIG.watchlist)}[/bold yellow]...\n")
        passports = scanner.scan_watchlist()
        for p in passports:
            display_passport_rich(p)

    elif args.command == "single":
        console.print(f"[bold yellow]Deliberating single opportunity: {args.ticker} (Regime: {args.regime}, VIX: {args.vix})[/bold yellow]...\n")
        passport = scanner.scan_asset(args.ticker, custom_regime=args.regime, custom_vix=args.vix)
        display_passport_rich(passport)

    elif args.command == "adversarial-demo":
        console.print("[bold magenta]Running Adversarial Demonstration (Testing Self-Refusal & Devil's Advocate Veto)[/bold magenta]...\n")
        console.print("[dim]Injecting high VIX spike (VIX = 31.0) and choppy market conditions to demonstrate autonomous veto...[/dim]\n")
        passport = scanner.scan_asset("TSLA", custom_regime="CHOPPY", custom_vix=31.0)
        display_passport_rich(passport)

    elif args.command == "kill-switch":
        if args.trigger_kill:
            kill_switch.trigger("Manual emergency halt invoked by operator via CLI.", circuit_type="MANUAL")
            console.print("[bold red]EMERGENCY KILL SWITCH ENGAGED! All open orders cancelled on Alpaca.[/bold red]")
        elif args.reset_kill:
            kill_switch.reset_override("Operator confirmed risk parameters normalized.")
            console.print("[bold green]KILL SWITCH RESET. Autonomous trading resumed.[/bold green]")
        else:
            status = kill_switch.inspect_portfolio_health()
            console.print(f"Kill Switch Status: {'ACTIVE (FROZEN)' if status.is_active else 'STANDBY (NORMAL)'}")

    elif args.command == "mcp":
        console.print("[bold cyan]Starting ARGUS AI Model Context Protocol (MCP) Server on stdio...[/bold cyan]")
        server = ArgusMCPServer()
        server.run_stdio()

    elif args.command == "stream":
        console.print("[bold cyan]Starting 24/7 WebSocket Real-Time Stream Listener...[/bold cyan]")
        listener = MarketStreamListener(on_trade_passport_callback=display_passport_rich)
        try:
            asyncio.run(listener.start_streaming_loop(max_ticks=5))
        except KeyboardInterrupt:
            listener.stop()

    elif args.command == "weights":
        mgr = AdaptiveWeightManager()
        console.print(Panel(
            "\n".join([f"• [cyan]{agent}:[/cyan] [bold green]{w*100:.1f}%[/bold green]" for agent, w in mgr.weights.items()]),
            title="🧠 Adaptive Bayesian Committee Weights (Online Learning)",
            border_style="magenta"
        ))

    elif args.command == "backtest":
        console.print(
            f"[bold cyan]Running Deterministic Multi-Regime Walk-Forward Backtest on {args.ticker} ({args.bars} bars)...[/bold cyan]\n"
        )
        df = generate_multi_regime_dataset(ticker=args.ticker, n_bars=args.bars, seed=42)
        engine = AdversarialBacktestEngine()
        report = engine.run_backtest(df, ticker=args.ticker)
        display_backtest_rich(report)
        if args.report_out:
            out_path = Path(args.report_out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(AdversarialBacktestEngine.to_markdown_report(report), encoding="utf-8")
            console.print(f"[bold green]Backtest verification report written to {out_path}[/bold green]")

    elif args.command == "replay":
        if args.passport:
            passport_source = Path(args.passport)
            verification = replay_passport(passport_source)
        else:
            passport = scanner.scan_asset(args.ticker, custom_regime=args.regime, custom_vix=args.vix)
            digest = compute_passport_digest(passport)
            verification = replay_passport(passport.model_dump_json(), expected_digest=digest)
        display_replay_rich(verification)


if __name__ == "__main__":
    main()
