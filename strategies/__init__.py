"""
Strategy definitions.

Each module exports a function that returns a StrategyConfig.
Import them here for convenient access from main.py.
"""

from strategies.blueprint_strangle import blueprint_strangle
from strategies.atm_straddle import atm_straddle
from strategies.atm_straddle_index_move import atm_straddle_index_move

__all__ = [
    "blueprint_strangle",
    "atm_straddle",
    "atm_straddle_index_move",
]
