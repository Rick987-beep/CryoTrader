"""
Reporting: console tables and HTML report generation.

Three sections:
    1. Console tables    — print_*() functions for terminal output
    2. Composite scoring — print_robust_selection() with 12 weighted metrics
    3. HTML report       — generate_html() with heatmaps, rankings, explainer

Imports strategy constants from straddle_strangle.py. To support a different
strategy, parameterize those imports or create a strategy-specific reporter.
"""

import statistics
from datetime import datetime, timezone

from pricing import VOL_LOOKBACK, hours_to_expiry
from metrics import compute_equity_metrics, percentile_ranks, neighbor_avg_pnl
from straddle_strangle import (
    OFFSETS, INDEX_TRIGGERS, MAX_HOLDS, SLIPPAGE, MAX_ENTRY_HOUR, MIN_SAMPLES,
    offset_label_short,
)


# ══════════════════════════════════════════════════════════════════
#  Console Output
# ══════════════════════════════════════════════════════════════════

def print_data_summary(candles, entry_spots, entry_vols, meta):
    all_spots = []
    for spots in entry_spots.values():
        all_spots.extend(spots)
    all_vols = []
    for vols in entry_vols.values():
        all_vols.extend(vols)

    dates = sorted(set(c["date"] for c in candles))
    print()
    print("=" * 80)
    print("  0DTE BACKTEST — Black-Scholes + Realized Vol")
    print("  %d weeks | Deribit fees | ±4%% slippage | weekdays only" % meta["weeks"])
    print("=" * 80)
    print("  Data:      %s to %s (%d total days, %d candles)" % (
        dates[0], dates[-1], len(dates), len(candles)))
    print("  Entries:   weekdays only (%d entry days)" % meta["n_entry_days"])
    print("  Pricing:   Black-Scholes (r=0, realized vol from trailing 24h)")
    print("  Slippage:  ±4%% (buy at ×1.04, sell at ×0.96)")
    print("  Fees:      Deribit: MIN(0.03%% × BTC, 12.5%% × leg_price)")
    print("  TP logic:  Sell to close when |BTC - entry_BTC| >= trigger")
    print("  Triggers:  %s" % INDEX_TRIGGERS)

    if all_spots:
        print("  BTC range: $%s – $%s (avg $%s)" % (
            "{:,.0f}".format(min(all_spots)),
            "{:,.0f}".format(max(all_spots)),
            "{:,.0f}".format(statistics.mean(all_spots))))
    if all_vols:
        print("  Vol range: %.0f%% – %.0f%% (avg %.0f%%, median %.0f%%)" % (
            min(all_vols) * 100, max(all_vols) * 100,
            statistics.mean(all_vols) * 100,
            statistics.median(all_vols) * 100))


def print_entry_premiums(entry_premiums, entry_spots, entry_vols):
    print()
    print("=" * 110)
    print("  AVG ENTRY PREMIUMS (Black-Scholes, realized vol)")
    print("  Averaged across all entry days | buy at ×1.04")
    print("=" * 110)

    hours = sorted(set(h for h, _ in entry_premiums.keys()))

    print("  %5s  %5s  %5s  %10s  %9s  %9s  %4s" % (
        "Hour", "DTE", "AvgVol", "Structure", "AvgPrem", "AvgPaid", "n"))
    print("  " + "-" * 70)

    for hour in hours:
        dte = hours_to_expiry(hour)
        avg_vol = statistics.mean(entry_vols.get(hour, [0.55]))
        for offset in OFFSETS:
            prems = entry_premiums.get((hour, offset), [])
            if not prems:
                continue
            avg_p = statistics.mean(prems)
            avg_paid = avg_p * (1 + SLIPPAGE)
            print("  %02d:00  %4.0fh  %4.0f%%  %10s  $%8s  $%8s  %4d" % (
                hour, dte, avg_vol * 100, offset_label_short(offset),
                "{:,.0f}".format(avg_p), "{:,.0f}".format(avg_paid),
                len(prems)))
        if hour != hours[-1]:
            print()


def print_btc_range(btc_ranges):
    print()
    print("=" * 110)
    print("  AVG BTC RANGE (high - low) PER ENTRY HOUR × HOLD WINDOW")
    print("=" * 110)

    hours = sorted(set(h for h, _ in btc_ranges.keys()))
    holds = sorted(set(mh for _, mh in btc_ranges.keys()))

    header = "  %6s  %4s" % ("Entry", "n")
    for mh in holds:
        header += "  %6s" % ("%dh" % mh)
    print(header)
    print("  " + "-" * (12 + 8 * len(holds)))

    for hour in hours:
        n = len(btc_ranges.get((hour, 1), []))
        row = "  %02d:00  %4d" % (hour, n)
        for mh in holds:
            vals = btc_ranges.get((hour, mh), [])
            if vals:
                row += "  $%5.0f" % statistics.mean(vals)
            else:
                row += "  %6s" % "—"
        print(row)


def print_trigger_hit_rate(stats):
    print()
    print("=" * 110)
    print("  TRIGGER HIT RATE — %% of trades where BTC moved enough")
    print("=" * 110)

    hours = sorted(set(k[1] for k in stats.keys()))
    triggers = sorted(set(k[2] for k in stats.keys()))

    header = "  %6s" % "Entry"
    for trig in triggers:
        header += "  %6s" % ("$%d" % trig)
    print(header)
    print("  " + "-" * (7 + 8 * len(triggers)))

    for hour in hours:
        row = "  %02d:00" % hour
        for trig in triggers:
            total_n = 0
            total_trig = 0
            for key, s in stats.items():
                if key[1] == hour and key[2] == trig and s["n"] >= MIN_SAMPLES:
                    total_n += s["n"]
                    total_trig += s["n"] * s["trigger_rate"]
            if total_n > 0:
                row += "  %5.0f%%" % (total_trig / total_n * 100)
            else:
                row += "  %6s" % "—"
        print(row)


def print_avg_pnl_table(stats):
    print()
    print("=" * 110)
    print("  AVG P&L WHEN TRIGGERED — by structure × trigger")
    print("=" * 110)

    triggers = sorted(set(k[2] for k in stats.keys()))

    print("  %10s" % "Structure", end="")
    for trig in triggers:
        print("  %8s" % ("$%d" % trig), end="")
    print()
    print("  " + "-" * (11 + 10 * len(triggers)))

    for offset in OFFSETS:
        row = "  %10s" % offset_label_short(offset)
        for trig in triggers:
            pnls = []
            for key, s in stats.items():
                if key[0] == offset and key[2] == trig:
                    if s["avg_trig_pnl"] is not None:
                        pnls.append(s["avg_trig_pnl"])
            if pnls:
                row += "  $%7.0f" % statistics.mean(pnls)
            else:
                row += "  %8s" % "—"
        print(row)


def print_conversion_efficiency(stats, entry_premiums):
    print()
    print("=" * 110)
    print("  CONVERSION EFFICIENCY — Avg P&L / Avg Entry Premium")
    print("=" * 110)

    triggers = sorted(set(k[2] for k in stats.keys()))

    print("  %10s" % "Structure", end="")
    for trig in triggers:
        print("  %8s" % ("$%d" % trig), end="")
    print()
    print("  " + "-" * (11 + 10 * len(triggers)))

    for offset in OFFSETS:
        row = "  %10s" % offset_label_short(offset)
        for trig in triggers:
            ratios = []
            for entry_h in range(MAX_ENTRY_HOUR + 1):
                prems = entry_premiums.get((entry_h, offset), [])
                if not prems:
                    continue
                avg_prem = statistics.mean(prems)
                if avg_prem < 5:
                    continue
                best = None
                for mh in MAX_HOLDS:
                    key = (offset, entry_h, trig, mh)
                    s = stats.get(key)
                    if s and s["n"] >= MIN_SAMPLES:
                        if best is None or s["avg_pnl"] > best:
                            best = s["avg_pnl"]
                if best is not None:
                    ratios.append(best / avg_prem * 100)
            if ratios:
                row += "  %7.0f%%" % statistics.mean(ratios)
            else:
                row += "  %8s" % "—"
        print(row)


