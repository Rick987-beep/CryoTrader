"""
Strategy definitions.

Each module exports a function that returns a StrategyConfig.
Import them here for convenient access from main.py.
"""

from strategies.blueprint_strangle import blueprint_strangle
from strategies.atm_straddle import atm_straddle
from strategies.atm_straddle_index_move import atm_straddle_index_move
from strategies.daily_put_sell import daily_put_sell
from strategies.smoke_test_strangle import smoke_test_strangle
from strategies.prod_test_put import prod_test_put

__all__ = [
    "blueprint_strangle",
    "atm_straddle",
    "atm_straddle_index_move",
    "daily_put_sell",
    "smoke_test_strangle",
    "prod_test_put",
]
