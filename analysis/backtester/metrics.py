"""
Backtest metrics: trade statistics, equity curves, and scoring helpers.

Strategy-agnostic — works with any results dict keyed by parameter-combo
tuples, where values are lists of (pnl, triggered, exit_h, entry_date).

Three areas:
    1. compute_stats()          — per-combo summary (avg, median, win rate, etc.)
    2. compute_equity_metrics() — daily equity curve, drawdown, Sortino, Calmar
    3. Scoring helpers          — percentile_ranks(), neighbor_avg_pnl()

To use with a new strategy, just return results in the same tuple format.
"""

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta


# ── Trade Statistics ──────────────────────────────────────────────

def compute_stats(results):
    """Compute summary statistics per combo key.

    Args:
        results: dict of key → list of (pnl, triggered, exit_h, entry_date)

    Returns:
        dict of key → stats dict
    """
    stats = {}
    for key, trades in results.items():
        pnls = [t[0] for t in trades]
        n = len(pnls)
        if n < 1:
            continue
        wins = sum(1 for p in pnls if p > 0)
        trig_count = sum(1 for t in trades if t[1])
        trig_pnls = [t[0] for t in trades if t[1]]
        stats[key] = {
            "n": n,
            "avg_pnl": statistics.mean(pnls),
            "median_pnl": statistics.median(pnls),
            "pnl_stdev": statistics.stdev(pnls) if n >= 2 else 0,
            "win_rate": wins / n,
            "trigger_rate": trig_count / n,
            "total_pnl": sum(pnls),
            "max_loss": min(pnls),
            "max_win": max(pnls),
            "avg_trig_pnl": statistics.mean(trig_pnls) if trig_pnls else None,
        }
    return stats


# ── Equity Curve ──────────────────────────────────────────────────

def compute_equity_metrics(trades, capital=10000):
    """Build daily equity curve and compute professional backtest metrics.

    Args:
        trades: list of (pnl, triggered, exit_h, entry_date) tuples
        capital: starting capital for drawdown calculations

    Returns dict with daily_pnl, cumulative_pnl, total_pnl, max_drawdown,
    max_drawdown_pct, max_consec_wins/losses, profit_factor, avg_win/loss,
    win_loss_ratio, expectancy, sortino, calmar, tail_ratio, recovery_factor, etc.
    """
    if not trades:
        return None

    # Group PnL by entry date
    date_pnl = defaultdict(float)
    for t in trades:
        date_pnl[t[3]] += t[0]

    # Full calendar from first to last date (weekends = $0 flat)
    sorted_trade_dates = sorted(date_pnl.keys())
    first = datetime.strptime(sorted_trade_dates[0], "%Y-%m-%d").date()
    last = datetime.strptime(sorted_trade_dates[-1], "%Y-%m-%d").date()
    all_dates = []
    d = first
    one_day = timedelta(days=1)
    while d <= last:
        ds = d.strftime("%Y-%m-%d")
        all_dates.append((ds, date_pnl.get(ds, 0.0)))
        d += one_day
    daily_pnl = all_dates

    # Cumulative PnL and equity curve
    cum = 0
    cumulative_pnl = []
    equity_curve = []
    for d, pnl in daily_pnl:
        cum += pnl
        cumulative_pnl.append((d, cum))
        equity_curve.append(capital + cum)

    total_pnl = cum

    # Max drawdown
    peak = capital
    max_dd = 0
    max_dd_pct = 0
    peak_equity = capital
    trough_equity = capital
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
            trough_equity = eq
            peak_equity = peak
        dd_pct = dd / peak if peak > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    # Consecutive wins / losses
    pnls_ordered = [pnl for _, pnl in daily_pnl]
    max_cw = 0
    max_cl = 0
    cur_cw = 0
    cur_cl = 0
    for p in pnls_ordered:
        if p > 0:
            cur_cw += 1
            cur_cl = 0
        elif p < 0:
            cur_cl += 1
            cur_cw = 0
        else:
            cur_cw = 0
            cur_cl = 0
        max_cw = max(max_cw, cur_cw)
        max_cl = max(max_cl, cur_cl)

    # Win/loss breakdown
    wins = [p for p in pnls_ordered if p > 0]
    losses = [p for p in pnls_ordered if p < 0]
    gross_wins = sum(wins) if wins else 0
    gross_losses = abs(sum(losses)) if losses else 0
    profit_factor = (gross_wins / gross_losses
                     if gross_losses > 0 else float('inf'))
    avg_win = statistics.mean(wins) if wins else 0
    avg_loss = statistics.mean(losses) if losses else 0
    win_loss_ratio = (avg_win / abs(avg_loss)
                      if avg_loss != 0 else float('inf'))

    n_trades = len(pnls_ordered)
    wr = len(wins) / n_trades if n_trades > 0 else 0
    lr = len(losses) / n_trades if n_trades > 0 else 0
    expectancy = wr * avg_win + lr * avg_loss

    # Sortino ratio (annualised)
    neg_returns = [p for p in pnls_ordered if p < 0]
    if len(neg_returns) >= 2:
        downside_dev = statistics.stdev(neg_returns)
        mean_daily = statistics.mean(pnls_ordered)
        sortino = (mean_daily / downside_dev * math.sqrt(252)
                   if downside_dev > 0 else 0)
    else:
        sortino = 0

    # Calmar ratio
    calmar = total_pnl / max_dd if max_dd > 0 else 0

    # Tail ratio (95th / |5th| percentile)
    if len(pnls_ordered) >= 20:
        sorted_pnls = sorted(pnls_ordered)
        idx_5 = max(0, int(len(sorted_pnls) * 0.05))
        idx_95 = min(len(sorted_pnls) - 1, int(len(sorted_pnls) * 0.95))
        p5 = sorted_pnls[idx_5]
        p95 = sorted_pnls[idx_95]
        tail_ratio = p95 / abs(p5) if p5 != 0 else 0
    else:
        tail_ratio = 0

    recovery_factor = total_pnl / max_dd if max_dd > 0 else 0

    return {
        "daily_pnl": daily_pnl,
        "cumulative_pnl": cumulative_pnl,
        "total_pnl": total_pnl,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "max_consec_wins": max_cw,
        "max_consec_losses": max_cl,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "win_loss_ratio": win_loss_ratio,
        "expectancy": expectancy,
        "sortino": sortino,
        "calmar": calmar,
        "tail_ratio": tail_ratio,
        "peak_equity": peak_equity,
        "trough_equity": trough_equity,
        "recovery_factor": recovery_factor,
        "n_days": len(daily_pnl),
    }