def print_top_combos(stats, entry_premiums, n=30):
    print()
    print("=" * 120)
    print("  TOP %d COMBINATIONS BY TOTAL P&L (min %d samples)" % (n, MIN_SAMPLES))
    print("=" * 120)

    ranked = []
    for key, s in stats.items():
        if s["n"] < MIN_SAMPLES:
            continue
        offset, entry_h, trigger, max_hold = key
        prems = entry_premiums.get((entry_h, offset), [])
        avg_prem = statistics.mean(prems) if prems else 0
        ranked.append({
            "label": offset_label_short(offset),
            "entry_h": entry_h,
            "trigger": trigger,
            "max_hold": max_hold,
            "avg_pnl": s["avg_pnl"],
            "median_pnl": s["median_pnl"],
            "total_pnl": s["total_pnl"],
            "win_rate": s["win_rate"],
            "trig_rate": s["trigger_rate"],
            "n": s["n"],
            "premium": avg_prem,
        })

    ranked.sort(key=lambda x: x["total_pnl"], reverse=True)

    print("  %10s  %6s  %8s  %5s  %10s  %9s  %9s  %6s  %6s  %8s  %4s" % (
        "Structure", "Entry", "Trigger", "MaxH", "Total P&L", "Avg P&L",
        "Med P&L", "Win%", "Trig%", "AvgPrem", "n"))
    print("  " + "-" * 105)

    for r in ranked[:n]:
        print(
            "  %10s  %02d:00  $%6d  %4dh  $%9s  $%8s  $%8s  %5.0f%%  %5.0f%%  $%7s  %4d"
            % (
                r["label"], r["entry_h"], r["trigger"],
                r["max_hold"],
                "{:,.0f}".format(r["total_pnl"]),
                "{:,.0f}".format(r["avg_pnl"]),
                "{:,.0f}".format(r["median_pnl"]),
                r["win_rate"] * 100,
                r["trig_rate"] * 100,
                "{:,.0f}".format(r["premium"]),
                r["n"],
            )
        )
    return ranked


def print_best_per_structure(stats, entry_premiums):
    print()
    print("=" * 80)
    print("  BEST CONFIGURATION PER STRUCTURE (min %d samples)" % MIN_SAMPLES)
    print("=" * 80)

    print("\n  %-15s %6s %8s %5s %9s %6s %6s %8s %4s" % (
        "Structure", "Entry", "Trigger", "Hold", "Avg P&L",
        "Win%", "Trig%", "AvgPrem", "n"))
    print("  " + "-" * 80)

    for offset in OFFSETS:
        best_key = None
        best_avg = -1e18
        for key, s in stats.items():
            if key[0] == offset and s["n"] >= MIN_SAMPLES:
                if s["avg_pnl"] > best_avg:
                    best_avg = s["avg_pnl"]
                    best_key = key
        if best_key:
            off, eh, trig, mh = best_key
            s = stats[best_key]
            prems = entry_premiums.get((eh, off), [])
            avg_prem = statistics.mean(prems) if prems else 0
            label = "ATM straddle" if offset == 0 else "Strangle +/-%d" % offset
            print(
                "  %-15s %02d:00 $%6d %4dh $%8s %5.0f%% %5.0f%% $%7s %4d"
                % (
                    label, eh, trig, mh,
                    "{:,.0f}".format(best_avg),
                    s["win_rate"] * 100,
                    s["trigger_rate"] * 100,
                    "{:,.0f}".format(avg_prem),
                    s["n"],
                )
            )


def print_hourly_summary(stats, entry_spots, entry_vols, entry_premiums):
    print()
    print("=" * 90)
    print("  BEST RESULT PER ENTRY HOUR (min %d samples)" % MIN_SAMPLES)
    print("=" * 90)

    print("\n  %6s %9s %5s  %15s %8s %5s %9s %6s %6s %4s" % (
        "Hour", "Avg BTC", "Vol", "Best Structure", "Trigger", "Hold",
        "Avg P&L", "Win%", "Trig%", "n"))
    print("  " + "-" * 88)

    for hour in range(MAX_ENTRY_HOUR + 1):
        best_key = None
        best_avg = -1e18
        for key, s in stats.items():
            if key[1] == hour and s["n"] >= MIN_SAMPLES:
                if s["avg_pnl"] > best_avg:
                    best_avg = s["avg_pnl"]
                    best_key = key
        if best_key is None:
            continue
        off, eh, trig, mh = best_key
        s = stats[best_key]
        spots = entry_spots.get(hour, [])
        vols = entry_vols.get(hour, [])
        avg_spot = statistics.mean(spots) if spots else 0
        avg_vol = statistics.mean(vols) if vols else 0
        print(
            "  %02d:00 $%8s  %3.0f%%  %15s $%6d %4dh $%8s %5.0f%% %5.0f%% %4d"
            % (
                hour, "{:,.0f}".format(avg_spot),
                avg_vol * 100, offset_label_short(off),
                trig, mh, "{:,.0f}".format(best_avg),
                s["win_rate"] * 100,
                s["trigger_rate"] * 100,
                s["n"],
            )
        )


# ══════════════════════════════════════════════════════════════════
#  Robust Parameter Selection (Composite Scoring)
# ══════════════════════════════════════════════════════════════════

