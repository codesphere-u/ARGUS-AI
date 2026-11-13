"""Production verification tests for deterministic use_llm=False mode and the HTTP Workbench."""

from __future__ import annotations

import http.client
import inspect
import json
import threading
import uuid
from collections.abc import Generator
from typing import Any

import pytest

import argus_ai.backtest as backtest_mod
from argus_ai.agents.audit_agent import AuditAgent
from argus_ai.agents.base_agent import BaseAgent
from argus_ai.backtest import (
    AdversarialBacktestEngine,
    BacktestEngine,
    compute_passport_digest,
    generate_multi_regime_dataset,
)
from argus_ai.cli import main as cli_main
from argus_ai.committee import CommitteeOrchestrator
from argus_ai.config.settings import CONFIG
from argus_ai.core.models import MarketContext
from argus_ai.http_server import ServerState, create_server


def _http_call(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        raw = resp.read()
        lower_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, lower_headers, raw
    finally:
        conn.close()


@pytest.fixture
def workbench_server() -> Generator[tuple[int, ServerState], None, None]:
    state = ServerState()
    server = create_server(host="127.0.0.1", port=0, max_body_bytes=4096, state=state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_backtest_module_uses_native_deterministic_mode_without_unittest_mock() -> None:
    source = inspect.getsource(backtest_mod)
    assert "unittest.mock" not in source
    assert BacktestEngine is AdversarialBacktestEngine

    engine = BacktestEngine(warmup_bars=40, eval_stride=8, holding_horizon=8, use_llm=False)
    assert engine.use_llm is False
    assert engine.committee.use_llm is False

    bars_df = generate_multi_regime_dataset(ticker="NVDA", n_bars=110, seed=42)
    report_a = engine.run_backtest(bars_df, ticker="NVDA")
    report_b = engine.run_backtest(bars_df, ticker="NVDA")

    assert report_a.argus_committee == report_b.argus_committee
    assert report_a.naive_baseline == report_b.naive_baseline
    assert report_a.evaluated_opportunities >= len(report_a.trade_records) > 0
    assert len(report_a.trade_records) == len(report_b.trade_records)


def test_use_llm_false_never_initializes_gemini_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(CONFIG.committee, "gemini_api_key", "fake-test-key-never-called")

    agent = BaseAgent("UnitTestAgent", use_llm=False)
    assert agent._gemini_client is None
    assert agent.query_llm_json("system", "user") is None

    audit = AuditAgent(log_dir=str(tmp_path), use_llm=False)
    assert audit._gemini_client is None

    orchestrator = CommitteeOrchestrator(use_llm=False)
    for member in (
        orchestrator.quant,
        orchestrator.news,
        orchestrator.macro,
        orchestrator.options,
        orchestrator.devils_advocate,
    ):
        assert member.use_llm is False
        assert member._gemini_client is None

    bars_df = generate_multi_regime_dataset(ticker="AAPL", n_bars=80, seed=9)
    latest = bars_df.iloc[-1]
    ctx = MarketContext(
        ticker="AAPL",
        current_price=float(latest["close"]),
        high_24h=float(latest["high"]),
        low_24h=float(latest["low"]),
        volume_24h=float(latest["volume"]),
        rsi_14=56.0,
        atr_14=2.4,
        market_regime="BULLISH",
        vix_level=16.5,
        recent_news=["Apple expands AI inference silicon pipeline"],
    )
    passport = orchestrator.evaluate_opportunity(ctx, bars_df, account_equity=100_000.0)
    assert passport.ticker == "AAPL"
    assert len(passport.agent_votes) == 4


def test_root_html_and_json_content_negotiation(workbench_server: tuple[int, ServerState]) -> None:
    port, _ = workbench_server

    status_html, hdrs_html, body_html = _http_call(
        port, "GET", "/", headers={"Accept": "text/html,application/xhtml+xml"}
    )
    assert status_html == 200
    assert "text/html" in hdrs_html["content-type"]
    assert hdrs_html["x-content-type-options"] == "nosniff"
    assert hdrs_html["x-frame-options"] == "SAMEORIGIN"
    assert hdrs_html["referrer-policy"] == "strict-origin-when-cross-origin"
    assert hdrs_html["cache-control"] == "no-cache"
    html_text = body_html.decode("utf-8")
    assert "ARGUS AI" in html_text
    assert "/api/backtest" in html_text
    assert "/api/verify-passport" in html_text

    status_json, hdrs_json, body_json = _http_call(port, "GET", "/", headers={"Accept": "application/json"})
    assert status_json == 200
    assert "application/json" in hdrs_json["content-type"]
    payload = json.loads(body_json)
    assert payload["service"] == "argus-ai"
    assert payload["deterministic_mode"] is True
    assert "/metrics" in payload["endpoints"]


@pytest.mark.parametrize("path", ["/health", "/healthz", "/readyz", "/info", "/api/committee-spec"])
def test_probes_and_head_requests(workbench_server: tuple[int, ServerState], path: str) -> None:
    port, _ = workbench_server

    status_get, hdrs_get, body_get = _http_call(port, "GET", path)
    assert status_get == 200
    assert "application/json" in hdrs_get["content-type"]
    assert hdrs_get["cache-control"] == "no-store"
    assert hdrs_get["x-content-type-options"] == "nosniff"
    data = json.loads(body_get)
    assert data["service"] == "argus-ai"

    status_head, hdrs_head, body_head = _http_call(port, "HEAD", path)
    assert status_head == 200
    assert body_head == b""
    assert int(hdrs_head["content-length"]) > 0


def test_metrics_prometheus_json_and_head(workbench_server: tuple[int, ServerState]) -> None:
    port, _ = workbench_server

    status_prom, hdrs_prom, body_prom = _http_call(port, "GET", "/metrics")
    assert status_prom == 200
    assert "text/plain" in hdrs_prom["content-type"]
    assert hdrs_prom["cache-control"] == "no-store"
    prom_text = body_prom.decode("utf-8")
    assert "argus_info{" in prom_text
    assert "argus_http_requests_total " in prom_text
    assert "argus_backtests_run_total " in prom_text

    status_head, hdrs_head, body_head = _http_call(port, "HEAD", "/metrics")
    assert status_head == 200
    assert body_head == b""
    assert int(hdrs_head["content-length"]) == len(body_prom)

    status_json, hdrs_json, body_json = _http_call(
        port, "GET", "/metrics", headers={"Accept": "application/json"}
    )
    assert status_json == 200
    assert "application/json" in hdrs_json["content-type"]
    metrics_json = json.loads(body_json)
    assert metrics_json["service"] == "argus-ai"
    assert metrics_json["requests_total"] >= 2

    status_q, _, body_q = _http_call(port, "GET", "/metrics?format=json")
    assert status_q == 200
    q_json = json.loads(body_q)
    assert q_json["service"] == "argus-ai"
    assert q_json["requests_total"] >= 3


def test_committee_spec_and_backtest_endpoint(workbench_server: tuple[int, ServerState]) -> None:
    port, _ = workbench_server

    status_spec, hdrs_spec, body_spec = _http_call(port, "GET", "/api/committee-spec")
    assert status_spec == 200
    assert hdrs_spec["cache-control"] == "no-store"
    spec = json.loads(body_spec)
    assert len(spec["agents"]) == 7
    assert spec["deterministic_mode"] is True

    req_body = json.dumps({"ticker": "MSFT", "bars": 120, "seed": 11, "eval_stride": 6}).encode("utf-8")
    status_bt, hdrs_bt, body_bt = _http_call(
        port,
        "POST",
        "/api/backtest",
        body=req_body,
        headers={"Content-Type": "application/json"},
    )
    assert status_bt == 200
    assert hdrs_bt["cache-control"] == "no-store"
    bt_data = json.loads(body_bt)
    assert bt_data["deterministic_mode"] is True
    assert bt_data["ticker"] == "MSFT"
    assert bt_data["total_bars"] == 120
    assert bt_data["argus_committee"]["strategy_name"]
    assert bt_data["naive_baseline"]["strategy_name"]

    # Empty POST body should cleanly fall back to deterministic defaults
    status_empty, _, body_empty = _http_call(port, "POST", "/api/backtest", body=b"")
    assert status_empty == 200
    assert json.loads(body_empty)["ticker"] == "NVDA"

    _, _, metrics_raw = _http_call(port, "GET", "/metrics?format=json")
    metrics = json.loads(metrics_raw)
    assert metrics["backtests_run_total"] == 2
    assert metrics["last_backtest"]["ticker"] == "NVDA"


def test_verify_passport_valid_tampered_and_invalid_schema(
    workbench_server: tuple[int, ServerState],
) -> None:
    port, _ = workbench_server

    orchestrator = CommitteeOrchestrator(use_llm=False)
    bars_df = generate_multi_regime_dataset(ticker="NVDA", n_bars=90, seed=5)
    latest = bars_df.iloc[-1]
    ctx = MarketContext(
        ticker="NVDA",
        current_price=float(latest["close"]),
        high_24h=float(latest["high"]),
        low_24h=float(latest["low"]),
        volume_24h=float(latest["volume"]),
        rsi_14=58.0,
        atr_14=3.0,
        market_regime="BULLISH",
        vix_level=15.0,
        recent_news=["NVIDIA posts record AI datacenter demand"],
    )
    passport = orchestrator.evaluate_opportunity(ctx, bars_df, account_equity=100_000.0)
    digest = compute_passport_digest(passport)

    valid_payload = json.dumps(
        {"passport": passport.model_dump(), "expected_digest": digest}
    ).encode("utf-8")
    status_ok, _, raw_ok = _http_call(
        port,
        "POST",
        "/api/verify-passport",
        body=valid_payload,
        headers={"Content-Type": "application/json"},
    )
    assert status_ok == 200
    res_ok = json.loads(raw_ok)
    assert res_ok["is_valid"] is True
    assert res_ok["expected_digest_matched"] is True
    assert res_ok["computed_digest"] == digest

    tampered_payload = json.dumps(
        {"passport": passport.model_dump(), "expected_digest": "0" * 64}
    ).encode("utf-8")
    status_bad, _, raw_bad = _http_call(
        port,
        "POST",
        "/api/verify-passport",
        body=tampered_payload,
        headers={"Content-Type": "application/json"},
    )
    assert status_bad == 200
    res_bad = json.loads(raw_bad)
    assert res_bad["is_valid"] is False
    assert res_bad["expected_digest_matched"] is False

    invalid_schema = json.dumps({"passport": {"ticker": "NVDA"}}).encode("utf-8")
    status_422, _, raw_422 = _http_call(
        port,
        "POST",
        "/api/verify-passport",
        body=invalid_schema,
        headers={"Content-Type": "application/json"},
    )
    assert status_422 == 422
    err_422 = json.loads(raw_422)
    assert err_422["error"] == "validation_error"
    assert "Traceback" not in err_422["message"]


def test_http_security_hardening_request_id_cors_413_and_errors(
    workbench_server: tuple[int, ServerState],
) -> None:
    port, _ = workbench_server

    # Valid X-Request-ID is echoed verbatim
    status_id, hdrs_id, _ = _http_call(
        port, "GET", "/healthz", headers={"X-Request-ID": "req-argus-check_01"}
    )
    assert status_id == 200
    assert hdrs_id["x-request-id"] == "req-argus-check_01"

    # Invalid X-Request-ID is replaced with a sanitized UUID
    unsafe_id = "bad request id !@#$"
    status_rep, hdrs_rep, _ = _http_call(
        port, "GET", "/healthz", headers={"X-Request-ID": unsafe_id}
    )
    assert status_rep == 200
    assert hdrs_rep["x-request-id"] != unsafe_id
    parsed_uuid = uuid.UUID(hdrs_rep["x-request-id"])
    assert parsed_uuid.version == 4

    # OPTIONS CORS preflight
    status_opt, hdrs_opt, body_opt = _http_call(port, "OPTIONS", "/api/backtest")
    assert status_opt == 204
    assert body_opt == b""
    assert hdrs_opt["access-control-allow-origin"] == "*"
    assert "POST" in hdrs_opt["access-control-allow-methods"]

    # 413 body limit guard (server max_body_bytes=4096)
    oversized = json.dumps({"ticker": "NVDA", "pad": "x" * 5000}).encode("utf-8")
    status_413, _, body_413 = _http_call(
        port,
        "POST",
        "/api/backtest",
        body=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert status_413 == 413
    assert json.loads(body_413)["error"] == "payload_too_large"

    # 400 malformed JSON & 422 semantic validation error
    status_400, _, body_400 = _http_call(
        port, "POST", "/api/backtest", body=b"{not-json", headers={"Content-Type": "application/json"}
    )
    assert status_400 == 400
    assert json.loads(body_400)["error"] == "bad_request"

    status_422, _, body_422 = _http_call(
        port,
        "POST",
        "/api/backtest",
        body=json.dumps({"ticker": "NVDA", "bars": 12}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert status_422 == 422
    assert json.loads(body_422)["error"] == "validation_error"

    # 404 unknown path & 405 wrong method
    status_404, _, body_404 = _http_call(port, "GET", "/does-not-exist")
    assert status_404 == 404
    assert json.loads(body_404)["error"] == "not_found"

    status_405_get, hdrs_405, _ = _http_call(port, "GET", "/api/backtest")
    assert status_405_get == 405
    assert "POST" in hdrs_405["allow"]

    status_405_put, _, _ = _http_call(port, "PUT", "/healthz")
    assert status_405_put == 405


def test_cli_serve_subcommand_dispatches_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_serve(*, host: str, port: int) -> None:
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("argus_ai.cli.serve", fake_serve)
    cli_main(["serve", "--host", "127.0.0.1", "--port", "9123"])
    assert captured == {"host": "127.0.0.1", "port": 9123}
