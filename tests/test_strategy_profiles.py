"""Tests for Phase 4: Strategy profile resolution, slot overrides, and fee formatting.

Covers:
    - Each strategy factory returns the correct named execution profile
    - Profile resolution stashes ExecutionProfile on trade metadata
    - Per-slot execution overrides apply correctly
    - Telegram fee formatting in close messages
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from execution.profiles import (
    ExecutionProfile,
    PhaseConfig,
    get_profile,
    load_profiles,
)

TOML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "execution_profiles.toml",
)


# ─── Strategy Factory Profile Tests ────────────────────────────────────────

class TestShortStrangleDeltaTpProfile:
    """Verify short_strangle_delta_tp uses delta_strangle_2phase profile."""

    def test_factory_sets_execution_profile(self):
        from strategies.short_strangle_delta_tp import short_strangle_delta_tp
        config = short_strangle_delta_tp()
        assert config.execution_profile == "delta_strangle_2phase"
        assert config.execution_params is None

    def test_profile_has_correct_structure(self):
        profile = get_profile("delta_strangle_2phase", toml_path=TOML_PATH)
        assert len(profile.open_phases) == 2
        assert len(profile.close_phases) == 2
        assert profile.open_phases[0].pricing == "fair"
        assert profile.open_phases[0].fair_aggression == 0.0
        assert profile.open_phases[1].fair_aggression == 1.0
        assert profile.close_phases[1].min_floor_price == pytest.approx(0.0001)
        assert profile.open_atomic is True
        assert profile.close_best_effort is True


class TestPutSell80dteProfile:
    """Verify put_sell_80dte uses passive_open_3phase profile."""

    def test_factory_sets_execution_profile(self):
        from strategies.put_sell_80dte import put_sell_80dte
        config = put_sell_80dte()
        assert config.execution_profile == "passive_open_3phase"
        assert config.execution_params is None

    def test_profile_has_correct_structure(self):
        profile = get_profile("passive_open_3phase", toml_path=TOML_PATH)
        assert len(profile.open_phases) == 3
        assert len(profile.close_phases) == 3
        # Open: fair@0.0 → fair@0.67 → fair@1.0
        assert profile.open_phases[0].fair_aggression == 0.0
        assert profile.open_phases[1].fair_aggression == pytest.approx(0.67)
        assert profile.open_phases[2].fair_aggression == 1.0
        # min_price_pct on phases 2 & 3
        assert profile.open_phases[1].min_price_pct_of_fair == pytest.approx(0.83)
        assert profile.open_phases[2].min_price_pct_of_fair == pytest.approx(0.83)


class TestLongStrangleIndexMoveProfile:
    """Verify long_strangle_index_move uses aggressive_2phase profile."""

    def test_factory_sets_execution_profile(self):
        from strategies.long_strangle_index_move import long_strangle_index_move
        config = long_strangle_index_move()
        assert config.execution_profile == "aggressive_2phase"
        assert config.execution_params is None

    def test_profile_has_correct_structure(self):
        profile = get_profile("aggressive_2phase", toml_path=TOML_PATH)
        assert len(profile.open_phases) == 2
        assert len(profile.close_phases) == 3
        assert profile.open_phases[0].pricing == "aggressive"
        assert profile.close_phases[2].min_floor_price == pytest.approx(0.0001)
        assert profile.close_phases[2].duration_seconds == 14400.0


# ─── Profile Resolution on Trade ────────────────────────────────────────────

class TestProfileResolutionOnTrade:
    """Verify StrategyRunner stashes resolved profile on trade metadata."""

    def _make_runner(self, profile_name="delta_strangle_2phase"):
        from strategy import StrategyConfig, StrategyRunner, TradingContext
        from option_selection import LegSpec

        profiles = load_profiles(TOML_PATH)

        config = StrategyConfig(
            name="test_strategy",
            legs=[LegSpec(option_type="C", side="buy", qty=1.0,
                          strike_criteria={"type": "delta", "value": 0.5},
                          expiry_criteria={"dte": 1})],
            execution_mode="limit",
            execution_profile=profile_name,
        )

        # Mock context (no spec — TradingContext is a dataclass)
        ctx = MagicMock()
        ctx.profiles = profiles

        # Mock lifecycle_manager.create to return a mock trade
        mock_trade = MagicMock()
        mock_trade.id = "test-123"
        mock_trade.metadata = {"strategy": "test_strategy"}
        ctx.lifecycle_manager.create.return_value = mock_trade
        ctx.lifecycle_manager.active_trades_for_strategy.return_value = []
        ctx.lifecycle_manager.all_trades_for_strategy.return_value = []

        runner = StrategyRunner(config, ctx)
        return runner, ctx, mock_trade

    @patch("strategy.resolve_legs")
    def test_profile_stashed_on_trade_metadata(self, mock_resolve):
        from trade_lifecycle import TradeLeg
        mock_resolve.return_value = [
            TradeLeg(symbol="BTC-1JAN26-50000-C", qty=1.0, side="buy")
        ]
        runner, ctx, mock_trade = self._make_runner()
        runner._open_trade()

        # Verify profile was stashed
        profile = mock_trade.metadata.get("_execution_profile")
        assert profile is not None
        assert isinstance(profile, ExecutionProfile)
        assert profile.name == "delta_strangle_2phase"

    @patch("strategy.resolve_legs")
    def test_profile_not_stashed_when_no_profile_name(self, mock_resolve):
        from strategy import StrategyConfig, StrategyRunner, TradingContext
        from option_selection import LegSpec
        from trade_lifecycle import TradeLeg

        mock_resolve.return_value = [
            TradeLeg(symbol="BTC-1JAN26-50000-C", qty=1.0, side="buy")
        ]

        config = StrategyConfig(
            name="no_profile",
            legs=[LegSpec(option_type="C", side="buy", qty=1.0,
                          strike_criteria={"type": "delta", "value": 0.5},
                          expiry_criteria={"dte": 1})],
            execution_mode="limit",
        )

        ctx = MagicMock()
        ctx.profiles = {}
        mock_trade = MagicMock()
        mock_trade.id = "test-456"
        mock_trade.metadata = {"strategy": "no_profile"}
        ctx.lifecycle_manager.create.return_value = mock_trade
        ctx.lifecycle_manager.active_trades_for_strategy.return_value = []
        ctx.lifecycle_manager.all_trades_for_strategy.return_value = []

        runner = StrategyRunner(config, ctx)
        runner._open_trade()

        assert "_execution_profile" not in mock_trade.metadata


# ─── Per-Slot Override Tests ────────────────────────────────────────────────

class TestSlotOverrides:
    """Test per-slot execution profile overrides via env vars."""

    def test_env_execution_profile_overrides_config(self):
        from strategy import StrategyConfig, StrategyRunner, TradingContext
        from option_selection import LegSpec

        profiles = load_profiles(TOML_PATH)
        config = StrategyConfig(
            name="test_override",
            legs=[LegSpec(option_type="C", side="buy", qty=1.0,
                          strike_criteria={"type": "delta", "value": 0.5},
                          expiry_criteria={"dte": 1})],
            execution_profile="delta_strangle_2phase",
        )

        ctx = MagicMock()
        ctx.profiles = profiles

        with patch.dict(os.environ, {"EXECUTION_PROFILE": "passive_open_3phase"}):
            runner = StrategyRunner(config, ctx)
            assert runner.config.execution_profile == "passive_open_3phase"

    def test_env_execution_overrides_collected(self):
        from strategy import StrategyConfig, StrategyRunner, TradingContext
        from option_selection import LegSpec

        config = StrategyConfig(
            name="test_override",
            legs=[LegSpec(option_type="C", side="buy", qty=1.0,
                          strike_criteria={"type": "delta", "value": 0.5},
                          expiry_criteria={"dte": 1})],
            execution_profile="passive_open_3phase",
        )

        ctx = MagicMock()
        ctx.profiles = {}

        env = {"EXECUTION_OVERRIDE_open_phase_1.duration_seconds": "120"}
        with patch.dict(os.environ, env, clear=False):
            runner = StrategyRunner(config, ctx)
            overrides = runner.config.metadata.get("execution_overrides", {})
            assert "open_phase_1.duration_seconds" in overrides
            assert overrides["open_phase_1.duration_seconds"] == 120.0

    @patch("strategy.resolve_legs")
    def test_overrides_applied_to_profile_on_trade(self, mock_resolve):
        from strategy import StrategyConfig, StrategyRunner, TradingContext
        from option_selection import LegSpec
        from trade_lifecycle import TradeLeg

        mock_resolve.return_value = [
            TradeLeg(symbol="BTC-1JAN26-50000-C", qty=1.0, side="buy")
        ]

        profiles = load_profiles(TOML_PATH)
        config = StrategyConfig(
            name="test_override",
            legs=[LegSpec(option_type="C", side="buy", qty=1.0,
                          strike_criteria={"type": "delta", "value": 0.5},
                          expiry_criteria={"dte": 1})],
            execution_mode="limit",
            execution_profile="passive_open_3phase",
            metadata={"execution_overrides": {"open_phase_1.duration_seconds": 120.0}},
        )

        ctx = MagicMock()
        ctx.profiles = profiles

        mock_trade = MagicMock()
        mock_trade.id = "test-789"
        mock_trade.metadata = {"strategy": "test_override"}
        ctx.lifecycle_manager.create.return_value = mock_trade
        ctx.lifecycle_manager.active_trades_for_strategy.return_value = []
        ctx.lifecycle_manager.all_trades_for_strategy.return_value = []

        runner = StrategyRunner(config, ctx)
        runner._open_trade()

        profile = mock_trade.metadata.get("_execution_profile")
        assert profile is not None
        assert profile.open_phases[0].duration_seconds == 120.0
        # Other fields unchanged
        assert profile.open_phases[1].duration_seconds == 45.0


class TestSlotConfigGeneration:
    """Test slot_config.py generates execution profile env vars."""

    def test_generate_env_with_execution_profile(self):
        from slot_config import generate_env

        slot_config = {
            "strategy": "put_sell_80dte",
            "account": "deribit-big",
            "execution_profile": "passive_open_3phase",
            "params": {"QTY": 0.1},
        }
        account = {"exchange": "deribit", "environment": "production",
                    "api_key_env": "DERIBIT_CLIENT_ID_PROD",
                    "api_secret_env": "DERIBIT_CLIENT_SECRET_PROD"}
        secrets = {"api_key": "test_key", "api_secret": "test_secret"}
        env_values = {}

        result = generate_env("01", slot_config, account, secrets, env_values)
        assert "EXECUTION_PROFILE=passive_open_3phase" in result

    def test_generate_env_with_execution_overrides(self):
        from slot_config import generate_env

        slot_config = {
            "strategy": "put_sell_80dte",
            "account": "deribit-big",
            "execution_overrides": {
                "open_phase_1.duration_seconds": 120,
            },
        }
        account = {"exchange": "deribit", "environment": "production",
                    "api_key_env": "DERIBIT_CLIENT_ID_PROD",
                    "api_secret_env": "DERIBIT_CLIENT_SECRET_PROD"}
        secrets = {"api_key": "test_key", "api_secret": "test_secret"}
        env_values = {}

        result = generate_env("01", slot_config, account, secrets, env_values)
        assert "EXECUTION_OVERRIDE_open_phase_1.duration_seconds=120" in result

    def test_generate_env_without_execution_profile(self):
        from slot_config import generate_env

        slot_config = {
            "strategy": "put_sell_80dte",
            "account": "deribit-big",
        }
        account = {"exchange": "deribit", "environment": "production",
                    "api_key_env": "DERIBIT_CLIENT_ID_PROD",
                    "api_secret_env": "DERIBIT_CLIENT_SECRET_PROD"}
        secrets = {"api_key": "test_key", "api_secret": "test_secret"}
        env_values = {}

        result = generate_env("01", slot_config, account, secrets, env_values)
        assert "EXECUTION_PROFILE" not in result
        assert "EXECUTION_OVERRIDE" not in result


# ─── Telegram Fee Formatting Tests ──────────────────────────────────────────

class TestTelegramFeeFormatting:
    """Test fee data appears in Telegram close messages."""

    def _make_trade(self, open_fees=None, close_fees=None):
        """Build a mock trade with fee data."""
        from execution.currency import Price, Currency

        trade = SimpleNamespace(
            id="test-fee-001",
            open_legs=[
                SimpleNamespace(
                    symbol="BTC-1JAN26-90000-C", side="sell", qty=1.0,
                    filled_qty=1.0, fill_price=0.0025, close_side="buy",
                ),
                SimpleNamespace(
                    symbol="BTC-1JAN26-80000-P", side="sell", qty=1.0,
                    filled_qty=1.0, fill_price=0.0020, close_side="buy",
                ),
            ],
            close_legs=[
                SimpleNamespace(
                    symbol="BTC-1JAN26-90000-C", side="buy", qty=1.0,
                    filled_qty=1.0, fill_price=0.0010,
                ),
                SimpleNamespace(
                    symbol="BTC-1JAN26-80000-P", side="buy", qty=1.0,
                    filled_qty=1.0, fill_price=0.0008,
                ),
            ],
            realized_pnl=0.0027,  # (0.0025+0.0020) - (0.0010+0.0008) = 0.0027
            hold_seconds=3600,
            opened_at=1000.0,
            closed_at=4600.0,
            created_at=999.0,
            open_fees=Price(open_fees, Currency.BTC) if open_fees else None,
            close_fees=Price(close_fees, Currency.BTC) if close_fees else None,
            metadata={
                "combined_premium": 0.0045,
                "sl_threshold": 0.0135,
            },
        )
        return trade

    @patch("strategies.short_strangle_delta_tp.get_notifier")
    @patch("strategies.short_strangle_delta_tp.get_btc_index_price", return_value=100000.0)
    @patch("strategies.short_strangle_delta_tp._fair", return_value=None)
    def test_strangle_close_message_includes_fees(self, mock_fair, mock_idx, mock_notifier):
        from strategies.short_strangle_delta_tp import _on_trade_closed

        trade = self._make_trade(open_fees=0.0003, close_fees=0.0002)
        account = SimpleNamespace(equity=50000.0)

        _on_trade_closed(trade, account)

        mock_notifier().send.assert_called_once()
        msg = mock_notifier().send.call_args[0][0]
        assert "Fees:" in msg
        assert "0.000500" in msg  # total fees = 0.0003 + 0.0002
        assert "Net PnL" in msg

    @patch("strategies.short_strangle_delta_tp.get_notifier")
    @patch("strategies.short_strangle_delta_tp.get_btc_index_price", return_value=100000.0)
    @patch("strategies.short_strangle_delta_tp._fair", return_value=None)
    def test_strangle_close_message_no_fees_when_zero(self, mock_fair, mock_idx, mock_notifier):
        from strategies.short_strangle_delta_tp import _on_trade_closed

        trade = self._make_trade()  # no fees
        account = SimpleNamespace(equity=50000.0)

        _on_trade_closed(trade, account)

        mock_notifier().send.assert_called_once()
        msg = mock_notifier().send.call_args[0][0]
        # No fee line when total_fees == 0
        assert "open 0.000" not in msg


# ─── Max Hold Profile Override Test ─────────────────────────────────────────

class TestMaxHoldProfileOverride:
    """Verify max-hold exit swaps to max_hold_close_1phase profile."""

    def test_max_hold_swaps_profile(self):
        from strategies.short_strangle_delta_tp import _max_hold_close

        max_hold_profile = get_profile("max_hold_close_1phase", toml_path=TOML_PATH)

        cond = _max_hold_close()

        trade = SimpleNamespace(
            id="test-mh",
            hold_seconds=200000,  # > MAX_HOLD_HOURS * 3600
            metadata={
                "_execution_profile": get_profile("delta_strangle_2phase", toml_path=TOML_PATH),
                "_max_hold_close_profile": max_hold_profile,
            },
        )
        account = SimpleNamespace()

        triggered = cond(account, trade)
        assert triggered is True
        # Profile should have been swapped to max-hold profile
        assert trade.metadata["_execution_profile"].name == "max_hold_close_1phase"
        assert len(trade.metadata["_execution_profile"].close_phases) == 1