def print_robust_selection(stats, entry_premiums, results):
    """Composite-score ranking. Total P&L 20%, Max DD 15%, etc."""
    print()
    print("=" * 140)
    print("  ROBUST PARAMETER SELECTION — Composite Score Ranking")
    print("  Total P&L 20%% | Max DD 15%% | Median P&L 10%% | Neighbour 10%% | Sharpe 10%%")
    print("  Calmar 5%% | PF 5%% | MaxCL 5%% | WinR 5%% | AvgPnL 5%% | TrigR 5%% | ConvEff 5%%")
    print("=" * 140)

    MIN_AVG_PNL = 25

    candidates = []
    for key, s in stats.items():
        if s["n"] < MIN_SAMPLES or s["avg_pnl"] < MIN_AVG_PNL:
            continue
        offset, entry_h, trigger, max_hold = key
        prems = entry_premiums.get((entry_h, offset), [])
        avg_prem_paid = statistics.mean(prems) * (1 + SLIPPAGE) if prems else 1
        stdev = s["pnl_stdev"]
        sharpe = s["avg_pnl"] / stdev if stdev > 0 else 0
        conv_eff = s["avg_pnl"] / avg_prem_paid if avg_prem_paid > 0 else 0
        nbr = neighbor_avg_pnl(stats, key, OFFSETS, INDEX_TRIGGERS,
                               MAX_HOLDS, MIN_SAMPLES)
        be_slip = s["avg_pnl"] / (2 * avg_prem_paid) if avg_prem_paid > 0 else 0

        trades = results.get(key, [])
        em = compute_equity_metrics(trades)
        calmar = em["calmar"] if em else 0
        pf = em["profit_factor"] if em else 0
        if pf == float('inf'):
            pf = 50.0
        mcl = em["max_consec_losses"] if em else 99
        mcl_inv = -mcl
        eq_total_pnl = em["total_pnl"] if em else 0
        eq_max_dd = em["max_drawdown"] if em else 9999
        max_dd_inv = -eq_max_dd

        candidates.append({
            "key": key,
            "avg_pnl": s["avg_pnl"],
            "median_pnl": s["median_pnl"],
            "total_pnl": eq_total_pnl,
            "max_drawdown": eq_max_dd,
            "max_dd_inv": max_dd_inv,
            "win_rate": s["win_rate"],
            "trigger_rate": s["trigger_rate"],
            "sharpe": sharpe,
            "conv_eff": conv_eff,
            "nbr_avg": nbr,
            "stdev": stdev,
            "n": s["n"],
            "max_loss": s["max_loss"],
            "max_win": s["max_win"],
            "premium": avg_prem_paid,
            "be_slip": be_slip,
            "calmar": calmar,
            "profit_factor": pf,
            "mcl_inv": mcl_inv,
            "max_consec_losses": mcl,
            "equity_metrics": em,
        })

    if not candidates:
        print("  No candidates meet minimum criteria.")
        return

    print("  Candidates: %d combos with avg P&L >= $%d and n >= %d"
          % (len(candidates), MIN_AVG_PNL, MIN_SAMPLES))

    metrics = ["total_pnl", "max_dd_inv", "median_pnl", "nbr_avg",
               "sharpe", "calmar", "profit_factor", "mcl_inv",
               "win_rate", "avg_pnl", "trigger_rate", "conv_eff"]
    for m in metrics:
        vals = [c[m] for c in candidates]
        ranks = percentile_ranks(vals)
        for c, r in zip(candidates, ranks):
            c["rank_" + m] = r

    weights = {
        "total_pnl":      0.20,
        "max_dd_inv":     0.15,
        "median_pnl":     0.10,
        "nbr_avg":        0.10,
        "sharpe":         0.10,
        "calmar":         0.05,
        "profit_factor":  0.05,
        "mcl_inv":        0.05,
        "win_rate":       0.05,
        "avg_pnl":        0.05,
        "trigger_rate":   0.05,
        "conv_eff":       0.05,
    }
    for c in candidates:
        c["score"] = sum(weights[m] * c["rank_" + m] for m in weights)

    candidates.sort(key=lambda c: c["score"], reverse=True)

    # Top 20
    print()
    hdr = ("  %3s  %10s  %5s  %7s  %4s  %6s  %10s  %9s"
           "  %7s  %7s  %5s  %5s  %6s  %5s  %3s  %7s  %4s")
    print(hdr % (
        "#", "Structure", "Entry", "Trigger", "Hold",
        "Score", "TotalPnL", "MaxDD",
        "AvgPnL", "MedPnL", "Win%%",
        "Shpe", "Calmr", "PF",
        "CL", "BEslip", "n"))
    print("  " + "-" * 135)

    for i, c in enumerate(candidates[:20]):
        offset, hour, trigger, max_hold = c["key"]
        pf_str = "%.1f" % c["profit_factor"]
        print(
            "  %3d  %10s  %02d:00  $%5d  %3dh  %5.3f  $%9s  $%8s"
            "  $%6.0f  $%6.0f  %4.0f%%  %4.2f  %5.1f  %4s  %3d   %4.1f%%  %3d"
            % (
                i + 1, offset_label_short(offset), hour, trigger, max_hold,
                c["score"],
                "{:,.0f}".format(c["total_pnl"]),
                "{:,.0f}".format(c["max_drawdown"]),
                c["avg_pnl"], c["median_pnl"],
                c["win_rate"] * 100,
                c["sharpe"],
                c["calmar"], pf_str,
                c["max_consec_losses"],
                c["be_slip"] * 100, c["n"],
            )
        )

    # Neighbourhood analysis for #1
    top = candidates[0]
    offset, hour, trigger, max_hold = top["key"]
    print()
    print("  " + chr(9472) * 100)
    print("  NEIGHBOURHOOD ANALYSIS: %s @ %02d:00, $%d trigger, %dh hold"
          % (offset_label_short(offset), hour, trigger, max_hold))
    print("  " + chr(9472) * 100)

    def _show_axis(label, keys_labels):
        items = []
        for nk, lbl, is_pick in keys_labels:
            s = stats.get(nk)
            if s and s["n"] >= MIN_SAMPLES:
                mark = " <<" if is_pick else ""
                items.append("    %12s: $%6.0f avg, %4.0f%% win, "
                             "%4.0f%% trig%s" % (
                                 lbl, s["avg_pnl"],
                                 s["win_rate"] * 100,
                                 s["trigger_rate"] * 100, mark))
        if items:
            print("  %s:" % label)
            for item in items:
                print(item)

    off_idx = OFFSETS.index(offset)
    kl = []
    for i in range(max(0, off_idx - 2), min(len(OFFSETS), off_idx + 3)):
        nk = (OFFSETS[i], hour, trigger, max_hold)
        kl.append((nk, offset_label_short(OFFSETS[i]), OFFSETS[i] == offset))
    _show_axis("Structure", kl)

    kl = []
    for h in range(max(0, hour - 2), min(MAX_ENTRY_HOUR + 1, hour + 3)):
        nk = (offset, h, trigger, max_hold)
        kl.append((nk, "%02d:00" % h, h == hour))
    _show_axis("Entry hour", kl)

    trig_idx = INDEX_TRIGGERS.index(trigger)
    kl = []
    for i in range(max(0, trig_idx - 2),
                   min(len(INDEX_TRIGGERS), trig_idx + 3)):
        nk = (offset, hour, INDEX_TRIGGERS[i], max_hold)
        kl.append((nk, "$%d" % INDEX_TRIGGERS[i],
                   INDEX_TRIGGERS[i] == trigger))
    _show_axis("Trigger", kl)

    hold_idx = MAX_HOLDS.index(max_hold)
    kl = []
    for i in range(max(0, hold_idx - 2),
                   min(len(MAX_HOLDS), hold_idx + 3)):
        nk = (offset, hour, trigger, MAX_HOLDS[i])
        kl.append((nk, "%dh" % MAX_HOLDS[i], MAX_HOLDS[i] == max_hold))
    _show_axis("Max hold", kl)

    # Final recommendation
    print()
    print("  " + chr(9552) * 100)
    print("  RECOMMENDATION")
    print("  " + chr(9552) * 100)
    print()
    print("  Config:  %s | %02d:00 UTC | $%d trigger | %dh max hold"
          % (offset_label_short(offset), hour, trigger, max_hold))
    print()
    print("  Per-trade expectations:")
    print("    Premium (incl 4%% slip):  $%s" % "{:,.0f}".format(top["premium"]))
    print("    Avg P&L:                 $%s" % "{:,.0f}".format(top["avg_pnl"]))
    print("    Median P&L:              $%s" % "{:,.0f}".format(top["median_pnl"]))
    print("    Std dev:                 $%s" % "{:,.0f}".format(top["stdev"]))
    print("    Win rate:                %.0f%%" % (top["win_rate"] * 100))
    print("    Trigger rate:            %.0f%%" % (top["trigger_rate"] * 100))
    print("    Worst trade:             $%s" % "{:,.0f}".format(top["max_loss"]))
    print("    Best trade:              $%s" % "{:,.0f}".format(top["max_win"]))
    print("    Breakeven addl slippage: %.1f%% (buffer above modelled 4%%)" %
          (top["be_slip"] * 100))
    print()
    annual = top["avg_pnl"] * 252
    print("  Annualised estimate (1 trade/weekday):")
    print("    ~$%s/year    capital at risk = $%s/trade" %
          ("{:,.0f}".format(annual), "{:,.0f}".format(top["premium"])))

    return candidates


