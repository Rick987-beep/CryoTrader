"""Tests for execution/profiles.py — profile loading from TOML."""

import os
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


class TestPhaseConfig:
    def test_defaults(self):
        p = PhaseConfig()
        assert p.pricing == "aggressive"
        assert p.duration_seconds == 30.0

    def test_invalid_pricing_raises(self):
        with pytest.raises(ValueError, match="Unknown pricing"):
            PhaseConfig(pricing="vwap")

    def test_min_duration_clamp(self):
        p = PhaseConfig(duration_seconds=5.0)
        assert p.duration_seconds == 10.0

    def test_min_reprice_clamp(self):
        p = PhaseConfig(reprice_interval=3.0)
        assert p.reprice_interval == 10.0


class TestLoadProfiles:
    def test_loads_all_profiles(self):
        profiles = load_profiles(TOML_PATH)
        assert "passive_open_3phase" in profiles
        assert "delta_strangle_2phase" in profiles
        assert "aggressive_2phase" in profiles
        assert "max_hold_close_1phase" in profiles

    def test_passive_open_3phase_structure(self):
        profiles = load_profiles(TOML_PATH)
        p = profiles["passive_open_3phase"]
        assert len(p.open_phases) == 3
        assert len(p.close_phases) == 3
        # Phase order: fair@0.0 → fair@0.67 → fair@1.0
        assert p.open_phases[0].pricing == "fair"
        assert p.open_phases[0].fair_aggression == 0.0
        assert p.open_phases[1].fair_aggression == pytest.approx(0.67)
        assert p.open_phases[2].fair_aggression == 1.0
        # min_price_pct_of_fair on phases 2 & 3
        assert p.open_phases[0].min_price_pct_of_fair is None
        assert p.open_phases[1].min_price_pct_of_fair == pytest.approx(0.83)
        assert p.open_phases[2].min_price_pct_of_fair == pytest.approx(0.83)

    def test_delta_strangle_2phase_structure(self):
        profiles = load_profiles(TOML_PATH)
        p = profiles["delta_strangle_2phase"]
        assert len(p.open_phases) == 2
        assert len(p.close_phases) == 2
        assert p.close_phases[1].min_floor_price == pytest.approx(0.0001)

    def test_aggressive_2phase_structure(self):
        profiles = load_profiles(TOML_PATH)
        p = profiles["aggressive_2phase"]
        assert len(p.open_phases) == 2
        assert len(p.close_phases) == 3
        assert p.open_phases[0].pricing == "aggressive"
        assert p.close_phases[2].min_floor_price == pytest.approx(0.0001)
        assert p.close_phases[2].duration_seconds == 14400.0

    def test_max_hold_close_1phase(self):
        profiles = load_profiles(TOML_PATH)
        p = profiles["max_hold_close_1phase"]
        assert len(p.open_phases) == 0
        assert len(p.close_phases) == 1
        assert p.close_phases[0].pricing == "fair"
        assert p.close_phases[0].fair_aggression == 1.0

    def test_atomic_flags(self):
        profiles = load_profiles(TOML_PATH)
        p = profiles["passive_open_3phase"]
        assert p.open_atomic is True
        assert p.close_best_effort is True


class TestGetProfile:
    def test_existing_profile(self):
        p = get_profile("passive_open_3phase", toml_path=TOML_PATH)
        assert p.name == "passive_open_3phase"

    def test_nonexistent_raises(self):
        with pytest.raises(ValueError, match="not found"):
            get_profile("nonexistent_profile", toml_path=TOML_PATH)


class TestOverrides:
    def test_override_single_field(self):
        profiles = load_profiles(TOML_PATH)
        p = profiles["passive_open_3phase"]
        overridden = p.apply_overrides({"open_phase_1.duration_seconds": 120.0})
        # Overridden field changed
        assert overridden.open_phases[0].duration_seconds == 120.0
        # Other fields unchanged
        assert overridden.open_phases[0].pricing == "fair"
        assert overridden.open_phases[1].duration_seconds == 45.0
        # Original unchanged
        assert p.open_phases[0].duration_seconds == 45.0

    def test_override_close_phase(self):
        profiles = load_profiles(TOML_PATH)
        p = profiles["delta_strangle_2phase"]
        overridden = p.apply_overrides({"close_phase_2.min_floor_price": 0.001})
        assert overridden.close_phases[1].min_floor_price == 0.001

    def test_override_top_level(self):
        profiles = load_profiles(TOML_PATH)
        p = profiles["passive_open_3phase"]
        overridden = p.apply_overrides({"rfq_mode": "hybrid"})
        assert overridden.rfq_mode == "hybrid"
        assert p.rfq_mode == "never"  # original unchanged