# ── Scoring Helpers ───────────────────────────────────────────────

def percentile_ranks(values):
    """Map a list of values to [0, 1] percentile ranks."""
    sorted_unique = sorted(set(values))
    n = max(len(sorted_unique) - 1, 1)
    rank_map = {v: i / n for i, v in enumerate(sorted_unique)}
    return [rank_map[v] for v in values]


def neighbor_avg_pnl(stats, key, offsets, triggers, max_holds, min_samples=5):
    """Average P&L of parameter-adjacent combos (±1 step per dimension).

    Args:
        stats: dict of key → stats dict
        key: (offset, hour, trigger, max_hold) tuple
        offsets: list of offset values (ordered)
        triggers: list of trigger values (ordered)
        max_holds: list of max_hold values (ordered)
        min_samples: minimum samples to include a neighbor
    """
    offset, hour, trigger, max_hold = key
    off_idx = offsets.index(offset)
    trig_idx = triggers.index(trigger)
    hold_idx = max_holds.index(max_hold)

    neighbor_pnls = []
    for i in [off_idx - 1, off_idx + 1]:
        if 0 <= i < len(offsets):
            nk = (offsets[i], hour, trigger, max_hold)
            if nk in stats and stats[nk]["n"] >= min_samples:
                neighbor_pnls.append(stats[nk]["avg_pnl"])
    for h in [hour - 1, hour + 1]:
        nk = (offset, h, trigger, max_hold)
        if nk in stats and stats[nk]["n"] >= min_samples:
            neighbor_pnls.append(stats[nk]["avg_pnl"])
    for i in [trig_idx - 1, trig_idx + 1]:
        if 0 <= i < len(triggers):
            nk = (offset, hour, triggers[i], max_hold)
            if nk in stats and stats[nk]["n"] >= min_samples:
                neighbor_pnls.append(stats[nk]["avg_pnl"])
    for i in [hold_idx - 1, hold_idx + 1]:
        if 0 <= i < len(max_holds):
            nk = (offset, hour, trigger, max_holds[i])
            if nk in stats and stats[nk]["n"] >= min_samples:
                neighbor_pnls.append(stats[nk]["avg_pnl"])
    return statistics.mean(neighbor_pnls) if neighbor_pnls else 0