# ══════════════════════════════════════════════════════════════════
#  Equity Curve Deep Dive
# ══════════════════════════════════════════════════════════════════

def print_equity_deep_dive(results, candidates, n=20):
    """Equity curve deep-dive for top N candidates."""
    print()
    print("=" * 130)
    print("  EQUITY CURVE DEEP DIVE — Top %d Combos ($10,000 capital base)"
          % n)
    print("=" * 130)

    top_n = candidates[:n]

    print()
    print("  %3s  %10s  %5s  %7s  %4s  %10s  %9s  %9s  %5s  %6s  %5s"
          "  %5s  %6s  %6s  %5s  %4s" % (
              "#", "Structure", "Entry", "Trigger", "Hold",
              "TotalPnL", "MaxDD", "MaxDD%",
              "CW", "CL", "PF",
              "W/L", "Sorti", "Calmr", "Tail", "Days"))
    print("  " + "-" * 126)

    equity_data = []
    for i, c in enumerate(top_n):
        key = c["key"]
        trades = results.get(key, [])
        em = compute_equity_metrics(trades)
        if em is None:
            continue

        offset, hour, trigger, max_hold = key
        pf_str = ("%.1f" % em["profit_factor"]
                  if em["profit_factor"] != float('inf') else "inf")

        print(
            "  %3d  %10s  %02d:00  $%5d  %3dh  $%9s  $%8s   %5.1f%%"
            "  %3d    %3d  %4s  %4.1f  %5.2f  %5.1f  %4.1f  %4d"
            % (
                i + 1, offset_label_short(offset), hour, trigger, max_hold,
                "{:,.0f}".format(em["total_pnl"]),
                "{:,.0f}".format(em["max_drawdown"]),
                em["max_drawdown_pct"] * 100,
                em["max_consec_wins"],
                em["max_consec_losses"],
                pf_str,
                em["win_loss_ratio"] if em["win_loss_ratio"] != float('inf')
                else 0,
                em["sortino"],
                em["calmar"],
                em["tail_ratio"],
                em["n_days"],
            )
        )
        equity_data.append({"key": key, "rank": i + 1, "metrics": em})

    # Detailed daily PnL for #1
    if equity_data:
        top_eq = equity_data[0]
        em = top_eq["metrics"]
        offset, hour, trigger, max_hold = top_eq["key"]
        print()
        print("  " + chr(9472) * 120)
        print("  DAILY P&L — #1: %s @ %02d:00, $%d trigger, %dh hold"
              % (offset_label_short(offset), hour, trigger, max_hold))
        print("  " + chr(9472) * 120)
        print()
        print("    %-12s  %9s  %11s  %8s" % (
            "Date", "Day P&L", "Cumulative", "Equity"))
        print("    " + "-" * 45)

        capital = 10000
        for d, cum in em["cumulative_pnl"]:
            day_pnl = dict(em["daily_pnl"])[d]
            eq = capital + cum
            marker = ""
            if day_pnl > 0:
                marker = " +"
            elif day_pnl < 0:
                marker = " -"
            print("    %-12s  $%8s  $%10s  $%7s%s" % (
                d,
                "{:,.0f}".format(day_pnl),
                "{:,.0f}".format(cum),
                "{:,.0f}".format(eq),
                marker,
            ))

        print()
        print("    Summary:")
        print("      Total P&L:          $%s" % "{:,.0f}".format(em["total_pnl"]))
        print("      Max drawdown:       $%s (%.1f%%)" % (
            "{:,.0f}".format(em["max_drawdown"]),
            em["max_drawdown_pct"] * 100))
        print("      Peak equity:        $%s" % "{:,.0f}".format(em["peak_equity"]))
        print("      Trough equity:      $%s" % "{:,.0f}".format(em["trough_equity"]))
        print("      Consec wins/losses: %d / %d" % (
            em["max_consec_wins"], em["max_consec_losses"]))
        pf_str = ("%.2f" % em["profit_factor"]
                  if em["profit_factor"] != float('inf') else "inf")
        print("      Profit factor:      %s" % pf_str)
        print("      Avg win:            $%s" % "{:,.0f}".format(em["avg_win"]))
        print("      Avg loss:           $%s" % "{:,.0f}".format(em["avg_loss"]))
        print("      Expectancy:         $%s" % "{:,.0f}".format(em["expectancy"]))
        print("      Sortino (ann.):     %.2f" % em["sortino"])
        print("      Calmar:             %.1f" % em["calmar"])
        print("      Tail ratio:         %.1f" % em["tail_ratio"])
        print("      Recovery factor:    %.1f" % em["recovery_factor"])

    return equity_data


# ══════════════════════════════════════════════════════════════════
#  HTML Report
# ══════════════════════════════════════════════════════════════════

