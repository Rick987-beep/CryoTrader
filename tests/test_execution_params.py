"""
Unit tests for ExecutionPhase and ExecutionParams dataclasses.

Pure data tests — no network, no mocks needed.
"""

from unittest.mock import MagicMock
import pytest

from trade_execution import ExecutionPhase, ExecutionParams, LimitFillManager
from trade_lifecycle import RFQParams, TradeLifecycle, TradeState
from strategy import StrategyConfig
from option_selection import strangle


# ── ExecutionPhase ───────────────────────────────────────────────────────

class TestExecutionPhase:
    def test_defaults(self):
        p = ExecutionPhase()
        assert p.pricing == "aggressive"
        assert p.duration_seconds == 30.0
        assert p.buffer_pct == 2.0
        assert p.reprice_interval == 30.0

    def test_duration_clamped_to_minimum(self):
        p = ExecutionPhase(pricing="mid", duration_seconds=5)
        assert p.duration_seconds == 10.0

    def test_reprice_interval_clamped_to_minimum(self):
        p = ExecutionPhase(pricing="mid", reprice_interval=3)
        assert p.reprice_interval == 10.0

    @pytest.mark.parametrize("mode", ["aggressive", "mid", "top_of_book", "mark", "passive", "fair"])
    def test_valid_pricing_modes(self, mode):
        p = ExecutionPhase(pricing=mode)
        assert p.pricing == mode

    def test_invalid_pricing_raises(self):
        with pytest.raises(ValueError):
            ExecutionPhase(pricing="invalid")


# ── ExecutionParams ──────────────────────────────────────────────────────

class TestExecutionParams:
    def test_legacy_defaults(self):
        ep = ExecutionParams()
        assert ep.phases is None
        assert ep.fill_timeout_seconds == 30.0
        assert ep.aggressive_buffer_pct == 2.0
        assert ep.max_requote_rounds == 10

    def test_phased_mode(self):
        ep = ExecutionParams(phases=[
            ExecutionPhase(pricing="mark", duration_seconds=300, reprice_interval=30),
            ExecutionPhase(pricing="aggressive", duration_seconds=120, buffer_pct=2.0),
        ])
        assert len(ep.phases) == 2
        assert ep.phases[0].pricing == "mark"
        assert ep.phases[0].duration_seconds == 300.0
        assert ep.phases[1].pricing == "aggressive"
        assert ep.phases[1].buffer_pct == 2.0


# ── RFQParams ────────────────────────────────────────────────────────────

class TestRFQParams:
    def test_defaults(self):
        rp = RFQParams()
        assert rp.timeout_seconds == 60.0
        assert rp.min_improvement_pct == -999.0
        assert rp.fallback_mode is None

    def test_custom_values(self):
        rp = RFQParams(timeout_seconds=300, min_improvement_pct=2.0, fallback_mode="limit")
        assert rp.timeout_seconds == 300.0
        assert rp.min_improvement_pct == 2.0
        assert rp.fallback_mode == "limit"


# ── TradeLifecycle params integration ────────────────────────────────────

class TestTradeLifecycleParams:
    def test_defaults_none(self):
        t = TradeLifecycle()
        assert t.execution_params is None
        assert t.rfq_params is None

    def test_accepts_params(self):
        ep = ExecutionParams(phases=[ExecutionPhase()])
        rp = RFQParams(timeout_seconds=300)
        t = TradeLifecycle(execution_params=ep, rfq_params=rp)
        assert t.execution_params is ep
        assert t.rfq_params is rp
        assert t.state == TradeState.PENDING_OPEN


# ── StrategyConfig params integration ────────────────────────────────────

class TestStrategyConfigParams:
    def test_defaults_none(self):
        legs = strangle(qty=0.01, call_delta=0.15, put_delta=-0.15, dte="next", side="buy")
        sc = StrategyConfig(name="test", legs=legs)
        assert sc.execution_params is None
        assert sc.rfq_params is None

    def test_accepts_params(self):
        legs = strangle(qty=0.01, call_delta=0.15, put_delta=-0.15, dte="next", side="buy")
        ep = ExecutionParams(phases=[ExecutionPhase()])
        rp = RFQParams(timeout_seconds=300)
        sc = StrategyConfig(
            name="test_phased", legs=legs,
            execution_mode="limit", execution_params=ep, rfq_params=rp,
        )
        assert sc.execution_params is ep
        assert sc.rfq_params is rp


# ── LimitFillManager initialization ──────────────────────────────────────

class TestLimitFillManagerInit:
    def test_legacy_mode(self):
        mgr = LimitFillManager(MagicMock(), ExecutionParams())
        assert mgr._using_phases is False

    def test_phased_mode(self):
        ep = ExecutionParams(phases=[
            ExecutionPhase(pricing="mark", duration_seconds=300),
            ExecutionPhase(pricing="aggressive", duration_seconds=120),
        ])
        mgr = LimitFillManager(MagicMock(), ep)
        assert mgr._using_phases is True
        assert mgr._phase_index == 0
        assert mgr._current_phase.pricing == "mark"

    def test_empty_phases_is_legacy(self):
        mgr = LimitFillManager(MagicMock(), ExecutionParams(phases=[]))
        assert mgr._using_phases is False
