"""
Unit tests for persistence — TradeStatePersistence JSONL save/load.

Pure file I/O tests using tmp_path — no network calls.
"""

import json
import os
import pytest

from persistence import TradeStatePersistence, HISTORY_FILE
from trade_lifecycle import TradeLifecycle, TradeLeg, TradeState


class FakeTrade:
    """Minimal duck-typed trade for persistence tests."""

    def __init__(self, trade_id="test-1", strategy_id="daily_put_sell",
                 state=TradeState.CLOSED, realized_pnl=42.5):
        self.id = trade_id
        self.strategy_id = strategy_id
        self.state = state
        self.created_at = 1000000.0
        self.opened_at = 1000010.0
        self.closed_at = 1000100.0
        self.hold_seconds = 90.0
        self.realized_pnl = realized_pnl
        self.open_legs = [
            TradeLeg(symbol="BTCUSD-28MAR26-85000-P", qty=0.8,
                     side="sell", fill_price=50.0, filled_qty=0.8),
        ]
        self.close_legs = [
            TradeLeg(symbol="BTCUSD-28MAR26-85000-P", qty=0.8,
                     side="buy", fill_price=30.0, filled_qty=0.8),
        ]

    def total_entry_cost(self):
        return -40.0


class TestTradeStatePersistence:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("persistence.HISTORY_FILE", str(tmp_path / "trade_history.jsonl"))
        monkeypatch.setattr("os.makedirs", lambda *a, **kw: None)

        p = TradeStatePersistence()
        trade = FakeTrade()
        p.save_completed_trade(trade)

        records = p.load_trade_history()
        assert len(records) == 1
        assert records[0]["id"] == "test-1"
        assert records[0]["strategy_id"] == "daily_put_sell"
        assert records[0]["realized_pnl"] == 42.5

    def test_multiple_trades_appended(self, tmp_path, monkeypatch):
        monkeypatch.setattr("persistence.HISTORY_FILE", str(tmp_path / "trade_history.jsonl"))
        monkeypatch.setattr("os.makedirs", lambda *a, **kw: None)

        p = TradeStatePersistence()
        p.save_completed_trade(FakeTrade(trade_id="t1", realized_pnl=10.0))
        p.save_completed_trade(FakeTrade(trade_id="t2", realized_pnl=20.0))

        records = p.load_trade_history()
        assert len(records) == 2
        assert records[0]["id"] == "t1"
        assert records[1]["id"] == "t2"

    def test_load_empty_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("persistence.HISTORY_FILE", str(tmp_path / "nonexistent.jsonl"))
        p = TradeStatePersistence()
        records = p.load_trade_history()
        assert records == []

    def test_legs_serialized(self, tmp_path, monkeypatch):
        monkeypatch.setattr("persistence.HISTORY_FILE", str(tmp_path / "trade_history.jsonl"))
        monkeypatch.setattr("os.makedirs", lambda *a, **kw: None)

        p = TradeStatePersistence()
        p.save_completed_trade(FakeTrade())

        records = p.load_trade_history()
        assert len(records[0]["open_legs"]) == 1
        assert records[0]["open_legs"][0]["symbol"] == "BTCUSD-28MAR26-85000-P"
        assert records[0]["open_legs"][0]["fill_price"] == 50.0