def generate_html(stats, btc_ranges, entry_spots, entry_vols,
                  entry_premiums, meta, results=None):
    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
           max-width: 1600px; margin: 20px auto; padding: 0 20px; background: #fafafa; color: #222; }
    h1 { border-bottom: 3px solid #333; padding-bottom: 10px; }
    h2 { margin-top: 40px; color: #333; }
    h3 { margin-top: 25px; color: #555; }
    .subtitle { color: #666; margin-top: -10px; font-style: italic; }
    .meta { background: #eef; padding: 12px 18px; border-radius: 6px; margin: 16px 0; }
    .meta span { margin-right: 30px; }
    .summary-box { background: #e8f5e9; border: 2px solid #4caf50; border-radius: 8px;
                   padding: 16px 24px; margin: 20px 0; }
    .summary-box h3 { margin: 0 0 8px; color: #2e7d32; }
    .summary-box p { margin: 4px 0; font-size: 15px; }
    .cost-box { background: #fff3e0; border: 2px solid #ff9800; border-radius: 8px;
                padding: 16px 24px; margin: 20px 0; }
    .cost-box h3 { margin: 0 0 8px; color: #e65100; }
    .logic-box { background: #e3f2fd; border: 2px solid #1976d2; border-radius: 8px;
                 padding: 16px 24px; margin: 20px 0; }
    .logic-box h3 { margin: 0 0 8px; color: #0d47a1; }
    .note-box { background: #f3e5f5; border: 2px solid #7b1fa2; border-radius: 8px;
                padding: 16px 24px; margin: 20px 0; }
    .note-box h3 { margin: 0 0 8px; color: #4a148c; }
    .table-wrap { overflow-x: auto; }
    table { border-collapse: collapse; font-size: 13px; margin: 10px 0 30px; }
    th, td { padding: 5px 8px; text-align: right; border: 1px solid #ccc; white-space: nowrap; }
    th { background: #333; color: #fff; font-weight: 600; position: sticky; top: 0; }
    .entry-col { text-align: left; font-weight: 600; background: #f0f0f0 !important;
                 color: #333 !important; min-width: 55px; }
    .empty { color: #bbb; background: #f8f8f8; }
    table.ranked { font-size: 14px; }
    table.ranked td { text-align: center; }
    table.ranked tr:nth-child(2) td { background: #fff9c4; font-weight: 700; }
    table.ranked tr:nth-child(3) td { background: #fff9c4; }
    table.ranked tr:nth-child(4) td { background: #fff9c4; }
    .positive { color: #2e7d32; font-weight: 600; }
    .negative { color: #c62828; }
    .rec-box { background: #e0f2f1; border: 2px solid #00897b; border-radius: 8px;
               padding: 16px 24px; margin: 20px 0; }
    .rec-box h3 { margin: 0 0 8px; color: #004d40; }
    .rec-box .config { font-size: 18px; font-weight: 700; color: #00695c; margin: 10px 0; }
    .rec-box .metric { display: inline-block; margin: 3px 18px 3px 0; }
    .rec-box .metric-label { color: #555; font-size: 12px; }
    .rec-box .metric-value { font-size: 16px; font-weight: 600; }
    .explainer { background: #fff; border: 1px solid #ddd; border-radius: 8px;
                 padding: 20px 28px; margin: 20px 0; line-height: 1.6; }
    .explainer h2 { margin-top: 0; color: #333; }
    .explainer h4 { margin: 14px 0 4px; color: #444; }
    .explainer ol, .explainer ul { margin: 4px 0 10px; }
    .explainer code { background: #f0f0f0; padding: 1px 5px; border-radius: 3px; font-size: 13px; }
    """

    def heatmap_color(val, vmin, vmax):
        if val is None or vmax == vmin:
            return "#f8f8f8"
        t = (val - vmin) / (vmax - vmin)
        if t < 0.5:
            r, g = 255, int(255 * (t * 2))
        else:
            r, g = int(255 * (2 - t * 2)), 255
        return "rgb(%d,%d,80)" % (r, g)

    def pnl_class(v):
        if v > 0:
            return "positive"
        if v < 0:
            return "negative"
        return ""

    def build_heatmap(title, subtitle, row_labels, col_labels, data_fn, fmt="$"):
        all_vals = []
        for r in row_labels:
            for c in col_labels:
                v = data_fn(r, c)
                if v is not None:
                    all_vals.append(v)
        if not all_vals:
            return ""
        vmin, vmax = min(all_vals), max(all_vals)
        rows = ['<h3>%s</h3>' % title]
        if subtitle:
            rows.append('<p class="subtitle">%s</p>' % subtitle)
        rows.append('<div class="table-wrap"><table>')
        rows.append('<tr><th class="entry-col"></th>')
        for c in col_labels:
            rows.append('<th>%s</th>' % c)
        rows.append('</tr>')
        for r in row_labels:
            rows.append('<tr><td class="entry-col">%s</td>' % r)
            for c in col_labels:
                v = data_fn(r, c)
                if v is not None:
                    bg = heatmap_color(v, vmin, vmax)
                    if fmt == "$":
                        cell = '$%s' % "{:,.0f}".format(v)
                    elif fmt == "%":
                        cell = '%.0f%%' % v
                    else:
                        cell = str(v)
                    rows.append(
                        '<td style="background:%s">%s</td>' % (bg, cell))
                else:
                    rows.append('<td class="empty">&mdash;</td>')
            rows.append('</tr>')
        rows.append('</table></div>')
        return '\n'.join(rows)

    # Gather metadata
    all_spots = []
    for spots in entry_spots.values():
        all_spots.extend(spots)
    all_vols = []
    for vols in entry_vols.values():
        all_vols.extend(vols)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hours_with_data = sorted(entry_spots.keys())
    triggers = sorted(set(k[2] for k in stats.keys()))

    parts = []
    parts.append("""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>0DTE Backtest &mdash; Black-Scholes + Realized Vol</title>
<style>%s</style>
</head><body>
<h1>0DTE Backtest &mdash; Black-Scholes + Realized Vol</h1>
<div class="meta">
  <span><strong>Generated:</strong> %s</span>
  <span><strong>Data:</strong> %d weeks of Binance 1h candles</span>
  <span><strong>Date range:</strong> %s to %s</span>
  <span><strong>Entry days:</strong> %d (weekdays)</span>
</div>
""" % (css, now, meta["weeks"],
       meta["date_range"][0], meta["date_range"][1], meta["n_entry_days"]))

    # Strategy explainer
    parts.append("""
<div class="explainer">
  <h2>How This Strategy Works</h2>
  <p>This backtest evaluates <strong>buying short-dated (0DTE) BTC option structures</strong>
     (long straddles/strangles) and exiting based on an <strong>underlying index move
     trigger</strong>, rather than a fixed dollar P&amp;L target.</p>

  <h4>1. Entry</h4>
  <ul>
    <li>At a given UTC hour, buy an option structure (ATM straddle or OTM strangle
        at offsets &plusmn;$500 to &plusmn;$3000) expiring the next day at 08:00 UTC.</li>
    <li>Strikes snap to Deribit&rsquo;s $500 grid: e.g. BTC at $70,300 &rarr;
        call strike $70,500, put strike $70,000.</li>
    <li>Entry premium = Black-Scholes per leg &times; 1.04 (4% buy slippage).</li>
  </ul>

  <h4>2. Take-Profit Trigger</h4>
  <ul>
    <li>Monitor the BTC index. When <code>|BTC_now &minus; BTC_entry| &ge; trigger</code>,
        the trade is closed.</li>
    <li>The trigger captures the insight that we are trading the underlying excursion &mdash;
        a large index move makes the winning (ITM) leg gain more than what the losing (OTM)
        leg lost, netting a profit on the structure.</li>
    <li>Triggers tested: $300 to $2,000.</li>
  </ul>

  <h4>3. Exit</h4>
  <ul>
    <li>If the trigger fires within <code>max_hold</code> hours: sell to close at BS price
        of each leg at the new spot, original strikes, remaining DTE, same vol
        &times; 0.96 (sell slippage).</li>
    <li>If the trigger does <em>not</em> fire: forced sell-to-close at the max hold hour
        (typically the losing case &mdash; theta has eaten into premium without enough
        directional move).</li>
    <li>Deribit fees: <code>MIN(0.03% &times; BTC, 12.5% &times; leg_price)</code> per leg per trade.</li>
  </ul>

  <h4>What Is Being Modelled</h4>
  <ul>
    <li><strong>Structures:</strong> 7 &mdash; ATM straddle, strangles at &plusmn;$500/$1000/$1500/$2000/$2500/$3000</li>
    <li><strong>Entry hours:</strong> 00:00&ndash;20:00 UTC (weekdays only)</li>
    <li><strong>Index triggers:</strong> 10 values ($300&ndash;$2000)</li>
    <li><strong>Max hold:</strong> 1&ndash;12 hours</li>
    <li><strong>Total parameter combos:</strong> 7 &times; 21 &times; 10 &times; 12 = 17,640</li>
    <li><strong>Pricing:</strong> Black-Scholes (r=0) with realized vol from trailing 24h hourly candles</li>
    <li><strong>Vol:</strong> Annualised from hourly log returns (&radic;8760), clamped 15%&ndash;300%</li>
  </ul>
</div>
""")

    # Logic box
    parts.append("""
<div class="logic-box">
  <h3>Pricing: Black-Scholes with Realized Vol</h3>
  <p><strong>Model:</strong> BS call/put pricing (r=0) with annualized vol from trailing %dh candles</p>
  <p><strong>Entry:</strong> BS(spot, K, T_entry, &sigma;) &times; 1.04 (slippage)</p>
  <p><strong>Exit:</strong> BS(exit_spot, K_original, T_remaining, &sigma;) &times; 0.96 (slippage)</p>
  <p><strong>Key improvement:</strong> Each leg priced separately &mdash; proper moneyness at exit,
     vol-regime awareness, BTC-level scaling</p>
  <p><strong>TP trigger:</strong> Sell when |BTC &minus; entry_BTC| &ge; trigger</p>
  <p><strong>Triggers tested:</strong> %s</p>
</div>
""" % (VOL_LOOKBACK, ", ".join("$%d" % t for t in INDEX_TRIGGERS)))

    if all_spots and all_vols:
        parts.append("""
<div class="summary-box">
  <h3>Market Conditions (%d weeks)</h3>
  <p><strong>BTC range:</strong> $%s &ndash; $%s (avg $%s)</p>
  <p><strong>Realized vol:</strong> %.0f%% &ndash; %.0f%% (avg %.0f%%, median %.0f%%)</p>
  <p><strong>Entry window:</strong> 00:00&ndash;%02d:00 UTC (weekdays only)</p>
  <p><strong>Samples per combo:</strong> ~%d (min %d for display)</p>
</div>
""" % (meta["weeks"],
       "{:,.0f}".format(min(all_spots)), "{:,.0f}".format(max(all_spots)),
       "{:,.0f}".format(statistics.mean(all_spots)),
       min(all_vols) * 100, max(all_vols) * 100,
       statistics.mean(all_vols) * 100, statistics.median(all_vols) * 100,
       MAX_ENTRY_HOUR, meta["n_entry_days"], MIN_SAMPLES))

    parts.append("""
<div class="cost-box">
  <h3>Cost Model</h3>
  <p><strong>Premiums:</strong> Black-Scholes per leg (r=0, &sigma; from trailing %dh candles)</p>
  <p><strong>Slippage:</strong> &plusmn;4%% &mdash; buy at BS &times; 1.04, sell at BS &times; 0.96</p>
  <p><strong>Fees:</strong> Deribit: MIN(0.03%% &times; BTC, 12.5%% &times; leg_price) per leg per trade</p>
  <p><strong>P&amp;L:</strong> exit_received &minus; entry_paid &minus; total_fees</p>
</div>
""" % VOL_LOOKBACK)

    parts.append("""
<div class="note-box">
  <h3>Model Notes</h3>
  <p>Uses <strong>flat vol</strong> across all strikes (no smile). OTM options in reality
     trade at higher IV than ATM, so strangles may be slightly underpriced at entry.</p>
  <p>&sigma; is held constant during each trade (entry vol used for exit pricing).
     In reality IV can shift intraday.</p>
  <p>Weekend candles included for hold-period walk-forward, but entries are weekday-only.</p>
</div>
""")

    # ── Composite Ranking (top 5) ──
    comp_candidates = []
    for key, s in stats.items():
        if s["n"] < MIN_SAMPLES or s["avg_pnl"] < 25:
            continue
        offset, entry_h, trigger, max_hold = key
        prems = entry_premiums.get((entry_h, offset), [])
        avg_prem_paid = (statistics.mean(prems) * (1 + SLIPPAGE)
                         if prems else 1)
        stdev = s["pnl_stdev"]
        sharpe = s["avg_pnl"] / stdev if stdev > 0 else 0
        conv_eff = s["avg_pnl"] / avg_prem_paid if avg_prem_paid > 0 else 0
        nbr = neighbor_avg_pnl(stats, key, OFFSETS, INDEX_TRIGGERS,
                               MAX_HOLDS, MIN_SAMPLES)
        be_slip = (s["avg_pnl"] / (2 * avg_prem_paid)
                   if avg_prem_paid > 0 else 0)

        if results is not None:
            trades = results.get(key, [])
            em = compute_equity_metrics(trades)
            calmar = em["calmar"] if em else 0
            pf = em["profit_factor"] if em else 0
            if pf == float('inf'):
                pf = 50.0
            mcl = em["max_consec_losses"] if em else 99
            eq_total_pnl = em["total_pnl"] if em else 0
            eq_max_dd = em["max_drawdown"] if em else 9999
        else:
            calmar = 0
            pf = 0
            mcl = 99
            eq_total_pnl = 0
            eq_max_dd = 9999
        mcl_inv = -mcl
        max_dd_inv = -eq_max_dd

        comp_candidates.append({
            "key": key,
            "avg_pnl": s["avg_pnl"],
            "median_pnl": s["median_pnl"],
            "total_pnl": eq_total_pnl,
            "max_drawdown": eq_max_dd,
            "max_dd_inv": max_dd_inv,
            "win_rate": s["win_rate"],
            "trigger_rate": s["trigger_rate"],
            "sharpe": sharpe,
            "conv_eff": conv_eff,
            "nbr_avg": nbr,
            "stdev": stdev,
            "n": s["n"],
            "max_loss": s["max_loss"],
            "max_win": s["max_win"],
            "premium": avg_prem_paid,
            "be_slip": be_slip,
            "calmar": calmar,
            "profit_factor": pf,
            "mcl_inv": mcl_inv,
        })

    if comp_candidates:
        comp_metrics = ["total_pnl", "max_dd_inv", "median_pnl",
                        "nbr_avg", "sharpe", "calmar",
                        "profit_factor", "mcl_inv",
                        "win_rate", "avg_pnl",
                        "trigger_rate", "conv_eff"]
        for m in comp_metrics:
            vals = [c[m] for c in comp_candidates]
            ranks = percentile_ranks(vals)
            for c, r in zip(comp_candidates, ranks):
                c["rank_" + m] = r
        comp_weights = {
            "total_pnl": 0.20, "max_dd_inv": 0.15,
            "median_pnl": 0.10, "nbr_avg": 0.10, "sharpe": 0.10,
            "calmar": 0.05, "profit_factor": 0.05, "mcl_inv": 0.05,
            "win_rate": 0.05, "avg_pnl": 0.05,
            "trigger_rate": 0.05, "conv_eff": 0.05,
        }
        for c in comp_candidates:
            c["score"] = sum(
                comp_weights[m] * c["rank_" + m] for m in comp_weights)
        comp_candidates.sort(key=lambda c: c["score"], reverse=True)

        top = comp_candidates[0]
        t_off, t_hr, t_trig, t_mh = top["key"]
        annual = top["avg_pnl"] * 252
        parts.append("""
<div class="rec-box">
  <h3>Recommended Configuration (Composite Score)</h3>
  <p style="font-size:13px;color:#555;">Scored by: Total P&amp;L 20%% | Max Drawdown 15%% |
     Median P&amp;L 10%% | Neighbour 10%% | Sharpe 10%% |
     Calmar 5%% | Profit Factor 5%% | Max Consec Loss 5%% |
     Win Rate 5%% | Avg P&amp;L 5%% | Trigger Rate 5%% | Conv.&thinsp;Eff 5%%</p>
  <p class="config">%s &nbsp;|&nbsp; %02d:00 UTC &nbsp;|&nbsp;
     $%d trigger &nbsp;|&nbsp; %dh max hold</p>
  <div>
    <span class="metric"><span class="metric-label">Premium:</span>
      <span class="metric-value">$%s</span></span>
    <span class="metric"><span class="metric-label">Avg P&amp;L:</span>
      <span class="metric-value">$%s</span></span>
    <span class="metric"><span class="metric-label">Median P&amp;L:</span>
      <span class="metric-value">$%s</span></span>
    <span class="metric"><span class="metric-label">Std Dev:</span>
      <span class="metric-value">$%s</span></span>
    <span class="metric"><span class="metric-label">Win Rate:</span>
      <span class="metric-value">%.0f%%%%</span></span>
    <span class="metric"><span class="metric-label">Trigger Rate:</span>
      <span class="metric-value">%.0f%%%%</span></span>
    <span class="metric"><span class="metric-label">Sharpe:</span>
      <span class="metric-value">%.2f</span></span>
    <span class="metric"><span class="metric-label">Worst:</span>
      <span class="metric-value">$%s</span></span>
    <span class="metric"><span class="metric-label">Best:</span>
      <span class="metric-value">$%s</span></span>
    <span class="metric"><span class="metric-label">BE addl slip:</span>
      <span class="metric-value">%.1f%%%%</span></span>
    <span class="metric"><span class="metric-label">~Annual (1/day):</span>
      <span class="metric-value">$%s</span></span>
  </div>
</div>
""" % (
            offset_label_short(t_off), t_hr, t_trig, t_mh,
            "{:,.0f}".format(top["premium"]),
            "{:,.0f}".format(top["avg_pnl"]),
            "{:,.0f}".format(top["median_pnl"]),
            "{:,.0f}".format(top["stdev"]),
            top["win_rate"] * 100,
            top["trigger_rate"] * 100,
            top["sharpe"],
            "{:,.0f}".format(top["max_loss"]),
            "{:,.0f}".format(top["max_win"]),
            top["be_slip"] * 100,
            "{:,.0f}".format(annual),
        ))

        # Top 20 table
        parts.append('<h2>Top 20 Configurations (Composite Score)</h2>')
        parts.append('<p class="subtitle">Filtered: avg P&amp;L &ge; $25, '
                     'n &ge; %d. Percentile-ranked, weighted.</p>'
                     % MIN_SAMPLES)
        parts.append('<table class="ranked">')
        parts.append(
            '<tr><th>#</th><th>Structure</th><th>Entry</th>'
            '<th>Trigger</th><th>Hold</th><th>Score</th>'
            '<th>Total P&amp;L</th><th>Max DD</th>'
            '<th>Avg P&amp;L</th><th>Med P&amp;L</th>'
            '<th>Win%%</th><th>Sharpe</th>'
            '<th>Calmar</th><th>n</th></tr>')
        for i, c in enumerate(comp_candidates[:20], 1):
            off, hr, trig, mh = c["key"]
            parts.append(
                '<tr><td>%d</td><td>%s</td><td>%02d:00</td>'
                '<td>$%d</td><td>%dh</td><td>%.3f</td>'
                '<td class="%s">$%s</td>'
                '<td>$%s</td>'
                '<td class="%s">$%s</td>'
                '<td class="%s">$%s</td>'
                '<td>%.0f%%</td><td>%.2f</td>'
                '<td>%.1f</td><td>%d</td></tr>' % (
                    i, offset_label_short(off), hr, trig, mh,
                    c["score"],
                    pnl_class(c["total_pnl"]),
                    "{:,.0f}".format(c["total_pnl"]),
                    "{:,.0f}".format(c["max_drawdown"]),
                    pnl_class(c["avg_pnl"]),
                    "{:,.0f}".format(c["avg_pnl"]),
                    pnl_class(c["median_pnl"]),
                    "{:,.0f}".format(c["median_pnl"]),
                    c["win_rate"] * 100,
                    c["sharpe"],
                    c["calmar"], c["n"],
                ))
        parts.append('</table>')

    # BTC Range heatmap
    range_hours = sorted(set(h for h, _ in btc_ranges.keys()))
    range_holds = sorted(set(mh for _, mh in btc_ranges.keys()))

    def range_fn(row, col):
        h = int(row.split(":")[0])
        mh = int(col.replace("h", ""))
        vals = btc_ranges.get((h, mh), [])
        return statistics.mean(vals) if vals else None

    parts.append('<h2>Avg BTC Range (Available Excursion)</h2>')
    parts.append(build_heatmap(
        "Avg BTC Range ($)", "high &minus; low during each window",
        ["%02d:00" % h for h in range_hours],
        ["%dh" % h for h in range_holds],
        range_fn))

    # Trigger hit rate
    parts.append('<h2>Trigger Hit Rate</h2>')

    def trigger_hit_fn(row, col):
        h = int(row.split(":")[0])
        trig = int(col.replace("$", "").replace(",", ""))
        total_n = 0
        total_trig = 0
        for key, s in stats.items():
            if key[1] == h and key[2] == trig and s["n"] >= MIN_SAMPLES:
                total_n += s["n"]
                total_trig += s["n"] * s["trigger_rate"]
        if total_n > 0:
            return total_trig / total_n * 100
        return None

    parts.append(build_heatmap(
        "Trigger Hit Rate", "% of trades where BTC moved enough",
        ["%02d:00" % h for h in hours_with_data],
        ["$%d" % t for t in triggers],
        trigger_hit_fn, fmt="%"))

    # P&L heatmaps per structure: Entry x Trigger
    parts.append('<h2>Avg P&amp;L Heatmaps: Entry Hour &times; Trigger</h2>')
    parts.append(
        '<p class="subtitle">Best avg P&amp;L across max holds</p>')

    for offset in OFFSETS:
        label = ("ATM Straddle" if offset == 0
                 else "Strangle &plusmn;$%d" % offset)

        def make_entry_trig_fn(off):
            def fn(row, col):
                h = int(row.split(":")[0])
                trig = int(col.replace("$", "").replace(",", ""))
                best = None
                for mh in MAX_HOLDS:
                    key = (off, h, trig, mh)
                    s = stats.get(key)
                    if s and s["n"] >= MIN_SAMPLES:
                        if best is None or s["avg_pnl"] > best:
                            best = s["avg_pnl"]
                return best
            return fn

        parts.append(build_heatmap(
            label, "Best avg P&amp;L across holds",
            ["%02d:00" % h for h in hours_with_data],
            ["$%d" % t for t in triggers],
            make_entry_trig_fn(offset)))

    # P&L heatmaps: Entry x MaxHold
    parts.append('<h2>Avg P&amp;L Heatmaps: Entry Hour &times; Max Hold</h2>')

    for offset in OFFSETS:
        label = ("ATM Straddle" if offset == 0
                 else "Strangle &plusmn;$%d" % offset)

        def make_entry_hold_fn(off):
            def fn(row, col):
                h = int(row.split(":")[0])
                mh = int(col.replace("h", ""))
                best = None
                for trig in INDEX_TRIGGERS:
                    key = (off, h, trig, mh)
                    s = stats.get(key)
                    if s and s["n"] >= MIN_SAMPLES:
                        if best is None or s["avg_pnl"] > best:
                            best = s["avg_pnl"]
                return best
            return fn

        parts.append(build_heatmap(
            label, "Best avg P&amp;L across triggers",
            ["%02d:00" % h for h in hours_with_data],
            ["%dh" % h for h in MAX_HOLDS],
            make_entry_hold_fn(offset)))

    # Conversion Efficiency
    parts.append(
        '<h2>Conversion Efficiency: Avg P&amp;L &divide; Avg Premium</h2>')

    for offset in OFFSETS:
        label = ("ATM Straddle" if offset == 0
                 else "Strangle &plusmn;$%d" % offset)

        def make_eff_fn(off):
            def fn(row, col):
                h = int(row.split(":")[0])
                trig = int(col.replace("$", "").replace(",", ""))
                prems = entry_premiums.get((h, off), [])
                if not prems:
                    return None
                avg_prem = statistics.mean(prems)
                if avg_prem < 5:
                    return None
                best = None
                for mh in MAX_HOLDS:
                    key = (off, h, trig, mh)
                    s = stats.get(key)
                    if s and s["n"] >= MIN_SAMPLES:
                        if best is None or s["avg_pnl"] > best:
                            best = s["avg_pnl"]
                if best is not None:
                    return best / avg_prem * 100
                return None
            return fn

        parts.append(build_heatmap(
            label, "Avg P&amp;L &divide; premium (%)",
            ["%02d:00" % h for h in hours_with_data],
            ["$%d" % t for t in triggers],
            make_eff_fn(offset), fmt="%"))

    # Win rate heatmaps
    parts.append('<h2>Win Rate Heatmaps: Entry Hour &times; Trigger</h2>')

    for offset in OFFSETS:
        label = ("ATM Straddle" if offset == 0
                 else "Strangle &plusmn;$%d" % offset)

        def make_wr_fn(off):
            def fn(row, col):
                h = int(row.split(":")[0])
                trig = int(col.replace("$", "").replace(",", ""))
                best_wr = None
                best_avg = -1e18
                for mh in MAX_HOLDS:
                    key = (off, h, trig, mh)
                    s = stats.get(key)
                    if s and s["n"] >= MIN_SAMPLES:
                        if s["avg_pnl"] > best_avg:
                            best_avg = s["avg_pnl"]
                            best_wr = s["win_rate"] * 100
                return best_wr
            return fn

        parts.append(build_heatmap(
            label, "Win rate for best-hold combo",
            ["%02d:00" % h for h in hours_with_data],
            ["$%d" % t for t in triggers],
            make_wr_fn(offset), fmt="%"))

    # Top 30 combos
    ranked = []
    for key, s in stats.items():
        if s["n"] < MIN_SAMPLES:
            continue
        offset, entry_h, trigger, max_hold = key
        prems = entry_premiums.get((entry_h, offset), [])
        avg_prem = statistics.mean(prems) if prems else 0
        ranked.append({
            "label": offset_label_short(offset),
            "entry_h": entry_h,
            "trigger": trigger,
            "max_hold": max_hold,
            "avg_pnl": s["avg_pnl"],
            "median_pnl": s["median_pnl"],
            "win_rate": s["win_rate"],
            "trig_rate": s["trigger_rate"],
            "n": s["n"],
            "premium": avg_prem,
        })
    ranked.sort(key=lambda x: x["avg_pnl"], reverse=True)

    parts.append('<h2>Top 30 Combinations</h2>')
    parts.append('<table class="ranked">')
    parts.append(
        '<tr><th>#</th><th>Structure</th><th>Entry</th><th>Trigger</th>'
        '<th>Max Hold</th><th>Avg P&amp;L</th><th>Median</th>'
        '<th>Win%%</th><th>Trig%%</th><th>AvgPrem</th><th>n</th></tr>')
    for i, r in enumerate(ranked[:30], 1):
        pc = pnl_class(r["avg_pnl"])
        parts.append(
            '<tr><td>%d</td><td>%s</td><td>%02d:00</td><td>$%d</td>'
            '<td>%dh</td><td class="%s">$%s</td>'
            '<td class="%s">$%s</td>'
            '<td>%.0f%%</td><td>%.0f%%</td>'
            '<td>$%s</td><td>%d</td></tr>' % (
                i, r["label"], r["entry_h"], r["trigger"], r["max_hold"],
                pc, "{:,.0f}".format(r["avg_pnl"]),
                pnl_class(r["median_pnl"]),
                "{:,.0f}".format(r["median_pnl"]),
                r["win_rate"] * 100, r["trig_rate"] * 100,
                "{:,.0f}".format(r["premium"]), r["n"]))
    parts.append('</table>')

    # Best per structure
    parts.append('<h2>Best Configuration Per Structure</h2>')
    parts.append('<table class="ranked">')
    parts.append(
        '<tr><th>Structure</th><th>Entry</th><th>Trigger</th><th>Hold</th>'
        '<th>Avg P&amp;L</th><th>Win%%</th><th>Trig%%</th>'
        '<th>AvgPrem</th><th>Return</th><th>n</th></tr>')

    for offset in OFFSETS:
        label = ("ATM Straddle" if offset == 0
                 else "Strangle &plusmn;$%d" % offset)
        best_key = None
        best_avg = -1e18
        for key, s in stats.items():
            if key[0] == offset and s["n"] >= MIN_SAMPLES:
                if s["avg_pnl"] > best_avg:
                    best_avg = s["avg_pnl"]
                    best_key = key
        if best_key:
            off, eh, trig, mh = best_key
            s = stats[best_key]
            prems = entry_premiums.get((eh, off), [])
            avg_prem = statistics.mean(prems) if prems else 1
            ret = best_avg / avg_prem * 100 if avg_prem > 0 else 0
            pc = pnl_class(best_avg)
            parts.append(
                '<tr><td>%s</td><td>%02d:00</td><td>$%d</td><td>%dh</td>'
                '<td class="%s">$%s</td><td>%.0f%%</td><td>%.0f%%</td>'
                '<td>$%s</td><td>%.0f%%</td><td>%d</td></tr>' % (
                    label, eh, trig, mh,
                    pc, "{:,.0f}".format(best_avg),
                    s["win_rate"] * 100, s["trigger_rate"] * 100,
                    "{:,.0f}".format(avg_prem), ret, s["n"]))
    parts.append('</table>')

    # Best per entry hour
    parts.append('<h2>Best Result Per Entry Hour</h2>')
    parts.append('<table class="ranked">')
    parts.append(
        '<tr><th>Hour</th><th>Avg BTC</th><th>Avg Vol</th>'
        '<th>Structure</th><th>Trigger</th><th>Hold</th>'
        '<th>Avg P&amp;L</th><th>Win%%</th><th>Trig%%</th>'
        '<th>AvgPrem</th><th>n</th></tr>')

    for hour in range(MAX_ENTRY_HOUR + 1):
        best_key = None
        best_avg = -1e18
        for key, s in stats.items():
            if key[1] == hour and s["n"] >= MIN_SAMPLES:
                if s["avg_pnl"] > best_avg:
                    best_avg = s["avg_pnl"]
                    best_key = key
        if best_key is None:
            continue
        off, eh, trig, mh = best_key
        s = stats[best_key]
        spots = entry_spots.get(hour, [])
        vols = entry_vols.get(hour, [])
        avg_spot = statistics.mean(spots) if spots else 0
        avg_vol = statistics.mean(vols) if vols else 0
        prems = entry_premiums.get((hour, off), [])
        avg_prem = statistics.mean(prems) if prems else 0
        pc = pnl_class(best_avg)
        parts.append(
            '<tr><td>%02d:00</td><td>$%s</td><td>%.0f%%</td>'
            '<td>%s</td><td>$%d</td><td>%dh</td>'
            '<td class="%s">$%s</td><td>%.0f%%</td><td>%.0f%%</td>'
            '<td>$%s</td><td>%d</td></tr>' % (
                hour, "{:,.0f}".format(avg_spot), avg_vol * 100,
                offset_label_short(off), trig, mh,
                pc, "{:,.0f}".format(best_avg),
                s["win_rate"] * 100, s["trigger_rate"] * 100,
                "{:,.0f}".format(avg_prem), s["n"]))
    parts.append('</table>')

    parts.append('\n</body></html>')
    return '\n'.join(parts)
