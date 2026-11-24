"""Tests for the ARGUS AI Headline Arena calibration integration."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any, Literal

import pytest

from argus_ai.backtest import compute_passport_digest
from argus_ai.cli import main as cli_main
from argus_ai.config.settings import HeadlineArenaSettings
from argus_ai.core.models import (
    AgentVote,
    ConsensusDecision,
    DevilsAdvocateReview,
    TradePassport,
)
from argus_ai.integrations.headline_arena import (
    HeadlineArenaChallenge,
    HeadlineArenaClient,
    HeadlineArenaDesk,
    HeadlineArenaError,
    TernaryForecast,
    build_model_attribution_fields,
    challenge_to_market_inputs,
    compute_calibration_summary,
    generate_offline_macro_challenges,
    normalize_origin,
    passport_to_ternary_forecast,
)


def _make_passport(
    *,
    ticker: str = "GC",
    direction: Literal["LONG", "SHORT"] = "LONG",
    status: Literal["APPROVED", "DOWNSIZED", "REJECTED", "VETOED"] = "APPROVED",
    composite_score: float = 65.0,
    overall_confidence: float = 0.80,
    da_risk: float = 18.0,
    veto: bool = False,
) -> TradePassport:
    return TradePassport(
        ticker=ticker,
        asset_class="MACRO_FUTURES",
        direction=direction,
        current_price=2550.0,
        target_entry=2550.0,
        stop_loss=2520.0 if direction == "LONG" else 2580.0,
        take_profit=2610.0 if direction == "LONG" else 2490.0,
        risk_reward_ratio=2.0,
        shares_or_contracts=0.0 if status in ("VETOED", "REJECTED") else 10.0,
        position_size_usd=0.0 if status in ("VETOED", "REJECTED") else 25500.0,
        account_risk_usd=0.0 if status in ("VETOED", "REJECTED") else 1000.0,
        account_risk_pct=0.0 if status in ("VETOED", "REJECTED") else 1.0,
        consensus=ConsensusDecision(
            status=status,
            overall_confidence=overall_confidence,
            composite_score=composite_score,
            downsize_factor=0.0 if status in ("VETOED", "REJECTED") else (0.5 if status == "DOWNSIZED" else 1.0),
            primary_catalyst="EMA alignment",
            main_opposition="Exhaustion risk" if veto else "None",
            executive_summary=f"Status {status}",
            unanimous=status == "APPROVED",
        ),
        agent_votes=[
            AgentVote(
                agent_name="Quant Agent",
                recommendation="BUY" if direction == "LONG" else "SELL",
                confidence=overall_confidence,
                score=composite_score,
                key_arguments=["Momentum trend"],
            )
        ],
        devils_advocate=DevilsAdvocateReview(
            adversarial_thesis="Overbought spike" if veto else "Low friction",
            identified_traps=["Overbought RSI + VIX spike"] if veto else [],
            disqualification_risk_pct=da_risk,
            veto_recommended=veto,
            recommended_contingency="Stand aside" if veto else "Standard ATR stop",
            severity_level="CRITICAL" if veto else "LOW",
        ),
    )


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any] | list[Any], status: int = 200) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> Any:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_normalize_origin_and_model_attribution_validation() -> None:
    assert normalize_origin("https://headlinearena.com/api/v1/") == "https://headlinearena.com"
    assert normalize_origin("http://127.0.0.1:8080/api/v1") == "http://127.0.0.1:8080"
    assert normalize_origin("http://localhost:8000") == "http://localhost:8000"

    with pytest.raises(HeadlineArenaError, match="HTTPS is required"):
        normalize_origin("http://headlinearena.com")
    with pytest.raises(HeadlineArenaError, match="Invalid Headline Arena base URL"):
        normalize_origin("https://")

    assert build_model_attribution_fields(None, None, None) == {}
    assert build_model_attribution_fields("Google", "gemini-2.5-flash", "2026-09") == {
        "model_provider": "Google",
        "model_name": "gemini-2.5-flash",
        "model_version": "2026-09",
    }
    with pytest.raises(HeadlineArenaError, match="together"):
        build_model_attribution_fields("Google", None, None)
    with pytest.raises(HeadlineArenaError, match="requires both"):
        build_model_attribution_fields(None, None, "v1")
    with pytest.raises(HeadlineArenaError, match="at most 64"):
        build_model_attribution_fields("x" * 65, "model", None)


def test_passport_to_ternary_forecast_calibration_across_verdicts() -> None:
    approved = _make_passport(status="APPROVED", direction="LONG", composite_score=72.0, overall_confidence=0.85, da_risk=15.0)
    downsized = _make_passport(status="DOWNSIZED", direction="LONG", composite_score=55.0, overall_confidence=0.70, da_risk=55.0)
    rejected = _make_passport(status="REJECTED", direction="LONG", composite_score=12.0, overall_confidence=0.45, da_risk=25.0)
    vetoed = _make_passport(status="VETOED", direction="LONG", composite_score=62.0, overall_confidence=0.75, da_risk=85.0, veto=True)

    narrow_ch = HeadlineArenaChallenge(
        challenge_id="ch-gc-1",
        asset="GC",
        open_price=2550.0,
        dead_zone_pct=0.15,
        resolution_criteria="Resolves bullish > +0.15%, bearish < -0.15%, else neutral.",
    )
    wide_ch = HeadlineArenaChallenge(
        challenge_id="ch-cl-1",
        asset="CL",
        open_price=75.0,
        dead_zone_pct=0.60,
    )

    fc_app = passport_to_ternary_forecast(approved, narrow_ch)
    fc_down = passport_to_ternary_forecast(downsized, narrow_ch)
    fc_rej = passport_to_ternary_forecast(rejected, narrow_ch)
    fc_veto = passport_to_ternary_forecast(vetoed, narrow_ch)
    fc_wide = passport_to_ternary_forecast(approved, wide_ch)

    for fc in (fc_app, fc_down, fc_rej, fc_veto, fc_wide):
        assert abs(sum(fc.probabilities.values()) - 1.0) <= 1e-6
        assert fc.confidence == fc.probabilities[fc.direction]

    assert fc_app.direction == "bullish"
    assert fc_app.probabilities["bullish"] > fc_down.probabilities["bullish"]
    assert fc_down.probabilities["neutral"] > fc_app.probabilities["neutral"]

    # Refusals map primary probability mass to neutral
    assert fc_rej.direction == "neutral"
    assert fc_veto.direction == "neutral"
    # Vetoed LONG allocates more tail risk to bearish (trap reversal) than bullish
    assert fc_veto.probabilities["bearish"] > fc_veto.probabilities["bullish"]

    # Wider dead-zone shifts additional mass to neutral
    assert fc_wide.probabilities["neutral"] > fc_app.probabilities["neutral"]

    # Cryptographic TradePassport traceability in forecast_context
    expected_digest = compute_passport_digest(approved)
    assert fc_app.passport_digest == expected_digest
    assert fc_app.forecast_context["schema_version"] == "1"
    assert fc_app.forecast_context["decision"] == "initial"
    assert fc_app.forecast_context["run_ref"] == approved.passport_id
    assert fc_app.forecast_context["evidence_refs"][0]["content_sha256"] == expected_digest


def test_ternary_forecast_simplex_validation_rejects_bad_vectors() -> None:
    base = passport_to_ternary_forecast(_make_passport())
    with pytest.raises(ValueError, match="sum to 1.0"):
        TernaryForecast(
            **{
                **base.model_dump(),
                "probabilities": {"bearish": 0.5, "neutral": 0.5, "bullish": 0.5},
            }
        )


def test_compute_calibration_summary_brier_and_refusals() -> None:
    empty = compute_calibration_summary([])
    assert empty.count == 0
    assert empty.brier_score == 0.0

    fc_bull = passport_to_ternary_forecast(_make_passport(status="APPROVED", direction="LONG", composite_score=75.0))
    fc_veto = passport_to_ternary_forecast(_make_passport(status="VETOED", direction="LONG", da_risk=85.0, veto=True))

    summary = compute_calibration_summary(
        [
            (fc_bull, "bullish"),
            (fc_veto, "neutral"),
        ]
    )
    assert summary.count == 2
    assert summary.directional_accuracy == 1.0
    assert summary.refusal_count == 1
    assert summary.refusal_calibration_rate == 1.0
    assert 0.0 < summary.brier_score < 0.35
    assert summary.headline_arena_avg_score > 65.0


def test_client_credentials_file_formats_auth_and_rest_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Single-agent credentials file format
    single_creds = tmp_path / "single.json"
    single_creds.write_text(
        json.dumps({"https://headlinearena.com": {"agent_id": "agt_single", "client_secret": "sec_1"}}),
        encoding="utf-8",
    )
    c1 = HeadlineArenaClient(
        HeadlineArenaSettings(
            base_url="https://headlinearena.com",
            agent_id="",
            client_secret="",
            credentials_path=str(single_creds),
        )
    )
    assert c1.has_credentials is True
    assert c1.agent_id == "agt_single"

    # 2. Multi-agent credentials file format
    multi_creds = tmp_path / "multi.json"
    multi_creds.write_text(
        json.dumps(
            {
                "https://headlinearena.com": {
                    "default": "agt_two",
                    "agents": {
                        "agt_one": {"agent_id": "agt_one", "client_secret": "sec_one"},
                        "agt_two": {"agent_id": "agt_two", "client_secret": "sec_two"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    c2 = HeadlineArenaClient(
        HeadlineArenaSettings(
            base_url="https://headlinearena.com",
            agent_id="",
            client_secret="",
            credentials_path=str(multi_creds),
        )
    )
    assert c2.agent_id == "agt_two"

    # 3. Mocked HTTP transport across token, challenges, market context, submit, scorecard, results
    captured: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = []

    def fake_urlopen(req: Any, timeout: float = 20.0) -> _FakeHTTPResponse:
        url = req.full_url
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        captured.append((req.get_method(), url, dict(req.header_items()), body))

        if url.endswith("/agent/auth/token"):
            return _FakeHTTPResponse({"access_token": "tok_abc", "expires_in": 3600})
        if "/eval/challenges/active" in url:
            return _FakeHTTPResponse(
                {
                    "challenges": [
                        {
                            "challenge": {
                                "id": "gc-live-1",
                                "asset": "GC",
                                "status": "open",
                                "open_price": 2610.5,
                                "dead_zone_pct": 0.15,
                                "resolution_criteria": "Close vs open +/-0.15%",
                            },
                            "context": {"ohlc": {"bars": []}},
                        },
                        {
                            "challenge": {
                                "id": "cl-closed",
                                "asset": "CL",
                                "status": "resolved",
                                "open_price": 75.0,
                            }
                        },
                    ]
                }
            )
        if "/eval/context/GC" in url:
            return _FakeHTTPResponse({"asset": "GC", "current_price": 2615.0, "ohlc": {"bars": []}})
        if url.endswith("/predict"):
            return _FakeHTTPResponse({"prediction_id": "pred_99", "revision_number": 1, "status": "accepted"})
        if "/scorecard" in url:
            return _FakeHTTPResponse({"agent_id": "agt_two", "avg_score": 78.5, "forecasting_skill": 0.84})
        if "/results" in url:
            return _FakeHTTPResponse({"challenge_id": "gc-live-1", "outcome": "bullish"})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    challenges = c2.list_open_challenges(asset="GC", authenticated=True)
    assert len(challenges) == 1
    assert challenges[0].challenge_id == "gc-live-1"

    ctx_resp = c2.fetch_market_context("GC", hours=24, bar_limit=48)
    assert ctx_resp["asset"] == "GC"

    fc = passport_to_ternary_forecast(_make_passport(), challenges[0])
    dry = c2.submit_forecast(fc, dry_run=True)
    assert dry["status"] == "dry_run"
    assert dry["payload"]["probabilities"] == fc.probabilities

    live = c2.submit_forecast(fc, model_provider="Google", model_name="gemini-2.5-flash", dry_run=False)
    assert live["prediction_id"] == "pred_99"
    # Verify token was cached (only 1 token request despite active challenges + submit)
    token_calls = [c for c in captured if c[1].endswith("/agent/auth/token")]
    assert len(token_calls) == 1

    submit_call = next(c for c in captured if c[1].endswith("/predict"))
    assert submit_call[2]["Authorization"] == "Bearer tok_abc"
    assert submit_call[2]["X-agent-id"] == "agt_two"
    assert "Idempotency-key" in submit_call[2]
    assert submit_call[3] is not None
    assert submit_call[3]["model_provider"] == "Google"

    sc = c2.get_scorecard()
    assert sc["avg_score"] == 78.5
    res = c2.get_challenge_results("gc-live-1")
    assert res["outcome"] == "bullish"


def test_client_error_handling_for_missing_creds_and_http_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = HeadlineArenaClient(
        HeadlineArenaSettings(
            base_url="https://headlinearena.com",
            agent_id="",
            client_secret="",
            credentials_path=str(tmp_path / "missing.json"),
        )
    )
    assert client.has_credentials is False
    with pytest.raises(HeadlineArenaError, match="credentials not configured"):
        client.get_access_token()
    with pytest.raises(HeadlineArenaError, match="agent_id is required"):
        client.get_scorecard()

    def raise_http(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.HTTPError(
            url="https://headlinearena.com/api/v1/eval/challenges",
            code=403,
            msg="Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"detail":"missing scope"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", raise_http)
    with pytest.raises(HeadlineArenaError, match="HTTP 403"):
        client.list_open_challenges()


def test_offline_macro_desk_cycle_and_cli_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    challenges = generate_offline_macro_challenges()
    assert [c.asset for c in challenges] == ["GC", "CL", "ES", "ZN"]

    # Verify fallback synthetic bars path when challenge has no OHLC bars
    bare = HeadlineArenaChallenge(challenge_id="bare-1", asset="GC", open_price=2500.0)
    ctx_bare, df_bare = challenge_to_market_inputs(bare)
    assert len(df_bare) == 50
    assert ctx_bare.ticker == "GC"

    desk = HeadlineArenaDesk(
        client=HeadlineArenaClient(
            HeadlineArenaSettings(
                base_url="https://headlinearena.com",
                agent_id="",
                client_secret="",
                credentials_path=str(tmp_path / "none.json"),
            )
        ),
        use_llm=False,
    )
    results = desk.run_cycle(live_discovery=False, submit=False)
    assert len(results) == 4
    statuses = {r.challenge.asset: r.passport.consensus.status for r in results}
    assert all(r.passport.asset_class == "MACRO_FUTURES" for r in results)
    # CL overbought spike + high VIX must trigger Devil's Advocate VETO -> neutral calibration
    assert statuses["CL"] == "VETOED"
    cl_res = next(r for r in results if r.challenge.asset == "CL")
    assert cl_res.forecast.direction == "neutral"

    # CLI dry-run + asset filter
    cli_main(["headline-arena", "--asset", "GC"])

    # CLI scorecard path (mocked)
    monkeypatch.setattr(
        HeadlineArenaClient,
        "get_scorecard",
        lambda self, agent_id=None: {"agent_id": agent_id or "agt_demo", "avg_score": 81.2},
    )
    cli_main(["headline-arena", "--scorecard", "--agent-id", "agt_demo"])
