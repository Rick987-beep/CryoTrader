"""
Strategy definitions.

Each module exports a function that returns a StrategyConfig.
Import them here for convenient access from main.py.
"""

from strategies.blueprint_strangle import blueprint_strangle
from strategies.atm_straddle import atm_straddle
from strategies.test_strangle_11mar import test_strangle_11mar

__all__ = [
    "blueprint_strangle",
    "test_strangle_11mar",
    "atm_straddle",
]
