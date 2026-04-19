"""
execution/profiles.py — Declarative execution profiles.

Profiles define how orders are placed and managed through phased execution.
Loaded from TOML, overridable per-slot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # Python < 3.11


@dataclass
class PhaseConfig:
    """One phase in a multi-phase execution plan."""
    pricing: str = "aggressive"
    duration_seconds: float = 30.0
    buffer_pct: float = 2.0
    fair_aggression: float = 0.0
    reprice_interval: float = 30.0
    min_price_pct_of_fair: Optional[float] = None
    min_floor_price: Optional[float] = None

    _ALLOWED_PRICING = {"aggressive", "mid", "top_of_book", "mark", "passive", "fair"}

    def __post_init__(self):
        if self.pricing not in self._ALLOWED_PRICING:
            raise ValueError(
                f"Unknown pricing '{self.pricing}', "
                f"must be one of {sorted(self._ALLOWED_PRICING)}"
            )
        if self.duration_seconds < 10:
            self.duration_seconds = 10.0
        if self.reprice_interval < 10:
            self.reprice_interval = 10.0


@dataclass
class ExecutionProfile:
    """A named execution profile with open and close phase lists."""
    name: str
    open_phases: List[PhaseConfig] = field(default_factory=list)
    close_phases: List[PhaseConfig] = field(default_factory=list)
    open_atomic: bool = True       # False = best_effort (skip failed legs)
    close_best_effort: bool = True
    rfq_mode: str = "never"        # "never", "hybrid", "always"
    max_open_retries: int = 3      # retries for skipped legs before unwind

    def apply_overrides(self, overrides: Dict[str, Any]) -> "ExecutionProfile":
        """Return a copy with field-level overrides applied.

        Supports flat overrides like:
            {"open_phase_1.duration_seconds": 120}
        """
        import copy
        profile = copy.deepcopy(self)
        for key, value in overrides.items():
            parts = key.split(".")
            if len(parts) == 2 and parts[0].startswith("open_phase_"):
                idx = int(parts[0].split("_")[-1]) - 1  # 1-based in TOML
                if 0 <= idx < len(profile.open_phases):
                    setattr(profile.open_phases[idx], parts[1], value)
            elif len(parts) == 2 and parts[0].startswith("close_phase_"):
                idx = int(parts[0].split("_")[-1]) - 1
                if 0 <= idx < len(profile.close_phases):
                    setattr(profile.close_phases[idx], parts[1], value)
            elif hasattr(profile, key):
                setattr(profile, key, value)
        return profile


def _parse_phases(raw: List[Dict[str, Any]]) -> List[PhaseConfig]:
    """Convert a list of TOML dicts to PhaseConfig objects."""
    return [PhaseConfig(**p) for p in raw]


def _collect_numbered_phases(
    section: Dict[str, Any], prefix: str
) -> List[PhaseConfig]:
    """Collect numbered phase tables (e.g. open_phase_1, open_phase_2, ...)
    from a profile section and return them in order."""
    phases: List[tuple] = []
    for key, value in section.items():
        if key.startswith(prefix) and isinstance(value, dict):
            try:
                idx = int(key.split("_")[-1])
                phases.append((idx, value))
            except ValueError:
                continue
    phases.sort(key=lambda t: t[0])
    return [PhaseConfig(**p) for _, p in phases]


def load_profiles(
    toml_path: Optional[str] = None,
) -> Dict[str, ExecutionProfile]:
    """Load all execution profiles from a TOML file.

    Default path: ``execution_profiles.toml`` in the project root.
    """
    if toml_path is None:
        toml_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "execution_profiles.toml",
        )
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    profiles: Dict[str, ExecutionProfile] = {}
    for name, section in data.get("profile", {}).items():
        # Support both numbered keys (open_phase_1, ...) and array-of-tables (open_phases)
        open_phases = _collect_numbered_phases(section, "open_phase_")
        if not open_phases:
            open_phases = _parse_phases(section.get("open_phases", []))
        close_phases = _collect_numbered_phases(section, "close_phase_")
        if not close_phases:
            close_phases = _parse_phases(section.get("close_phases", []))

        profiles[name] = ExecutionProfile(
            name=name,
            open_phases=open_phases,
            close_phases=close_phases,
            open_atomic=section.get("open_atomic", True),
            close_best_effort=section.get("close_best_effort", True),
            rfq_mode=section.get("rfq_mode", "never"),
        )
    return profiles


def get_profile(
    name: str,
    profiles: Optional[Dict[str, ExecutionProfile]] = None,
    toml_path: Optional[str] = None,
) -> ExecutionProfile:
    """Load a single profile by name.  Raises ValueError if not found."""
    if profiles is None:
        profiles = load_profiles(toml_path)
    if name not in profiles:
        raise ValueError(
            f"Execution profile '{name}' not found. "
            f"Available: {sorted(profiles)}"
        )
    return profiles[name]
