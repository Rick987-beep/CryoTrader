#!/usr/bin/env python3
"""
Tests for the configurable execution timing feature.

Tests cover:
  1. ExecutionPhase dataclass validation
  2. ExecutionParams backward compatibility (legacy mode)
  3. ExecutionParams phased mode
  4. RFQParams dataclass
  5. TradeLifecycle new fields
  6. StrategyConfig new fields
  7. LimitFillManager phase-aware initialization
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade_execution import ExecutionPhase, ExecutionParams, LimitFillManager
from trade_lifecycle import RFQParams, TradeLifecycle, TradeState
from strategy import StrategyConfig, ExecutionParams as StrategyExecutionParams

passed = 0
failed = 0

def check(name, condition):
    global passed, failed
    if condition:
        print(f"  ✓ {name}")
        passed += 1
    else:
        print(f"  ✗ {name}")
        failed += 1


print("=== ExecutionPhase ===")

# Defaults
p = ExecutionPhase()
check("default pricing is aggressive", p.pricing == "aggressive")
check("default duration is 30s", p.duration_seconds == 30.0)
check("default buffer_pct is 2.0", p.buffer_pct == 2.0)
check("default reprice_interval is 30s", p.reprice_interval == 30.0)

# Duration clamping
p2 = ExecutionPhase(pricing="mid", duration_seconds=5)
check("duration clamped to 10s minimum", p2.duration_seconds == 10.0)

# Reprice interval clamping
p3 = ExecutionPhase(pricing="mid", reprice_interval=3)
check("reprice_interval clamped to 10s minimum", p3.reprice_interval == 10.0)

# Valid pricing modes
for mode in ["aggressive", "mid", "top_of_book", "mark"]:
    p = ExecutionPhase(pricing=mode)
    check(f"pricing '{mode}' accepted", p.pricing == mode)

# Invalid pricing
try:
    ExecutionPhase(pricing="invalid")
    check("invalid pricing raises ValueError", False)
except ValueError:
    check("invalid pricing raises ValueError", True)


print("\n=== ExecutionParams (legacy) ===")

ep = ExecutionParams()
check("default phases is None", ep.phases is None)
check("default fill_timeout is 30s", ep.fill_timeout_seconds == 30.0)
check("default aggressive_buffer is 2%", ep.aggressive_buffer_pct == 2.0)
check("default max_requote_rounds is 10", ep.max_requote_rounds == 10)


print("\n=== ExecutionParams (phased) ===")

ep2 = ExecutionParams(phases=[
    ExecutionPhase(pricing="mark", duration_seconds=300, reprice_interval=30),
    ExecutionPhase(pricing="aggressive", duration_seconds=120, buffer_pct=2.0),
])
check("phases list has 2 entries", len(ep2.phases) == 2)
check("phase 1 pricing is mark", ep2.phases[0].pricing == "mark")
check("phase 1 duration is 300s", ep2.phases[0].duration_seconds == 300.0)
check("phase 2 pricing is aggressive", ep2.phases[1].pricing == "aggressive")
check("phase 2 buffer is 2%", ep2.phases[1].buffer_pct == 2.0)


print("\n=== RFQParams ===")

rp = RFQParams()
check("default timeout is 60s", rp.timeout_seconds == 60.0)
check("default improvement is -999", rp.min_improvement_pct == -999.0)
check("default fallback is None", rp.fallback_mode is None)

rp2 = RFQParams(timeout_seconds=300, min_improvement_pct=2.0, fallback_mode="limit")
check("custom timeout is 300s", rp2.timeout_seconds == 300.0)
check("custom improvement is 2%", rp2.min_improvement_pct == 2.0)
check("custom fallback is 'limit'", rp2.fallback_mode == "limit")


print("\n=== TradeLifecycle new fields ===")

t = TradeLifecycle()
check("default execution_params is None", t.execution_params is None)
check("default rfq_params is None", t.rfq_params is None)

t2 = TradeLifecycle(execution_params=ep2, rfq_params=rp2)
check("accepts execution_params", t2.execution_params is ep2)
check("accepts rfq_params", t2.rfq_params is rp2)
check("state is still PENDING_OPEN", t2.state == TradeState.PENDING_OPEN)


print("\n=== StrategyConfig new fields ===")

from option_selection import strangle
legs = strangle(qty=0.01, call_delta=0.15, put_delta=-0.15, dte="next", side="buy")

sc = StrategyConfig(name="test", legs=legs)
check("default execution_params is None", sc.execution_params is None)
check("default rfq_params is None", sc.rfq_params is None)

sc2 = StrategyConfig(
    name="test_phased",
    legs=legs,
    execution_mode="limit",
    execution_params=ep2,
    rfq_params=rp2,
)
check("accepts execution_params", sc2.execution_params is ep2)
check("accepts rfq_params", sc2.rfq_params is rp2)


print("\n=== LimitFillManager initialization ===")

# Can't fully test without an executor, but verify phase tracking init
from unittest.mock import MagicMock
mock_executor = MagicMock()

# Legacy mode
mgr1 = LimitFillManager(mock_executor, ExecutionParams())
check("legacy mode: _using_phases is False", mgr1._using_phases == False)

# Phased mode
mgr2 = LimitFillManager(mock_executor, ep2)
check("phased mode: _using_phases is True", mgr2._using_phases == True)
check("phased mode: _phase_index starts at 0", mgr2._phase_index == 0)
check("phased mode: current phase is mark", mgr2._current_phase.pricing == "mark")

# Empty phases list (should be legacy)
mgr3 = LimitFillManager(mock_executor, ExecutionParams(phases=[]))
check("empty phases: _using_phases is False", mgr3._using_phases == False)


print("\n" + "=" * 50)
print(f"Results: {passed} passed, {failed} failed")
if failed > 0:
    sys.exit(1)
else:
    print("All tests passed!")
