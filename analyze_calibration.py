"""
Odds calibration analysis: actual hit-rate vs odds-implied probability,
bucketed across all resolved picks in the past ~2 months of backtest data.
"""
import glob
import math
import os
import re
from collections import defaultdict
from datetime import date

import openpyxl


# ---------- helpers ----------

def parse_american(odds_str):
    """'+128' / '-110' -> int (None if unparseable)."""
    if odds_str is None:
        return None
    s = str(odds_str).strip().replace(' ', '')
    m = re.match(r'^([+-]?)(\d+)$', s)
    if not m:
        return None
    sign, num = m.groups()
    val = int(num)
    if sign == '-':
        return -val
    return val


def implied_prob(american):
    """American odds -> raw implied probability (vig-included)."""
    if american is None:
        return None
    if american < 0:
        return (-american) / ((-american) + 100)
    return 100 / (american + 100)


def wilson_ci(wins, n, z=1.96):
    """95% Wilson score CI for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def parse_game_date(v):
    """Excel cell -> date (None on failure)."""
    if v is None:
        return None
    if isinstance(v, date):
        return v
    s = str(v).strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


# ---------- load ----------

ROOT = os.path.dirname(os.path.abspath(__file__))

def all_backtest_files():
    files = []
    for fn in os.listdir(ROOT):
        if fn.startswith('backtest_') and fn.endswith('.xlsx'):
            files.append(os.path.join(ROOT, fn))
    files += sorted(glob.glob(os.path.join(ROOT, '_bt_recent', '*.xlsx')))
    files += sorted(glob.glob(os.path.join(ROOT, 'Backtest Archives', '*.xlsx')))
    return files


def load_all_picks(window_start, window_end):
    """Loads from 'All Picks' (main markets) and 'Player Props' sheets.

    Returns dict keyed by unique bet -> dict. Excludes promos.
    """
    bets = {}
    files = all_backtest_files()
    for fp in files:
        try:
            wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
        except Exception as e:
            print(f'  skip {os.path.basename(fp)}: {e}')
            continue
        for sheet_name, pick_col in (('All Picks', 'Pick'), ('Player Props', 'Player')):
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            header = None
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    header = {name: idx for idx, name in enumerate(row)}
                    continue
                try:
                    gd = parse_game_date(row[header['Game Date']])
                    if gd is None or gd < window_start or gd > window_end:
                        continue
                    result = row[header['Result']]
                    if result not in ('WIN', 'LOSS'):
                        continue
                    odds = parse_american(row[header['Odds']])
                    if odds is None:
                        continue
                    # Player Props uses Player+Side, All Picks uses Pick
                    if sheet_name == 'Player Props':
                        pick = f"{row[header['Player']]}/{row[header['Side']]}"
                        market = row[header['Market']]
                    else:
                        pick = row[header[pick_col]]
                        market = row[header['Market']]
                    key = (
                        gd.isoformat(),
                        row[header['Sport']],
                        row[header['Game']],
                        row[header['Book']],
                        market,
                        pick,
                        row[header['Line']],
                        odds,
                    )
                    if key not in bets:
                        bets[key] = {
                            'game_date': gd,
                            'sport': row[header['Sport']],
                            'market': market,
                            'book': row[header['Book']],
                            'odds': odds,
                            'result': result,
                            'is_prop': sheet_name == 'Player Props',
                        }
                except (KeyError, IndexError, TypeError):
                    continue
        wb.close()
    return bets


# ---------- bucketing ----------

# Implied-probability bins (vig-included).
PROB_BINS = [
    (0.00, 0.20, '< 20% (longshots, +400+)'),
    (0.20, 0.30, '20-30% (+233 to +400)'),
    (0.30, 0.40, '30-40% (+150 to +233)'),
    (0.40, 0.45, '40-45% (+122 to +150)'),
    (0.45, 0.50, '45-50% (+101 to +122)'),
    (0.50, 0.525, '50.0-52.5% (-105 to +100)'),
    (0.525, 0.55, '52.5-55% (-122 to -105)'),
    (0.55, 0.575, '55.0-57.5% (-136 to -122)'),
    (0.575, 0.60, '57.5-60% (-150 to -136)'),
    (0.60, 0.65, '60-65% (-186 to -150)'),
    (0.65, 0.70, '65-70% (-233 to -186)'),
    (0.70, 0.80, '70-80% (-400 to -233)'),
    (0.80, 1.01, '> 80% (-400 and lower)'),
]

def bin_for(p):
    for lo, hi, label in PROB_BINS:
        if lo <= p < hi:
            return label
    return None


# American-odds bins. Inclusive low, inclusive high (both ends).
# Ordered favorites -> underdogs.
AMERICAN_BINS = [
    (-100000, -400, 'worse than -400 (heavy favs)'),
    (-399,    -251, '-400 to -251'),
    (-250,    -176, '-250 to -176'),
    (-175,    -141, '-175 to -141'),
    (-140,    -121, '-140 to -121'),
    (-120,    -111, '-120 to -111'),
    (-110,    -106, '-110 to -106'),
    (-105,    -101, '-105 to -101'),
    (+100,    +104, '+100 to +104 (even/pickem)'),
    (+105,    +110, '+105 to +110'),
    (+111,    +120, '+111 to +120'),
    (+121,    +140, '+121 to +140'),
    (+141,    +175, '+141 to +175'),
    (+176,    +250, '+176 to +250'),
    (+251,    +400, '+251 to +400'),
    (+401,  100000, 'worse than +400 (longshots)'),
]

def bin_for_american(odds):
    for lo, hi, label in AMERICAN_BINS:
        if lo <= odds <= hi:
            return label
    return None


# ---------- analysis ----------

def analyze(bets, label='', mode='prob'):
    """mode='prob' bins by implied probability, mode='american' bins by American odds."""
    if mode == 'american':
        bins = AMERICAN_BINS
        bin_fn = lambda b: bin_for_american(b['odds'])
        width = 35
    else:
        bins = PROB_BINS
        bin_fn = lambda b: bin_for(implied_prob(b['odds']))
        width = 35

    buckets = defaultdict(lambda: {'n': 0, 'w': 0, 'imp_sum': 0.0, 'odds_sum': 0})
    overall = {'n': 0, 'w': 0, 'imp_sum': 0.0}
    for b in bets.values():
        p = implied_prob(b['odds'])
        if p is None:
            continue
        lab = bin_fn(b)
        if lab is None:
            continue
        buckets[lab]['n'] += 1
        buckets[lab]['imp_sum'] += p
        buckets[lab]['odds_sum'] += b['odds']
        overall['n'] += 1
        overall['imp_sum'] += p
        if b['result'] == 'WIN':
            buckets[lab]['w'] += 1
            overall['w'] += 1

    print()
    print('=' * 120)
    print(f'CALIBRATION ({mode}): {label}')
    print('=' * 120)
    if mode == 'american':
        print(f'{"American odds bucket":<35} {"n":>5} {"wins":>5} {"hit%":>7} {"impl%":>7} {"edge":>7} {"avg odds":>9} {"95% CI":>17}')
    else:
        print(f'{"Bucket":<35} {"n":>5} {"wins":>5} {"hit%":>7} {"impl%":>7} {"edge":>7} {"95% CI":>17}')
    print('-' * 120)
    for lo, hi, lab in bins:
        s = buckets.get(lab)
        if not s or s['n'] == 0:
            continue
        hit = s['w'] / s['n']
        imp = s['imp_sum'] / s['n']
        edge = hit - imp
        ci_lo, ci_hi = wilson_ci(s['w'], s['n'])
        marker = ''
        if ci_lo > imp:
            marker = ' **beats**'
        elif ci_hi < imp:
            marker = ' **misses**'
        if mode == 'american':
            avg_odds = s['odds_sum'] / s['n']
            avg_str = f'{avg_odds:+.0f}'
            print(f'{lab:<35} {s["n"]:>5d} {s["w"]:>5d} {hit*100:>6.1f}% {imp*100:>6.1f}% {edge*100:>+6.1f}% '
                  f'{avg_str:>9} [{ci_lo*100:>5.1f}%, {ci_hi*100:>5.1f}%]{marker}')
        else:
            print(f'{lab:<35} {s["n"]:>5d} {s["w"]:>5d} {hit*100:>6.1f}% {imp*100:>6.1f}% {edge*100:>+6.1f}% '
                  f'[{ci_lo*100:>5.1f}%, {ci_hi*100:>5.1f}%]{marker}')

    if overall['n'] > 0:
        hit = overall['w'] / overall['n']
        imp = overall['imp_sum'] / overall['n']
        ci_lo, ci_hi = wilson_ci(overall['w'], overall['n'])
        print('-' * 120)
        if mode == 'american':
            print(f'{"OVERALL":<35} {overall["n"]:>5d} {overall["w"]:>5d} {hit*100:>6.1f}% {imp*100:>6.1f}% '
                  f'{(hit-imp)*100:>+6.1f}% {"":>9} [{ci_lo*100:>5.1f}%, {ci_hi*100:>5.1f}%]')
        else:
            print(f'{"OVERALL":<35} {overall["n"]:>5d} {overall["w"]:>5d} {hit*100:>6.1f}% {imp*100:>6.1f}% '
                  f'{(hit-imp)*100:>+6.1f}% [{ci_lo*100:>5.1f}%, {ci_hi*100:>5.1f}%]')


if __name__ == '__main__':
    end = date(2026, 5, 26)
    start = date(2026, 3, 26)
    print(f'Loading bets in window {start} .. {end}')
    bets = load_all_picks(start, end)
    print(f'\n{len(bets)} unique resolved picks (WIN/LOSS only)')

    # spot-check by sport
    by_sport = defaultdict(int)
    for b in bets.values():
        by_sport[b['sport']] += 1
    print('\nBy sport:')
    for s, n in sorted(by_sport.items(), key=lambda x: -x[1]):
        print(f'  {s:<20s} {n:>5d}')

    main_bets = {k: v for k, v in bets.items() if not v['is_prop']}
    prop_bets = {k: v for k, v in bets.items() if v['is_prop']}

    # American-odds buckets (what the user asked for this round)
    analyze(bets,      label='All bets, past ~2 months',                       mode='american')
    analyze(main_bets, label=f'Main markets only (H2H/SPREADS/TOTALS), n={len(main_bets)}', mode='american')
    analyze(prop_bets, label=f'Player props only, n={len(prop_bets)}',         mode='american')

    # Per-sport summary on main markets only (overall hit vs implied)
    print()
    print('=' * 80)
    print('PER-SPORT OVERALL CALIBRATION (main markets only, n>=50)')
    print('=' * 80)
    print(f'{"Sport":<15} {"n":>5} {"hit%":>7} {"impl%":>7} {"edge":>7} {"95% CI":>17}')
    print('-' * 80)
    by_sport = defaultdict(lambda: {'n': 0, 'w': 0, 'imp_sum': 0.0})
    for b in main_bets.values():
        p = implied_prob(b['odds'])
        if p is None:
            continue
        s = by_sport[b['sport']]
        s['n'] += 1
        s['imp_sum'] += p
        if b['result'] == 'WIN':
            s['w'] += 1
    rows = []
    for sport, s in by_sport.items():
        if s['n'] < 50:
            continue
        hit = s['w'] / s['n']
        imp = s['imp_sum'] / s['n']
        ci_lo, ci_hi = wilson_ci(s['w'], s['n'])
        rows.append((sport, s['n'], hit, imp, hit - imp, ci_lo, ci_hi))
    for sport, n, hit, imp, edge, ci_lo, ci_hi in sorted(rows, key=lambda r: -r[4]):
        marker = ''
        if ci_lo > imp:
            marker = ' beats'
        elif ci_hi < imp:
            marker = ' misses'
        print(f'{sport:<15} {n:>5d} {hit*100:>6.1f}% {imp*100:>6.1f}% {edge*100:>+6.1f}% '
              f'[{ci_lo*100:>5.1f}%, {ci_hi*100:>5.1f}%]{marker}')
